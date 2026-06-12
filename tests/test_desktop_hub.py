"""Desktop hub gateway: roster RPC, wake/freeze, defaults, drafts (server/hub.py).

Everything runs against a temp LUNAMOTH_HOME; no network, no LLM (provider
HTTP paths are exercised separately / mocked here)."""
import json
from pathlib import Path

import pytest

from lunamoth.server import hub as H
from lunamoth.session import sessions as S


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / "home"))
    yield tmp_path / "home"


def dispatch(method, params=None):
    out = []
    d = H.HubDispatcher(lambda f: out.append(f) or True)
    resp = d.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    return resp


def result(method, params=None):
    resp = dispatch(method, params)
    assert "error" not in resp, resp.get("error")
    return resp["result"]


def rpc_error(method, params=None):
    resp = dispatch(method, params)
    assert "error" in resp, resp
    return resp["error"]


def luna_card_path():
    return str(H.bundled_cards_dir() / "LunaMoth.zh.json")


def set_defaults():
    H.save_defaults({"provider": "openrouter", "base_url": "https://example.invalid/v1",
                     "api_key": "sk-test", "model": "test/model"})


def draft_payload(svg="<svg viewBox=\"0 0 64 64\"><circle cx=\"32\" cy=\"32\" r=\"20\" fill=\"#7C5CFF\"/></svg>"):
    return {
        "name": "Aster",
        "description": "Aster is a lantern keeper from a quiet orbital garden. "
        "They speak gently, collect small impossible weather signs, and keep careful notes for visitors.",
        "first_mes": "The lanterns are awake. Did you bring a question for the dark?",
        "world_entries": [
            {"keys": ["Orbital Garden"], "content": "A ring habitat where seasons are tuned by old mirrors.", "constant": True},
            {"keys": ["Lantern Archive"], "content": "Aster's catalogue of weather, omens, and names.", "constant": False},
        ],
        "seed_goals": ["Map the mirror-season drift", "Welcome careful visitors"],
        "tagline": "A gentle keeper of orbital lanterns",
        "theme_color": "#7c5cff",
        "avatar_svg": svg,
    }


def mock_completion(monkeypatch, content, seen=None):
    def fake_http(url, api_key="", payload=None, timeout=H._HTTP_TIMEOUT):
        if seen is not None:
            seen.append({"url": url, "api_key": api_key, "payload": payload, "timeout": timeout})
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(H, "_http_json", fake_http)


# ---- hub.state & defaults -----------------------------------------------------

def test_state_first_run_and_cards():
    r = result("hub.state")
    assert r["first_run"] is True
    assert r["sessions"] == []
    names = {c["name"] for c in r["cards"]}
    assert "月蛾" in names  # bundled deck is visible
    assert r["defaults"]["has_key"] is False


def test_defaults_never_echo_the_key():
    set_defaults()
    r = result("defaults.get")
    assert r["has_key"] is True
    assert "api_key" not in r
    raw = json.loads(H.desktop_config_path().read_text(encoding="utf-8"))
    assert raw["api_key"] == "sk-test"  # stored, just never echoed


def test_defaults_set_ignores_unknown_fields():
    r = result("defaults.set", {"provider": "openrouter", "evil": "x", "ui_lang": "zh"})
    assert r["provider"] == "openrouter"
    assert "evil" not in r


