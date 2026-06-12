"""Skills (incl. the chara writing its own) and the minimal MCP stdio client."""
import json
import sys
import textwrap

import pytest

from lunamoth.tools.mcp import McpManager
from lunamoth.tools.skills import SkillStore, parse_frontmatter
from lunamoth.session.settings import Settings


# ---- skills ---------------------------------------------------------------------------


def _store(tmp_path):
    own = tmp_path / "own"
    user = tmp_path / "user"
    for base, name, desc in ((user, "brew-tea", "How to brew tea."), (user, "shared", "User version.")):
        f = base / name / "SKILL.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"---\nname: {name}\ndescription: {desc}\n---\nBody of {name}.\n", encoding="utf-8")
    return SkillStore(own_dir=own, dirs=[user])


def test_frontmatter_parsing():
    meta, body = parse_frontmatter("---\nname: x\ndescription: 'Quoted desc'\n---\nThe body.")
    assert meta == {"name": "x", "description": "Quoted desc"}
    assert body.strip() == "The body."
    assert parse_frontmatter("no frontmatter")[0] == {}


def test_scan_read_and_own_shadows_user(tmp_path):
    s = _store(tmp_path)
    assert {x["name"] for x in s.scan()} == {"brew-tea", "shared"}
    assert "Body of brew-tea" in s.read("brew-tea")
    # The chara revises "shared" for itself -> its version wins (hermes local-first).
    s.create("shared", "My own take.", "I do it differently.")
    mine = next(x for x in s.scan() if x["name"] == "shared")
    assert mine["origin"] == "own" and mine["description"] == "My own take."
    assert "differently" in s.read("shared")


def test_create_validates(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.create("Bad Name!", "desc", "body")
    with pytest.raises(ValueError):
        s.create("ok-name", "", "body")
    # Model-supplied frontmatter is replaced by the engine's (one source of truth).
    s.create("ok-name", "Real desc.", "---\nname: liar\ndescription: nope\n---\nactual body")
    text = s.read("ok-name")
    assert "Real desc." in text and "liar" not in text and "actual body" in text


def test_render_block_lists_index(tmp_path):
    s = _store(tmp_path)
    block = s.render_block()
    assert "brew-tea — How to brew tea." in block and "create_skill" in block
    assert SkillStore(own_dir=tmp_path / "none", dirs=[tmp_path / "nope"]).render_block() == ""


# ---- MCP ------------------------------------------------------------------------------

# A real subprocess speaking newline-delimited JSON-RPC: initialize, tools/list,
# and an "echo" tool. End-to-end through our client, no mocks.
_FAKE_SERVER = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue  # notification
        m = msg["method"]
        if m == "initialize":
            r = {"protocolVersion": "2025-03-26", "capabilities": {}}
        elif m == "tools/list":
            r = {"tools": [{"name": "echo", "description": "Echo text back.",
                            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                                            "required": ["text"]}}]}
        elif m == "tools/call":
            t = msg["params"]["arguments"].get("text", "")
            r = {"content": [{"type": "text", "text": f"echo: {t}"}]}
        else:
            r = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": r}) + "\\n")
        sys.stdout.flush()
""")


@pytest.fixture
def mcp(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"fake": {"command": sys.executable, "args": ["-c", _FAKE_SERVER]}}
    }), encoding="utf-8")
    mgr = McpManager(config_dir=tmp_path)
    yield mgr
    mgr.close_all()


def test_mcp_end_to_end(mcp):
    specs = mcp.schemas(["fake"])
    assert specs and specs[0]["function"]["name"] == "mcp__fake__echo"
    out = mcp.call("mcp__fake__echo", {"text": "月光"})
    assert out == "echo: 月光"


def test_mcp_pack_opt_in(mcp):
    assert mcp.allowed_servers(["*"]) == ["fake"]
    assert mcp.allowed_servers(["fake", "ghost"]) == ["fake"]
    assert mcp.allowed_servers([]) == [] and mcp.allowed_servers(None) == []


# ---- gateway integration ----------------------------------------------------------------


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_SANDBOX", str(tmp_path / "sandbox"))
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.core.agent import LunaMothAgent

    def make(**kw):
        kw.setdefault("toolpack", "sandbox")
        return LunaMothAgent(Settings(character_path="", **kw))

    return make


def test_chara_writes_and_reads_its_own_skill(agent):
    a = agent()
    out = a.tools.call("create_skill", name="fold-origami", description="Paper cranes.", content="Fold twice.")
    assert out["ok"], out
    out = a.tools.call("read_skill", name="fold-origami")
    assert out["ok"] and "Fold twice." in out["data"]
    blob = "\n".join(a._build_system_messages("hi"))
    assert "fold-origami" in blob  # the index rides the system prompt


def test_unconfigured_mcp_tool_is_denied(agent):
    a = agent()
    out = a.tools.call("mcp__ghost__anything", text="x")
    assert not out["ok"] and "denied" in out["error"]
