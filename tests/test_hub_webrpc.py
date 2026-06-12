"""Web-facing hub RPC batch: works.read, messaging.get/save (masked secrets),
card.avatar_draft, weixin.qr / weixin.qr_status (server/hub.py).

Everything runs against a temp LUNAMOTH_HOME; no network (provider and iLink
HTTP calls are monkeypatched)."""
import json
import os

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
    return d.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def result(method, params=None):
    resp = dispatch(method, params)
    assert "error" not in resp, resp.get("error")
    return resp["result"]


def rpc_error(method, params=None):
    resp = dispatch(method, params)
    assert "error" in resp, resp
    return resp["error"]


def set_defaults():
    H.save_defaults({"provider": "openrouter", "base_url": "https://example.invalid/v1",
                     "api_key": "sk-test", "model": "test/model"})


def wake_session():
    set_defaults()
    card = str(H.bundled_cards_dir() / "Quinn.zh.json")
    entry = result("session.wake", {"card": card})
    return S.load_session(entry["name"])


# ---- works.read -------------------------------------------------------------------

def test_works_read_text_image_binary_and_caps():
    meta = wake_session()
    ws = meta.sandbox_dir / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "note.md").write_text("# hello 月蛾", encoding="utf-8")
    (ws / "dot.png").write_bytes(b"\x89PNG\r\n\x1a\nxxxx")
    (ws / "big.txt").write_text("x" * (H._WORK_READ_CAP + 10), encoding="utf-8")
    (ws / "blob.dat").write_bytes(b"\x00\x01\x02")

    text = result("works.read", {"name": meta.name, "rel": "workspace/note.md"})
    assert text["kind"] == "text" and "月蛾" in text["content"] and text["truncated"] is False

    img = result("works.read", {"name": meta.name, "rel": "workspace/dot.png"})
    assert img["kind"] == "image" and img["data_uri"].startswith("data:image/png;base64,")

    big = result("works.read", {"name": meta.name, "rel": "workspace/big.txt"})
    assert big["truncated"] is True and len(big["content"]) == H._WORK_READ_CAP

    blob = result("works.read", {"name": meta.name, "rel": "workspace/blob.dat"})
    assert blob["kind"] == "binary" and "content" not in blob and "data_uri" not in blob


def test_works_read_refuses_traversal_and_outside_trees():
    meta = wake_session()
    (meta.root / "config.json").exists()  # the prize a traversal would want
    for rel in ("../config.json", "workspace/../../config.json", "logs/audit.jsonl", ""):
        err = rpc_error("works.read", {"name": meta.name, "rel": rel})
        assert err["code"] in (-32031, -32602), rel
    err = rpc_error("works.read", {"name": meta.name, "rel": "workspace/missing.txt"})
    assert err["code"] == -32035


# ---- messaging.get / messaging.save -----------------------------------------------

def test_messaging_save_masks_and_roundtrips_secrets():
    meta = wake_session()
    cfg = {"enabled": True,
           "adapters": {"weixin": {"base_url": "https://ilink.example/v1", "bot_token": "tok-123"},
                        "wecom": {"corp_secret": "ssh", "agent_id": "7"}}}
    saved = result("messaging.save", {"name": meta.name, "config": cfg})
    assert saved["config"]["adapters"]["weixin"]["bot_token"] == H._SECRET_MASK
    assert saved["config"]["adapters"]["wecom"]["corp_secret"] == H._SECRET_MASK
    assert saved["config"]["adapters"]["weixin"]["base_url"] == "https://ilink.example/v1"

    # On disk: real secrets, 0600.
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["adapters"]["weixin"]["bot_token"] == "tok-123"
    assert (os.stat(meta.root / "messaging.json").st_mode & 0o777) == 0o600

    # Round-trip: the UI sends the mask back unchanged -> original preserved,
    # edited fields land.
    got = result("messaging.get", {"name": meta.name})["config"]
    got["adapters"]["weixin"]["base_url"] = "https://ilink.example/v2"
    result("messaging.save", {"name": meta.name, "config": got})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["adapters"]["weixin"]["bot_token"] == "tok-123"
    assert on_disk["adapters"]["weixin"]["base_url"] == "https://ilink.example/v2"
    assert on_disk["enabled"] is True  # not dropped by a form that omits it? (it sent it)


