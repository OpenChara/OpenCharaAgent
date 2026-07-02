"""The faithful per-turn request log: SANDBOX_ROOT/logs/requests.jsonl.

Always on, capped at the last 200 records, best-effort (never raises). The
content must be the EXACT system + messages + tools that the request used.
"""
import json

import pytest

from lunamoth.session.settings import Settings


@pytest.fixture
def agent(tmp_path, monkeypatch):
    # SANDBOX_ROOT pins at import — set env BEFORE importing the runtime module.
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.core.agent import LunaMothAgent

    def make(**kw):
        kw.setdefault("toolpack", "")
        return LunaMothAgent(Settings(character_path="", **kw))

    return make


def _requests_path():
    from lunamoth.config import SANDBOX_ROOT

    return SANDBOX_ROOT / "logs" / "requests.jsonl"


def test_handle_logs_a_faithful_request(agent):
    a = agent()
    a.transcript.reset()  # SANDBOX_ROOT is import-time global; isolate
    path = _requests_path()
    if path.exists():
        path.unlink()
    s = a.make_session()
    a.handle("hello there", s)
    assert path.exists()
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    rec = lines[-1]
    assert rec["kind"] == "send"
    assert rec["model"] == a.settings.model
    assert isinstance(rec["system"], list) and rec["system"]  # stable+volatile strings
    assert all(isinstance(s2, str) for s2 in rec["system"])
    # The messages are the SAME render view the request used (the operator's
    # line is in there).
    assert any(m.get("role") == "user" and "hello there" in str(m.get("content", ""))
               for m in rec["messages"])
    assert isinstance(rec["tools"], list)
    assert "ts" in rec


def test_request_log_caps_at_200_lines(agent):
    from lunamoth.core import request_log as agent_mod

    path = _requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    # The trim is amortized (every _REQUEST_LOG_TRIM_EVERY appends); start the
    # counter at the cusp so the very next batch crossing the cap trims to 200.
    agent_mod._request_log_appends = 0
    for i in range(250):
        agent_mod._append_request_log("send", [f"sys{i}"], [{"role": "user", "content": str(i)}], [], "m")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    # The OLDEST were dropped: the window holds 50..249.
    first = json.loads(lines[0])
    last = json.loads(lines[-1])
    assert first["messages"][0]["content"] == "50"
    assert last["messages"][0]["content"] == "249"


def test_request_log_never_raises(agent, monkeypatch):
    from lunamoth.core import request_log as agent_mod

    # A non-serializable payload must be swallowed, not raised.
    agent_mod._append_request_log("send", ["sys"], [{"role": "user", "content": object()}], [], "m")


def test_request_log_redacts_secrets(agent):
    """A secret flowing through context (this file is bundled into the export
    ZIP) must be masked before it ever lands on disk — never cleartext."""
    from lunamoth.core import request_log as agent_mod

    path = _requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    secret = "sk-ant-SUPERSECRETKEY0123456789abcdef"
    agent_mod._append_request_log(
        "send", ["use this key"],
        [{"role": "user", "content": f"my api key is {secret}"}], [], "m")
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw  # the literal key never appears
    # The record is still valid JSON with the surrounding structure intact.
    rec = json.loads(raw.splitlines()[-1])
    assert rec["kind"] == "send"
    assert "my api key is" in str(rec["messages"][0]["content"])


def test_request_log_stays_bounded_after_many_appends(agent):
    """Many appends keep the file bounded near the cap (no unbounded growth),
    and the file is never corrupted (every line parses) under repeated writes."""
    from lunamoth.core import request_log as agent_mod

    path = _requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    agent_mod._request_log_appends = 0  # deterministic counter start
    for i in range(1000):
        agent_mod._append_request_log(
            "send", [f"sys{i}"], [{"role": "user", "content": str(i)}], [], "m")
    lines = path.read_text(encoding="utf-8").splitlines()
    # Bounded near the cap: never grows without limit. The amortized trim allows
    # up to cap + trim-interval before the next sweep.
    assert len(lines) <= agent_mod._REQUEST_LOG_MAX_LINES + agent_mod._REQUEST_LOG_TRIM_EVERY
    assert len(lines) >= agent_mod._REQUEST_LOG_MAX_LINES
    # No corruption: every line is valid JSON, and the newest record survived.
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[-1]["messages"][0]["content"] == "999"
    # Records are strictly increasing (the atomic os.replace never scrambles order).
    nums = [int(p["messages"][0]["content"]) for p in parsed]
    assert nums == sorted(nums)


