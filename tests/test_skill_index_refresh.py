"""The in-prompt skill index stays fresh mid-session (hermes-shaped mtime/size
manifest): a skill written after startup appears next turn WITHOUT a /reset, while
an unchanged skill set never busts the stable-prefix cache (no thrash).

The agent is given an ISOLATED, empty skills store (SANDBOX_ROOT is pinned at import
time, so the env-based sandbox dir can't be relied on under a full-suite run)."""
import pytest

from lunamoth.session.settings import Settings
from lunamoth.tools.skills import SkillStore


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.core.agent import LunaMothAgent

    def make(**kw):
        kw.setdefault("toolpack", "sandbox")  # tools on → the skill index is in the prompt
        a = LunaMothAgent(Settings(character_path="", **kw))
        # Isolate skills to a clean tmp dir so the test is deterministic regardless
        # of the import-pinned SANDBOX_ROOT / any bundled library.
        a.skills = SkillStore(skills_dir=tmp_path / "iso-skills", external_dirs=[])
        a._freeze_skills()
        a._invalidate_stable_prefix()
        return a

    return make


def _write_skill(store, name, desc):
    d = store.skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n", encoding="utf-8")


def test_index_refreshes_when_a_skill_is_written_midsession(agent):
    a = agent()
    assert "midnight-ritual" not in "\n".join(a._build_system_messages("hi"))
    # a new skill lands on disk mid-session (the chara's skill_manage, or dropped in)
    _write_skill(a.skills, "midnight-ritual", "How to greet the moon.")
    # ...and the index reflects it on the next build, no /reset needed
    assert "midnight-ritual" in "\n".join(a._build_system_messages("hi"))


def test_unchanged_skills_keep_the_cached_prefix(agent):
    a = agent()
    _write_skill(a.skills, "stable-skill", "Does not change.")
    p1 = a._stable_prefix()
    p2 = a._stable_prefix()
    assert p1 is p2  # identical cached object — nothing changed, no rebuild/thrash


def test_manifest_does_not_follow_symlink_cycles(agent):
    # The per-turn freshness scan walks with followlinks=False, so a symlink cycle in a
    # skills dir can't turn the hot-path stat-walk into an infinite loop / huge traversal.
    a = agent()
    sd = a.skills.skills_dir
    sd.mkdir(parents=True, exist_ok=True)
    _write_skill(a.skills, "real-skill", "A genuine one.")
    try:
        (sd / "loop").symlink_to(sd, target_is_directory=True)  # a cycle back to the root
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    fp = a.skills.manifest()  # returns (no hang), and still sees the real skill
    assert isinstance(fp, dict)
    assert any("real-skill" in k for k in fp)


def test_a_filesystem_hiccup_does_not_kill_the_turn(agent, monkeypatch):
    a = agent()
    a._build_system_messages("hi")  # warm
    monkeypatch.setattr(a.skills, "manifest", lambda: (_ for _ in ()).throw(OSError("boom")))
    # the scan raises, but the turn still builds (best-effort: keep the snapshot)
    assert isinstance(a._build_system_messages("hi"), list)
