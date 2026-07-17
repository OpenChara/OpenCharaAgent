"""Code-level secret redaction (core/redact.py) + its use as the compaction
summary backstop. Apple-to-apple with the upstream reference's redact.py."""
from chara.core.redact import mask_secret, redact_sensitive_text


# ---- the patterns --------------------------------------------------------------

def test_api_key_prefixes_masked():
    r = redact_sensitive_text("my key is sk-or-v1-abcdef0123456789abcdef and ghp_abcdef0123456789")
    assert "sk-or-v1-abcdef0123456789abcdef" not in r
    assert "ghp_abcdef0123456789" not in r
    assert "..." in r  # masked, not just dropped


def test_env_assignment_masked():
    r = redact_sensitive_text("OPENAI_API_KEY=supersecretvalue1234567890")
    assert "supersecretvalue1234567890" not in r
    assert "OPENAI_API_KEY=" in r


def test_json_field_masked():
    r = redact_sensitive_text('{"api_key": "abcdef0123456789secret", "name": "ok"}')
    assert "abcdef0123456789secret" not in r
    assert '"name": "ok"' in r  # non-secret field untouched


def test_auth_header_masked():
    r = redact_sensitive_text("Authorization: Bearer eyJhbGciOiJ.payloadpart.sigpart")
    assert "payloadpart" not in r


def test_db_connstring_password_masked():
    r = redact_sensitive_text("postgres://user:hunter2password@db.example.com:5432/app")
    assert "hunter2password" not in r
    assert "postgres://user:***@db.example.com" in r


def test_jwt_masked():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdEFGHijkl"
    assert jwt not in redact_sensitive_text(f"token={jwt}")


def test_private_key_block_masked():
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIabcdef==\n-----END RSA PRIVATE KEY-----"
    r = redact_sensitive_text(f"here:\n{block}\ndone")
    assert "MIIabcdef" not in r
    assert "[REDACTED PRIVATE KEY]" in r


def test_non_secret_text_passes_through():
    s = "The operator asked to refactor the parser. Test 3/50 failed: test_parse at line 45."
    assert redact_sensitive_text(s) == s  # nothing masked


def test_short_token_fully_masked_long_keeps_ends():
    assert mask_secret("short") == "***"
    assert mask_secret("sk-proj-abcdef1234567890") == "sk-p...7890"


def test_force_redacts_even_when_globally_disabled(monkeypatch):
    # Simulate the global flag off; force=True must still redact (safety boundary).
    import chara.core.redact as R
    monkeypatch.setattr(R, "_REDACT_ENABLED", False)
    leaky = "key sk-or-v1-abcdef0123456789abcdef end"
    assert "sk-or-v1-abcdef0123456789abcdef" in R.redact_sensitive_text(leaky)          # disabled → passes
    assert "sk-or-v1-abcdef0123456789abcdef" not in R.redact_sensitive_text(leaky, force=True)  # force → masked


# ---- the compaction backstop (in + out) ----------------------------------------

class _FakeLLM:
    """Echoes a summary that itself contains a leaked secret (model ignored the
    [REDACTED] instruction) — the OUT-bound scrub must catch it."""
    def __init__(self, summary):
        self._summary = summary
        self.seen_prompt = ""

    def is_live(self):
        return True

    def raw_complete(self, messages, max_tokens=1024, timeout=60.0):
        self.seen_prompt = messages[0]["content"]
        return self._summary


def test_compaction_redacts_secret_in_and_out():
    from chara.core import compaction
    head = [
        {"role": "user", "content": "deploy with OPENAI_API_KEY=topsecretkey1234567890 please"},
        {"role": "assistant", "content": "done"},
    ]
    # the model "leaks" a key back into its summary
    llm = _FakeLLM(summary="Set the key sk-or-v1-leakedkey0123456789abcd during deploy.")
    out = compaction._summarize(head, 4000, llm)
    # IN: the conversation key never reached the summarizer prompt
    assert "topsecretkey1234567890" not in llm.seen_prompt
    # OUT: the model's leaked key was scrubbed before persisting
    assert "sk-or-v1-leakedkey0123456789abcd" not in out
