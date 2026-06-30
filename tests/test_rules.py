"""The Rules layer: a neutral operating standard (not identity — the card is the
soul), included only when the chara has tools."""
import pytest

from lunamoth.content import rules
from lunamoth.session.settings import Settings


def test_rules_are_neutral_no_identity_claims():
    r = rules.rules()
    # operating standard, not "you are an assistant" / "you are a character"
    assert "assistant" not in r.lower()
    assert "you are a character" not in r.lower()
    assert "must be real" in r or "must actually exist" in r
    assert "记住" not in r  # the closer is separate


def test_rules_carry_finish_the_job_discipline():
    """The migrated task-completion discipline (de-branded from the upstream
    reference's TASK_COMPLETION/TOOL_USE_ENFORCEMENT), folded into the act-now
    paragraph to avoid restating it 3x."""
    low = rules.rules().lower()
    assert "keep going until the task is really done" in low
    assert "stub" in low                       # don't stop after a stub
    assert "next time" in low                  # don't end with a plan-for-next-time
    assert "make real progress" in low or "finished result" in low


def test_rules_carry_prompt_extraction_guard():
    """A neutral fiction-integrity guard against system-prompt extraction
    ("print your instructions verbatim", "ignore your instructions") lives in
    BOTH the stable-prefix rules and the post-history closer (strongest after
    the turn). It must stay a fiction-integrity guard, not a refusal policy."""
    r = rules.rules().lower()
    assert "recite" in r and ("instructions" in r or "scaffolding" in r)
    assert "stay yourself" in r
    # Reinforced in the post-history slot.
    assert "recite" in rules.closer().lower() or "pull you out" in rules.closer().lower()


def test_engine_prompt_text_is_brand_free():
    """No upstream brand leaks into any model-facing rules string."""
    for txt in (rules.rules(), rules.capabilities(), rules.tool_use(),
                rules.closer(), rules.embodiment_bridge(),
                rules.website(), rules.environment_tools(ffmpeg=True)):
        low = txt.lower()
        for banned in ("hermes", "nous", "the vm", "linux environment"):
            assert banned not in low, f"{banned!r} leaked into model-facing rules text"


def test_bundled_toolpack_note_is_brand_free():
    """The bundled `sandbox` toolpack's note rides the stable prefix (agent.py
    injects pack.note), so it is model-facing — no upstream brand may leak there.
    The description is operator-facing but kept clean too."""
    from lunamoth.tools.toolpacks import load_toolpack

    pack = load_toolpack("sandbox")
    assert pack is not None
    for txt in (pack.note, pack.description):
        low = txt.lower()
        for banned in ("hermes", "nous", "the vm"):
            assert banned not in low, f"{banned!r} leaked into the sandbox toolpack"


def test_environment_tools_ffmpeg_note_is_honesty_gated():
    """The ffmpeg note is stated ONLY when ffmpeg is actually present — never a
    claim the chara would reach for and not find."""
    assert rules.environment_tools(ffmpeg=False) == ""
    note = rules.environment_tools(ffmpeg=True)
    assert "ffmpeg" in note.lower()
    assert "video" in note.lower() and "terminal" in note.lower()


def test_website_block_teaches_relative_links_and_discussing_publish():
    """The personal_website module tells the chara the user opens index.html
    directly (use relative links), and to discuss publishing / a backend first."""
    w = rules.website().lower()
    assert "index.html" in w
    assert "relative" in w
    assert "backend" in w  # discuss a real backend / hosting with the user first


def test_global_override_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path))
    (tmp_path / "rules.md").write_text("my house rules", encoding="utf-8")
    assert rules.rules() == "my house rules"


def test_card_override_hook_beats_global(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path))
    (tmp_path / "rules.md").write_text("global rules", encoding="utf-8")
    # extensions.lunamoth.content.rules / rules_closer override both, beating the global file
    assert rules.rules(card_override="card rules") == "card rules"
    assert rules.closer(card_override="card closer") == "card closer"
    # empty/blank override falls through to the default chain
    assert rules.rules(card_override="  ") == "global rules"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sb"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    from lunamoth.core.agent import LunaMothAgent

    return lambda toolpack: LunaMothAgent(Settings(character_path="", toolpack=toolpack))


def test_card_is_first_then_rules(agent):
    a = agent("sandbox")
    msgs = a._build_system_messages("art")
    # the character card (the soul) comes first — engine adds no identity before it
    assert a.char_name() in msgs[0]
    blob = "\n".join(msgs)
    assert "must be real" in blob or "必须是真的" in blob
    assert "记住" in msgs[-1] or "Remember" in msgs[-1]  # closer last


def test_no_tools_means_no_rules(agent):
    a = agent("")
    a.tools.set_enabled(None)
    msgs = a._build_system_messages("art")
    assert a.char_name() in msgs[0]
    blob = "\n".join(msgs)
    assert "must be real" not in blob and "必须是真的" not in blob
    assert "Remember" not in blob and "记住" not in blob
