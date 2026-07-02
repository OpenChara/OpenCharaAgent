"""The minimal MCP stdio client + schema sanitization (audit #19/#20/#22).

(Skills moved to the hermes-shaped contract — those cases now live in
tests/test_memory_skills.py.)"""
import json
import sys
import textwrap
import time

import pytest

from lunamoth.tools.mcp import McpManager
from lunamoth.session.settings import Settings


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


# A server whose tool returns an image block + a text block that leaks a key.
_IMAGE_SECRET_SERVER = textwrap.dedent("""
    import json, sys, base64
    img = base64.b64encode(b"\\x89PNG\\r\\n\\x1a\\nFAKE").decode()
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        m = msg["method"]
        if m == "initialize":
            r = {"protocolVersion": "2025-03-26", "capabilities": {}}
        elif m == "tools/list":
            r = {"tools": [{"name": "shot", "description": "screenshot",
                            "inputSchema": {"type": "object", "properties": {}}}]}
        elif m == "tools/call":
            r = {"content": [
                {"type": "image", "data": img, "mimeType": "image/png"},
                {"type": "text", "text": "done; key sk-or-v1-abcdef0123456789abcdef end"},
            ]}
        else:
            r = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": r}) + "\\n")
        sys.stdout.flush()
""")


def test_mcp_image_result_saved_as_media_and_text_redacted(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"vis": {"command": sys.executable, "args": ["-c", _IMAGE_SECRET_SERVER]}}
    }), encoding="utf-8")
    ws = tmp_path / "ws"
    mgr = McpManager(config_dir=tmp_path, media_dir=ws)
    try:
        out = mgr.call("mcp__vis__shot", {})
    finally:
        mgr.close_all()
    # the image block is WRITTEN to the workspace and surfaced as a MEDIA: note,
    # not dropped as "[image content omitted]"
    assert "MEDIA:mcp/vis-shot-0.png" in out
    saved = ws / "mcp" / "vis-shot-0.png"
    assert saved.exists() and saved.read_bytes().startswith(b"\x89PNG")
    # the leaked key in the text block is redacted before it reaches the model
    assert "sk-or-v1-abcdef0123456789abcdef" not in out


def test_mcp_save_media_confines_hostile_names(tmp_path):
    """The tool name is MODEL-supplied and the mime type SERVER-supplied — both
    are interpolated into the write path, so a traversal shape in either must be
    sanitized and the write confined to the media dir."""
    import base64

    from lunamoth.tools.mcp import _Client

    c = _Client.__new__(_Client)  # no server spawn — exercise the write path only
    c.name = "vis"
    c.media_dir = tmp_path / "ws"
    payload = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKE").decode()

    note = c._save_media({"type": "image", "data": payload, "mimeType": "image/png"},
                         "../../../../etc/cron.d/evil", 0)
    assert note is not None  # sanitized, not dropped — the content still surfaces
    note2 = c._save_media({"type": "image", "data": payload, "mimeType": "image/../../png"},
                          "shot", 1)
    assert note2 is not None
    files = [p for p in (tmp_path / "ws").rglob("*") if p.is_file()]
    assert len(files) == 2
    base = (tmp_path / "ws").resolve()
    for p in files:
        assert base in p.resolve().parents  # every write stayed under the media dir
        assert "/" not in p.name and not p.name.startswith(".")
    assert not (tmp_path / "etc").exists()  # nothing escaped alongside the workspace


def test_mcp_safe_component():
    from lunamoth.tools.mcp import _safe_component

    assert _safe_component("shot", "t") == "shot"
    assert _safe_component("a tool/name", "t") == "a_tool_name"
    s = _safe_component("../../../etc/passwd", "t")
    assert "/" not in s and not s.startswith(".")
    assert _safe_component("", "fallback") == "fallback"
    assert _safe_component("..", "fallback") == "fallback"  # nothing safe survives
    assert len(_safe_component("x" * 500, "t")) <= 64


# A server that answers the handshake but hangs forever on tools/call —
# the audit-#19 wedge: without a real RPC timeout this blocked the turn forever.
_HANGING_SERVER = textwrap.dedent("""
    import json, sys, time
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        m = msg["method"]
        if m == "initialize":
            r = {"protocolVersion": "2025-03-26", "capabilities": {}}
        elif m == "tools/list":
            r = {"tools": [{"name": "hang", "description": "Never answers.",
                            "inputSchema": {"type": "object", "properties": {}}}]}
        else:
            time.sleep(3600)
            r = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": r}) + "\\n")
        sys.stdout.flush()
""")

# A server that never answers anything — hung handshake.
_MUTE_SERVER = "import time; time.sleep(3600)"


def _manager(tmp_path, script):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"hung": {"command": sys.executable, "args": ["-c", script]}}
    }), encoding="utf-8")
    return McpManager(config_dir=tmp_path)


def test_mcp_call_timeout_kills_and_marks_dead(tmp_path, monkeypatch):
    import lunamoth.tools.mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "_CALL_TIMEOUT", 0.3)
    mgr = _manager(tmp_path, _HANGING_SERVER)
    try:
        assert mgr.schemas(["hung"])  # handshake works
        client = mgr._client("hung")
        proc = client.proc
        t0 = time.monotonic()
        with pytest.raises(mcp_mod.McpError, match="timed out"):
            mgr.call("mcp__hung__hang", {})
        assert time.monotonic() - t0 < 5  # bounded, not a wedge
        # The hung server was killed AND reaped — no zombie, no orphan.
        assert proc.poll() is not None
        # Marked dead: the next call fails fast instead of restart-and-hang.
        t0 = time.monotonic()
        with pytest.raises(mcp_mod.McpError, match="disabled"):
            mgr.call("mcp__hung__hang", {})
        assert time.monotonic() - t0 < 0.2
    finally:
        mgr.close_all()