def test_request_log_elides_inline_image_bytes(agent):
    """Inline base64 image data URIs must be replaced by a short placeholder —
    the same image sits in context (and would be re-logged) EVERY turn, so the
    pixels are pure disk churn. The record's shape stays faithful and the input
    messages are never mutated."""
    from lunamoth.core import request_log as agent_mod

    path = _requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    payload = "data:image/png;base64," + "A" * 100_000
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": payload}},
    ]}]
    agent_mod._append_request_log("send", ["sys"], messages, [], "m")
    raw = path.read_text(encoding="utf-8")
    assert "A" * 1000 not in raw  # the pixels never land on disk
    rec = json.loads(raw.splitlines()[-1])
    parts = rec["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "look at this"}  # text intact
    assert parts[1]["type"] == "image_url"                       # shape intact
    assert parts[1]["image_url"]["url"].startswith("data:[inline image elided")
    # The caller's message list was NOT mutated (the context still needs the pixels).
    assert messages[0]["content"][1]["image_url"]["url"] == payload
    # A remote http(s) image URL is tiny and kept verbatim.
    agent_mod._append_request_log("send", ["sys"], [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.test/x.png"}}]}], [], "m")
    rec2 = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec2["messages"][0]["content"][0]["image_url"]["url"] == "https://example.test/x.png"


def test_request_log_trim_is_byte_bounded(agent, monkeypatch):
    """Oversized records are capped by BYTES, not just lines: the trim keeps a
    bounded tail (never reading the whole file), and every kept line is a
    complete, parseable record with the newest one intact."""
    from lunamoth.core import request_log as agent_mod

    path = _requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    monkeypatch.setattr(agent_mod, "_REQUEST_LOG_MAX_BYTES", 50_000)
    agent_mod._request_log_appends = 0
    big = "x" * 5_000  # ~5KB per record → one trim interval is well over the byte cap
    for i in range(agent_mod._REQUEST_LOG_TRIM_EVERY):
        agent_mod._append_request_log("send", [big], [{"role": "user", "content": str(i)}], [], "m")
    assert path.stat().st_size <= 50_000  # byte cap enforced even under 200 lines
    lines = path.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(ln) for ln in lines]  # no half-line at the window edge
    assert parsed[-1]["messages"][0]["content"] == str(agent_mod._REQUEST_LOG_TRIM_EVERY - 1)
    nums = [int(p["messages"][0]["content"]) for p in parsed]
    assert nums == sorted(nums)  # a contiguous newest tail, oldest dropped


def test_request_log_keeps_the_log_when_a_single_record_exceeds_the_byte_cap(agent, monkeypatch):
    """When the tail window holds no complete line (one record bigger than the
    byte cap), the trim used to rewrite the file to NOTHING. It must skip the
    rewrite instead — the newest record survives, even oversized."""
    from lunamoth.core import request_log as agent_mod

    path = _requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    monkeypatch.setattr(agent_mod, "_REQUEST_LOG_MAX_BYTES", 10_000)
    # The trim sweep fires exactly on this append.
    agent_mod._request_log_appends = agent_mod._REQUEST_LOG_TRIM_EVERY - 1
    big = "y" * 50_000  # one record far over the byte window
    agent_mod._append_request_log("send", [big], [{"role": "user", "content": "keep-me"}], [], "m")
    raw = path.read_text(encoding="utf-8")
    assert raw  # never self-emptied
    rec = json.loads(raw.splitlines()[-1])
    assert rec["messages"][0]["content"] == "keep-me"
    # And once normal-sized records follow, the next sweep trims back down.
    agent_mod._request_log_appends = 0
    for i in range(agent_mod._REQUEST_LOG_TRIM_EVERY):
        agent_mod._append_request_log("send", ["s"], [{"role": "user", "content": str(i)}], [], "m")
    assert path.stat().st_size <= 10_000


def test_request_log_no_temp_files_left_behind(agent):
    """The atomic trim must not leave .tmp scratch files in the logs dir."""
    from lunamoth.core import request_log as agent_mod

    path = _requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    agent_mod._request_log_appends = 0
    for i in range(600):
        agent_mod._append_request_log(
            "send", ["s"], [{"role": "user", "content": str(i)}], [], "m")
    leftovers = [p.name for p in path.parent.glob("requests.jsonl.tmp*")]
    assert leftovers == []