def test_defaults_set_reports_key_rotation_candidates_and_apply_key_rewrites_only_matching_configs():
    H.save_defaults({"provider": "openrouter", "base_url": "https://example.invalid/v1",
                     "api_key": "old-key", "model": "test/model"})
    a = result("session.wake", {"card": luna_card_path(), "name": "same"})
    b = result("session.wake", {"card": luna_card_path(), "name": "other"})
    c = result("session.wake", {"card": luna_card_path(), "name": "current"})
    same = S.load_session(a["name"])
    other = S.load_session(b["name"])
    current = S.load_session(c["name"])
    other_cfg = json.loads(other.config_path.read_text(encoding="utf-8"))
    other_cfg["base_url"] = "https://elsewhere.invalid/v1"
    other.config_path.write_text(json.dumps(other_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    current_cfg = json.loads(current.config_path.read_text(encoding="utf-8"))
    current_cfg["api_key"] = "new-key"
    current.config_path.write_text(json.dumps(current_cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    saved = result("defaults.set", {"api_key": "new-key"})

    candidates = saved["key_update_candidates"]
    assert [x["name"] for x in candidates] == [same.name]
    assert "api_key" not in json.dumps(candidates)

    applied = result("defaults.apply_key", {"names": [same.name, other.name, current.name, "missing"]})
    assert applied["updated"] == [same.name]
    skipped = {x["name"]: x["reason"] for x in applied["skipped"]}
    assert skipped[other.name] == "provider_base_url_mismatch"
    assert skipped[current.name] == "already_current"
    assert skipped["missing"] == "missing"

    assert json.loads(same.config_path.read_text(encoding="utf-8"))["api_key"] == "new-key"
    assert json.loads(other.config_path.read_text(encoding="utf-8"))["api_key"] == "old-key"
    assert json.loads(current.config_path.read_text(encoding="utf-8"))["api_key"] == "new-key"
    # The rewrite preserves unrelated fields from the per-session config.
    assert json.loads(same.config_path.read_text(encoding="utf-8"))["character_path"].endswith("card.json")


def test_apply_key_validates_names_param():
    err = rpc_error("defaults.apply_key", {"names": "not a list"})
    assert err["code"] == -32602


# ---- wake (instantiation) ------------------------------------------------------

def test_wake_freezes_card_and_writes_config():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path(), "isolation": "sandbox"})
    assert entry["char_name"] == "月蛾"
    assert entry["status"] == "idle"
    meta = S.load_session(entry["name"])
    assert meta is not None
    frozen = meta.root / "card.json"
    assert frozen.exists()
    assert (meta.root / "card_source").read_text(encoding="utf-8") == luna_card_path()
    cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
    assert cfg["character_path"] == str(frozen)
    assert cfg["api_key"] == "sk-test"
    assert cfg["toolpack"] == "sandbox"  # from the card's extensions.lunamoth
    assert cfg["py_backend"] == "sandbox"


def test_wake_without_model_config_is_refused():
    err = rpc_error("session.wake", {"card": luna_card_path()})
    assert "no model configured" in err["message"]


def test_wake_twice_gets_distinct_names_and_freezes_deck_card():
    set_defaults()
    a = result("session.wake", {"card": luna_card_path()})
    b = result("session.wake", {"card": luna_card_path()})
    assert a["name"] != b["name"]
    cards = result("cards.list")
    luna = next(c for c in cards if c["path"] == luna_card_path())
    assert luna["frozen"] is True
    assert set(luna["used_by"]) == {a["name"], b["name"]}


# ---- delete / export -----------------------------------------------------------

def test_delete_requires_exact_confirmation():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    err = rpc_error("session.delete", {"name": entry["name"], "confirm": "nope"})
    assert err["code"] == -32034
    result("session.delete", {"name": entry["name"], "confirm": entry["name"]})
    assert S.load_session(entry["name"]) is None


def test_export_zips_the_whole_session(tmp_path, monkeypatch):
    set_defaults()
    monkeypatch.setattr(H.Path, "home", classmethod(lambda cls: tmp_path))
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    (meta.sandbox_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (meta.sandbox_dir / "workspace" / "art.txt").write_text("aurora", encoding="utf-8")
    r = result("session.export", {"name": entry["name"]})
    assert r["path"].endswith(".zip")
    import zipfile

    names = zipfile.ZipFile(r["path"]).namelist()
    assert any(n.endswith("workspace/art.txt") for n in names)
    assert any(n.endswith("card.json") for n in names)


# ---- cards: drafts, save, delete ------------------------------------------------

def test_card_from_draft_roundtrip():
    draft = {
        "name": "白枢", "appearance": "修复师。", "personality": "温和而固执。",
        "scenario": "长夜图书馆。", "first_mes": "轻一点关门。",
        "alternate_greetings": ["你来了。"],
        "world": [{"key": "长夜图书馆", "desc": "只在日落后开门。", "constant": True}],
        "relationship": "你是少数能进工作间的访客。",
        "goals": ["补完《长夜目录》"], "rules": "", "toolpack_hint": "sandbox",
    }
    r = result("card.from_draft", {"draft": draft, "origin": "深夜图书馆修书人", "as_draft": True})
    card = json.loads((H.user_cards_dir() / "白枢.json").read_text(encoding="utf-8")) \
        if (H.user_cards_dir() / "白枢.json").exists() else json.loads(open(r["path"], encoding="utf-8").read())
    data = card["data"]
    assert data["name"] == "白枢"
    assert data["first_mes"] == "轻一点关门。"
    assert data["character_book"]["entries"][0]["keys"] == ["长夜图书馆"]
    assert data["extensions"]["lunamoth"]["toolpack"] == "sandbox"
    assert data["extensions"]["lunamoth"]["embodiment"] == "literal"
    assert data["extensions"]["lunamoth"]["tempo"] == "normal"
    assert data["extensions"]["lunamoth"]["draft"] is True
    assert data["extensions"]["lunamoth"]["origin"] == "深夜图书馆修书人"
    listed = result("cards.list")
    mine = next(c for c in listed if c["name"] == "白枢")
    assert mine["draft"] is True and mine["builtin"] is False


def test_cards_draft_happy_path_uses_default_model(monkeypatch):
    set_defaults()
    seen = []
    mock_completion(monkeypatch, json.dumps(draft_payload()), seen)
    r = result("cards.draft", {"inspiration": "an orbital lantern keeper"})
    assert r["name"] == "Aster"
    assert r["world_entries"][0]["keys"] == ["Orbital Garden"]
    assert r["seed_goals"] == ["Map the mirror-season drift", "Welcome careful visitors"]
    assert r["theme_color"] == "#7C5CFF"
    assert r["avatar_svg"].startswith("<svg")
    assert r["embodiment"] == "literal"
    payload = seen[0]["payload"]
    assert payload["model"] == "test/model"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.75
    assert seen[0]["api_key"] == "sk-test"


def test_cards_draft_invalid_json_is_clear_error_and_no_save(monkeypatch):
    set_defaults()
    mock_completion(monkeypatch, "not json")
    err = rpc_error("cards.draft", {"inspiration": "keep this exact text"})
    assert err["code"] == -32050
    assert "strict JSON" in err["message"]
    assert err["data"]["kind"] == "draft_json"
    assert not H.user_cards_dir().exists()


def test_cards_draft_rejects_parallel_schema(monkeypatch):
    set_defaults()
    bad = draft_payload()
    bad["extra"] = "nope"
    mock_completion(monkeypatch, json.dumps(bad))
    err = rpc_error("cards.draft", {"inspiration": "extra field"})
    assert err["code"] == -32050
    assert err["data"]["kind"] == "draft_schema"
    assert "unexpected: extra" in err["data"]["detail"]


@pytest.mark.parametrize(
    "svg",
    [
        "<svg viewBox=\"0 0 64 64\"><script>alert(1)</script></svg>",
        "<svg onload=\"alert(1)\" viewBox=\"0 0 64 64\"><circle r=\"2\"/></svg>",
        "<svg viewBox=\"0 0 64 64\"><foreignObject></foreignObject></svg>",
        "<svg viewBox=\"0 0 64 64\">" + ("<circle/>" * 300) + "</svg>",
    ],
)
def test_cards_draft_svg_sanitizer_drops_unsafe_but_keeps_draft(monkeypatch, svg):
    set_defaults()
    mock_completion(monkeypatch, json.dumps(draft_payload(svg)))
    r = result("cards.draft", {"inspiration": "unsafe svg attempt"})
    assert r["name"] == "Aster"
    assert "avatar_svg" not in r
    assert r["notes"] and "avatar_svg dropped" in r["notes"][0]


def test_card_save_roundtrips_new_lunamoth_extension_fields():
    card = H.draft_to_card({
        **draft_payload(),
        "embodiment": "actor",
        "tempo": "quiet",
    }, origin_text="orbital lantern keeper")
    r = result("card.save", {"data": card})
    raw = result("card.read", {"path": r["path"]})["raw"]
    ext = raw["data"]["extensions"]["lunamoth"]
    assert ext["avatar_svg"].startswith("<svg")
    assert ext["theme_color"] == "#7C5CFF"
    assert ext["embodiment"] == "actor"
    assert ext["tempo"] == "quiet"
    assert ext["tagline"] == "A gentle keeper of orbital lanterns"
    assert ext["goals"] == ["Map the mirror-season drift", "Welcome careful visitors"]
    assert raw["data"]["character_book"]["entries"][0]["keys"] == ["Orbital Garden"]
    listed = result("cards.list")
    mine = next(c for c in listed if c["path"] == r["path"])
    assert mine["avatar_svg"].startswith("<svg")
    assert mine["theme_color"] == "#7C5CFF"
    assert mine["tagline"] == "A gentle keeper of orbital lanterns"


def test_card_studio_actor_embodiment_feeds_prompt_bridge(tmp_path, monkeypatch):
    """Studio-saved `extensions.lunamoth.embodiment=actor` is read by the prompt machine."""
    card = H.draft_to_card({
        **draft_payload(),
        "name": "BridgeCard",
        "description": "BridgeCard keeps a quiet stage ledger.",
        "embodiment": "actor",
    }, origin_text="stage ledger keeper")
    r = result("card.save", {"data": card})

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LUNAMOTH_CONFIG_DIR", str(tmp_path / "cfg"))
    from lunamoth.core import agent as agent_mod
    from lunamoth.tools import skills as skills_mod

    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(agent_mod, "SANDBOX_ROOT", sandbox)
    monkeypatch.setattr(skills_mod, "SANDBOX_ROOT", sandbox)

    from lunamoth.core.agent import LunaMothAgent
    from lunamoth.session.settings import Settings

    agent = LunaMothAgent(Settings(provider="mock", character_path=r["path"], toolpack="sandbox"))
    agent.transcript.reset()
    stable = "\n\n".join(agent._stable_prefix())
    stable_words = " ".join(stable.split())

    assert Path(r["path"]).read_text(encoding="utf-8").find('\"embodiment\": \"actor\"') >= 0
    assert agent.effective_embodiment() == "actor"
    assert "You are giving BridgeCard life" in stable
    assert "backstage of this embodiment" in stable_words
    assert stable.index("You are giving BridgeCard life") < stable.index("must be real")


def test_builtin_cards_cannot_be_deleted():
    err = rpc_error("card.delete", {"path": luna_card_path()})
    assert err["code"] == -32031


def test_referenced_card_cannot_be_deleted():
    set_defaults()
    draft = {"name": "T", "appearance": "x", "personality": "", "scenario": "",
             "first_mes": "hi", "alternate_greetings": [], "world": [],
             "relationship": "", "goals": [], "rules": "", "toolpack_hint": ""}
    r = result("card.from_draft", {"draft": draft})
    result("session.wake", {"card": r["path"]})
    err = rpc_error("card.delete", {"path": r["path"]})
    assert err["code"] == -32032


# ---- works & extras --------------------------------------------------------------

def test_works_list_reads_sandbox_tree():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    ws = meta.sandbox_dir / "workspace" / "gallery"
    ws.mkdir(parents=True)
    (ws / "aurora.html").write_text("<html>", encoding="utf-8")
    (meta.sandbox_dir / "logs").mkdir(exist_ok=True)
    (meta.sandbox_dir / "logs" / "noise.log").write_text("x", encoding="utf-8")
    works = result("works.list", {"name": entry["name"]})
    names = [w["name"] for w in works]
    assert "aurora.html" in names
    assert "noise.log" not in names  # logs are diagnostics, not works
    assert works[0]["kind"] == "web"


def test_open_path_refuses_outside_home():
    err = rpc_error("open.path", {"path": "/etc/hosts"})
    assert err["code"] in (-32040, -32041)


# ---- error classification ---------------------------------------------------------

def test_http_error_classification():
    assert H._classify_http_error(401, "")["kind"] == "auth"
    assert H._classify_http_error(402, "")["kind"] == "credit"
    assert H._classify_http_error(500, "Insufficient credits")["kind"] == "credit"
    assert H._classify_http_error(429, "")["kind"] == "ratelimit"
    assert H._classify_http_error(404, "")["kind"] == "model"


def test_board_error_kind_classifies_provider_auth_errors():
    assert H.board_error_kind("2026 ERROR llm: permanent HTTP error: HTTP 401 User not found") == "auth"
    assert H.board_error_kind("HTTP 403 forbidden") == "auth"
    assert H.board_error_kind("HTTP 404 model not found") == "model"
    assert H.board_error_kind("connection failed: timeout") == "network"


def test_session_entry_includes_auth_error_kind():
    set_defaults()
    entry = result("session.wake", {"card": luna_card_path()})
    meta = S.load_session(entry["name"])
    log = meta.sandbox_dir / "logs" / "errors.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("2026-06-12 ERROR [x] lunamoth.terminal: permanent model error: HTTP 401 User not found\n",
                   encoding="utf-8")
    row = result("sessions.list")[0]
    assert row["error_kind"] == "auth"
    assert "HTTP 401" in row["error"]


def test_unknown_method_is_a_clean_rpc_error():
    err = rpc_error("nope.nothing")
    assert err["code"] == -32601