def test_messaging_save_mask_without_original_is_an_error():
    meta = wake_session()
    err = rpc_error("messaging.save", {"name": meta.name, "config": {
        "adapters": {"weixin": {"bot_token": H._SECRET_MASK}}}})
    assert err["code"] == -32602


def test_messaging_save_preserves_enabled_when_omitted():
    meta = wake_session()
    result("messaging.save", {"name": meta.name, "config": {"enabled": True, "adapters": {}}})
    result("messaging.save", {"name": meta.name, "config": {"adapters": {"qq": {"ws_url": "ws://x"}}}})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["enabled"] is True


def test_messaging_save_merges_per_the_web_form_contract():
    """The deck form sends only the platform on screen and omits unchanged
    secrets — the backend must merge, never replace (webui-needs #7)."""
    meta = wake_session()
    result("messaging.save", {"name": meta.name, "config": {
        "enabled": True,
        "adapters": {"weixin": {"base_url": "https://ilink.example/v1", "bot_token": "tok-123"}}}})
    # Saving the wecom tab must not drop the weixin adapter.
    result("messaging.save", {"name": meta.name, "config": {
        "enabled": True, "allowed_senders": ["alice"],
        "adapters": {"wecom": {"corp_id": "c1", "corp_secret": "s1"}}}})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["adapters"]["weixin"]["bot_token"] == "tok-123"
    assert on_disk["adapters"]["wecom"]["corp_id"] == "c1"
    assert on_disk["allowed_senders"] == ["alice"]
    # Editing one weixin field with the secret omitted keeps the secret.
    result("messaging.save", {"name": meta.name, "config": {
        "adapters": {"weixin": {"base_url": "https://ilink.example/v2"}}}})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["adapters"]["weixin"]["bot_token"] == "tok-123"
    assert on_disk["adapters"]["weixin"]["base_url"] == "https://ilink.example/v2"
    # Explicit null deletes a field / a platform.
    result("messaging.save", {"name": meta.name, "config": {
        "adapters": {"weixin": {"bot_token": None}, "wecom": None}}})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert "bot_token" not in on_disk["adapters"]["weixin"]
    assert "wecom" not in on_disk["adapters"]


# ---- card.avatar_draft -------------------------------------------------------------

GOOD_SVG = '<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="20" fill="#7C5CFF"/></svg>'


def test_avatar_draft_keeps_safe_candidates_and_notes_dropped(monkeypatch):
    set_defaults()
    canned = {"candidates": [
        {"avatar_svg": GOOD_SVG, "theme_color": "#7c5cff"},
        {"avatar_svg": '<svg viewBox="0 0 64 64"><script>x</script></svg>', "theme_color": "#000000"},
        {"avatar_svg": GOOD_SVG.replace("circle", "rect"), "theme_color": "nope"},
    ]}
    monkeypatch.setattr(H, "_complete", lambda *a, **k: json.dumps(canned))
    out = result("card.avatar_draft", {"description": "a quiet blue moth"})
    assert len(out["candidates"]) == 2
    assert out["candidates"][0]["theme_color"] == "#7C5CFF"
    assert out["candidates"][1]["theme_color"] == ""  # invalid color cleaned, svg kept
    assert any("script" in n for n in out["notes"])


def test_avatar_draft_all_dropped_is_a_visible_error(monkeypatch):
    set_defaults()
    canned = {"candidates": [{"avatar_svg": "<div>nope</div>", "theme_color": "#123456"}]}
    monkeypatch.setattr(H, "_complete", lambda *a, **k: json.dumps(canned))
    err = rpc_error("card.avatar_draft", {"description": "x"})
    assert err["code"] == -32050


def test_avatar_draft_needs_input():
    set_defaults()
    assert rpc_error("card.avatar_draft", {})["code"] == -32602


def test_avatar_draft_reads_card_summary(monkeypatch):
    set_defaults()
    seen = {}

    def fake_complete(defaults, system, user, **kw):
        seen["user"] = user
        return json.dumps({"candidates": [{"avatar_svg": GOOD_SVG, "theme_color": "#7C5CFF"}]})

    monkeypatch.setattr(H, "_complete", fake_complete)
    card = str(H.bundled_cards_dir() / "Quinn.en.json")
    out = result("card.avatar_draft", {"card_path": card, "description": "rounder"})
    assert out["candidates"]
    assert "Name: Quinn" in seen["user"] and "rounder" in seen["user"]


# ---- weixin.qr / weixin.qr_status --------------------------------------------------