def test_mcp_hung_handshake_does_not_wedge_schemas(tmp_path, monkeypatch):
    import lunamoth.tools.mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "_CONNECT_TIMEOUT", 0.3)
    mgr = _manager(tmp_path, _MUTE_SERVER)
    try:
        t0 = time.monotonic()
        assert mgr.schemas(["hung"]) == []  # skipped, no fabricated entries
        assert time.monotonic() - t0 < 5
    finally:
        mgr.close_all()


def test_mcp_close_reaps_the_server(mcp):
    mcp.schemas(["fake"])  # spawn it
    proc = mcp._client("fake").proc
    assert proc.poll() is None
    mcp.close_all()
    assert proc.poll() is not None  # waited for, not just signalled


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


def test_unconfigured_mcp_tool_is_denied(agent):
    a = agent()
    out = a.tools.call("mcp__ghost__anything", text="x")
    assert not out["ok"] and "denied" in out["error"]


def test_mcp_server_stderr_lands_in_the_shared_log(tmp_path, monkeypatch):
    """A crashing server must leave diagnostics (audit #20): stderr goes to
    sandbox/logs/mcp-stderr.log with a per-spawn header, never DEVNULL."""
    import lunamoth.tools.mcp as M

    monkeypatch.setattr(M, "SANDBOX_ROOT", tmp_path)
    client = M._Client("whiny", {
        "command": "/bin/sh",
        "args": ["-c", "echo BOOM-DIAGNOSTIC >&2; exit 3"],
    })
    try:
        client.list_tools()
    except M.McpError:
        pass  # the crash itself is expected — we're after the diagnostics
    log = (tmp_path / "logs" / "mcp-stderr.log").read_text(encoding="utf-8")
    assert "--- whiny (/bin/sh)" in log
    assert "BOOM-DIAGNOSTIC" in log


# ---- schema sanitization (audit #22) ----------------------------------------------------

from lunamoth.tools.schema_sanitizer import sanitize_input_schema


def test_sanitize_nullable_union_collapses_to_non_null():
    # Pydantic/MCP optional field: anyOf[X, null] → X with a nullable hint.
    out = sanitize_input_schema({
        "type": "object",
        "properties": {
            "q": {"anyOf": [{"type": "string"}, {"type": "null"}],
                  "description": "the query", "default": None},
        },
    })
    q = out["properties"]["q"]
    assert q["type"] == "string" and q["nullable"] is True
    assert q["description"] == "the query"  # outer metadata carried over
    assert "anyOf" not in q
    # A genuine two-branch union is meaningful and left intact.
    keep = sanitize_input_schema({
        "type": "object",
        "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
    })
    assert "anyOf" in keep["properties"]["x"]


def test_sanitize_array_type_and_empty_object():
    out = sanitize_input_schema({
        "type": "object",
        "properties": {
            "n": {"type": ["integer", "null"]},
            "blob": {"type": "object"},  # no properties → grammar-hostile
        },
    })
    n = out["properties"]["n"]
    assert n["type"] == "integer" and n["nullable"] is True
    assert out["properties"]["blob"]["properties"] == {}


def test_sanitize_top_level_combinator_and_bad_required():
    # Top-level anyOf and a non-object top get forced to a plain object;
    # required entries naming absent properties are pruned.
    out = sanitize_input_schema({
        "anyOf": [{"type": "object"}, {"type": "null"}],
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a", "ghost"],
    })
    assert out["type"] == "object" and "anyOf" not in out
    assert out["required"] == ["a"]
    # A bare-string schema (malformed MCP output) becomes a valid object.
    assert sanitize_input_schema("object") == {"type": "object", "properties": {}}
    # Missing schema → minimal valid object.
    assert sanitize_input_schema(None) == {"type": "object", "properties": {}}


def test_schemas_does_not_mutate_the_cached_input_schema(tmp_path):
    # THE SCAR: sanitizing must deep-copy, never mutate the client's cached
    # _tools entry, or a second turn would see a corrupted schema.
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"fake": {"command": sys.executable, "args": ["-c", _FAKE_SERVER]}}
    }), encoding="utf-8")
    mgr = McpManager(config_dir=tmp_path)
    try:
        first = mgr.schemas(["fake"])[0]["function"]["parameters"]
        # Mutate the returned (sanitized) copy.
        first["properties"]["INJECTED"] = {"type": "string"}
        cached = mgr._client("fake")._tools[0]["inputSchema"]
        assert "INJECTED" not in cached["properties"]  # cache untouched
        second = mgr.schemas(["fake"])[0]["function"]["parameters"]
        assert "INJECTED" not in second["properties"]  # next turn is clean
    finally:
        mgr.close_all()


_HOSTILE_SERVER = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        m = msg["method"]
        if m == "initialize":
            r = {"protocolVersion": "2025-03-26", "capabilities": {}}
        elif m == "tools/list":
            r = {"tools": [{"name": "hostile", "description": "Strict-backend hostile schema.",
                            "inputSchema": {"type": "object", "properties": {
                                "opt": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                "free": {"type": "object"}}}}]}
        else:
            r = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": r}) + "\\n")
        sys.stdout.flush()
""")


def test_schemas_sanitizes_hostile_server_schema_end_to_end(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"h": {"command": sys.executable, "args": ["-c", _HOSTILE_SERVER]}}
    }), encoding="utf-8")
    mgr = McpManager(config_dir=tmp_path)
    try:
        params = mgr.schemas(["h"])[0]["function"]["parameters"]
        assert "anyOf" not in params["properties"]["opt"]
        assert params["properties"]["opt"]["type"] == "string"
        assert params["properties"]["free"]["properties"] == {}
    finally:
        mgr.close_all()