class FakeWeixinAPI:
    last = None

    def __init__(self, *, base_url="", **kw):
        self.base_url = base_url
        FakeWeixinAPI.last = self

    def get_bot_qrcode(self, bot_type):
        self.bot_type = bot_type
        return {"qrcode": "QR-VALUE", "qrcode_img_content": "aWNvbg=="}

    def get_qrcode_status(self, qrcode_value, *, timeout_ms):
        self.polled = (qrcode_value, timeout_ms)
        return self.status_response


def test_weixin_qr_returns_value_img_and_fallback(monkeypatch):
    meta = wake_session()
    import lunamoth.messaging.weixin as W
    monkeypatch.setattr(W, "WeixinAPI", FakeWeixinAPI)
    out = result("weixin.qr", {"name": meta.name})
    assert out["qrcode"] == "QR-VALUE" and out["img"] == "aWNvbg=="
    assert "QR-VALUE" in out["fallback_url"]
    assert FakeWeixinAPI.last.bot_type  # a bot_type was always passed


def test_weixin_qr_status_confirmed_persists_login_state(monkeypatch):
    meta = wake_session()
    import lunamoth.messaging.weixin as W
    monkeypatch.setattr(W, "WeixinAPI", FakeWeixinAPI)
    FakeWeixinAPI.status_response = {"status": "confirmed", "bot_token": "bot-tok",
                                     "ilink_bot_id": "bot-1", "ilink_user_id": "u-1"}
    out = result("weixin.qr_status", {"name": meta.name, "qrcode": "QR-VALUE"})
    assert out["status"] == "confirmed" and out["account_id"] == "bot-1"
    state = json.loads((meta.root / "weixin_state.json").read_text(encoding="utf-8"))
    assert state["token"] == "bot-tok" and state["needs_relogin"] is False
    assert (os.stat(meta.root / "weixin_state.json").st_mode & 0o777) == 0o600


def test_weixin_qr_status_wait_passes_through(monkeypatch):
    meta = wake_session()
    import lunamoth.messaging.weixin as W
    monkeypatch.setattr(W, "WeixinAPI", FakeWeixinAPI)
    FakeWeixinAPI.status_response = {"status": "wait"}
    out = result("weixin.qr_status", {"name": meta.name, "qrcode": "QR-VALUE"})
    assert out == {"status": "wait"}
    assert not (meta.root / "weixin_state.json").exists()


def test_weixin_qr_status_needs_qrcode():
    meta = wake_session()
    assert rpc_error("weixin.qr_status", {"name": meta.name})["code"] == -32602


def test_works_list_visible_under_a_dot_dir_home(tmp_path, monkeypatch):
    """Production sandboxes live under ~/.lunamoth — a dot-dir ancestor must
    not hide every work (the filter judges only the path under the tree)."""
    monkeypatch.setenv("LUNAMOTH_HOME", str(tmp_path / ".lunahome"))
    meta = wake_session()
    assert ".lunahome" in str(meta.sandbox_dir)
    ws = meta.sandbox_dir / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "poem.md").write_text("moth", encoding="utf-8")
    (ws / ".hidden.md").write_text("x", encoding="utf-8")
    works = result("works.list", {"name": meta.name})
    names = [w["name"] for w in works]
    assert "poem.md" in names and ".hidden.md" not in names


# ---- card.duplicate ----------------------------------------------------------------

def test_card_duplicate_is_distinct_and_never_default():
    src = str(H.bundled_cards_dir() / "Quinn.zh.json")
    out = result("card.duplicate", {"path": src})
    dup = json.loads(open(out["path"], encoding="utf-8").read())
    assert dup["data"]["name"].endswith("（副本）")
    assert "default" not in [t.lower() for t in dup["data"]["tags"]]
    # original untouched
    orig = json.loads(open(src, encoding="utf-8").read())
    assert "default" in orig["data"]["tags"] and not orig["data"]["name"].endswith("（副本）")
    # an English card gets the English suffix
    out2 = result("card.duplicate", {"path": str(H.bundled_cards_dir() / "Quinn.en.json")})
    dup2 = json.loads(open(out2["path"], encoding="utf-8").read())
    assert dup2["data"]["name"].endswith(" (copy)")


def test_card_duplicate_missing_is_an_error():
    assert rpc_error("card.duplicate", {"path": "/nope/missing.json"})["code"] == -32035
