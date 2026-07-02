"""Web-facing hub RPC batch: works.read, messaging.get/save (masked secrets),
card.avatar_generate/upload/read, weixin.qr / weixin.qr_status (server/hub.py).

Everything runs against a temp LUNAMOTH_HOME; no network (provider and iLink
HTTP calls are monkeypatched)."""
import json
import os
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
    # The keyring is the ONE key store: seed a default provider key + activate it (the
    # product's provider→key→model path), not a legacy top-level api_key.
    H.save_key("default", provider="openrouter", base_url="https://example.invalid/v1",
               api_key="sk-test", model="test/model")
    H.use_key("default")


def wake_session():
    set_defaults()
    card = str(H.bundled_cards_dir() / "Quinn" / "card.json")
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
                        "qq": {"access_token": "ssh", "self_id": "7"}}}
    saved = result("messaging.save", {"name": meta.name, "config": cfg})
    assert saved["config"]["adapters"]["weixin"]["bot_token"] == H._SECRET_MASK
    assert saved["config"]["adapters"]["qq"]["access_token"] == H._SECRET_MASK
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


def test_messaging_save_derives_top_level_enabled_from_platforms():
    # The top-level `enabled` (= is the host on at all) is DERIVED, not an
    # independent flag: a save claiming enabled:true while turning its only
    # platform OFF must land enabled:false — and vice-versa — so the kill-switch
    # the supervisor reads can't drift from what make_adapters actually runs.
    meta = wake_session()
    result("messaging.save", {"name": meta.name, "config": {
        "enabled": True, "adapters": {"qq": {"url": "ws://x", "enabled": False}}}})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["enabled"] is False  # no platform effectively on → host off
    result("messaging.save", {"name": meta.name, "config": {
        "enabled": False, "adapters": {"qq": {"enabled": True}}}})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["enabled"] is True   # a platform on → host on, despite enabled:false


def test_messaging_save_merges_per_the_web_form_contract():
    """The deck form sends only the platform on screen and omits unchanged
    secrets — the backend must merge, never replace (webui-needs #7)."""
    meta = wake_session()
    result("messaging.save", {"name": meta.name, "config": {
        "enabled": True,
        "adapters": {"weixin": {"base_url": "https://ilink.example/v1", "bot_token": "tok-123"}}}})
    # Saving the qq tab must not drop the weixin adapter.
    result("messaging.save", {"name": meta.name, "config": {
        "enabled": True, "allowed_senders": ["alice"],
        "adapters": {"qq": {"ws_url": "ws://c1", "access_token": "s1"}}}})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["adapters"]["weixin"]["bot_token"] == "tok-123"
    assert on_disk["adapters"]["qq"]["ws_url"] == "ws://c1"
    assert on_disk["allowed_senders"] == ["alice"]
    # Editing one weixin field with the secret omitted keeps the secret.
    result("messaging.save", {"name": meta.name, "config": {
        "adapters": {"weixin": {"base_url": "https://ilink.example/v2"}}}})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert on_disk["adapters"]["weixin"]["bot_token"] == "tok-123"
    assert on_disk["adapters"]["weixin"]["base_url"] == "https://ilink.example/v2"
    # Explicit null deletes a field / a platform.
    result("messaging.save", {"name": meta.name, "config": {
        "adapters": {"weixin": {"bot_token": None}, "qq": None}}})
    on_disk = json.loads((meta.root / "messaging.json").read_text(encoding="utf-8"))
    assert "bot_token" not in on_disk["adapters"]["weixin"]
    assert "qq" not in on_disk["adapters"]


# ---- avatar: generate / upload / read ---------------------------------------------

GOOD_SVG = '<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="20" fill="#7C5CFF"/></svg>'
# A 1x1 transparent PNG (valid magic bytes).
PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
           b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _b64(data):
    import base64
    return base64.b64encode(data).decode("ascii")


def _make_user_card(name="Tester"):
    """A minimal editable JSON card in the user deck; returns its path."""
    out = result("card.save", {"data": {"spec": "chara_card_v3", "spec_version": "3.0",
                                         "data": {"name": name, "description": "d"}}})
    return out["path"]


def test_avatar_upload_svg_writes_sidecar_and_points_card():
    set_defaults()
    path = _make_user_card()
    out = result("card.avatar_upload", {"path": path, "data_b64": _b64(GOOD_SVG.encode()), "ext": "svg"})
    assert ".avatar." in out["avatar_file"] and out["avatar_file"].endswith(".svg")
    sidecar = os.path.join(os.path.dirname(path), out["avatar_file"])
    assert os.path.isfile(sidecar)
    raw = json.loads(open(path, encoding="utf-8").read())
    lm = raw["data"]["extensions"]["lunamoth"]
    assert lm["avatar_file"] == out["avatar_file"]
    assert lm["assets"]["avatar"] == out["avatar_file"]          # also the gallery selection
    assert out["avatar_file"] in lm["assets"]["options"]["avatar"]
    assert "avatar_svg" not in lm           # inline fallback dropped once a sidecar exists
    # avatar_read resolves the sidecar to a data-URI.
    read = result("card.avatar_read", {"path": path})
    assert read["data_uri"].startswith("data:image/svg+xml")


def test_avatar_upload_png_validates_magic_and_stores_as_is():
    set_defaults()
    path = _make_user_card("PngCard")
    out = result("card.avatar_upload", {"path": path, "data_b64": _b64(PNG_1PX), "ext": "png"})
    assert ".avatar." in out["avatar_file"] and out["avatar_file"].endswith(".png")
    read = result("card.avatar_read", {"path": path})
    assert read["data_uri"].startswith("data:image/png;base64,")
    # second upload APPENDS a candidate (non-destructive gallery) + selects the newest
    out2 = result("card.avatar_upload", {"path": path, "data_b64": _b64(PNG_1PX), "ext": "png"})
    assert out2["avatar_file"] != out["avatar_file"]
    lm = json.loads(open(path, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    assert lm["avatar_file"] == out2["avatar_file"]
    assert lm["assets"]["options"]["avatar"] == [out["avatar_file"], out2["avatar_file"]]
    # select the first back via the generic gallery RPC → avatar_file follows
    result("card.asset_select", {"path": path, "kind": "avatar", "name": out["avatar_file"]})
    lm = json.loads(open(path, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    assert lm["avatar_file"] == out["avatar_file"] and lm["assets"]["avatar"] == out["avatar_file"]
    # remove it → falls back to the other; avatar_file follows; delete clears all
    result("card.asset_remove", {"path": path, "kind": "avatar", "name": out["avatar_file"]})
    lm = json.loads(open(path, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    assert lm["avatar_file"] == out2["avatar_file"]
    result("card.asset_delete", {"path": path, "kind": "avatar"})
    lm = json.loads(open(path, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    assert "avatar_file" not in lm and "avatar" not in lm.get("assets", {})


def test_avatar_upload_rejects_unsafe_svg():
    set_defaults()
    path = _make_user_card("BadSvg")
    bad = '<svg viewBox="0 0 64 64"><script>alert(1)</script></svg>'
    err = rpc_error("card.avatar_upload", {"path": path, "data_b64": _b64(bad.encode()), "ext": "svg"})
    assert err["code"] == -32050


def test_avatar_upload_rejects_bad_type_and_oversize():
    set_defaults()
    path = _make_user_card("Limits")
    assert rpc_error("card.avatar_upload",
                     {"path": path, "data_b64": _b64(b"GIF89a"), "ext": "gif"})["code"] == -32602
    big = _b64(b"\x89PNG\r\n\x1a\n" + b"x" * (1024 * 1024 + 10))
    assert rpc_error("card.avatar_upload", {"path": path, "data_b64": big, "ext": "png"})["code"] == -32602


def test_avatar_upload_png_magic_mismatch_is_an_error():
    set_defaults()
    path = _make_user_card("FakePng")
    err = rpc_error("card.avatar_upload",
                    {"path": path, "data_b64": _b64(b"not a png at all"), "ext": "png"})
    assert err["code"] == -32602


def test_avatar_upload_refuses_builtin_card():
    set_defaults()
    builtin = str(H.bundled_cards_dir() / "Quinn" / "card.json")
    err = rpc_error("card.avatar_upload",
                    {"path": builtin, "data_b64": _b64(GOOD_SVG.encode()), "ext": "svg"})
    assert err["code"] == -32031


def test_avatar_read_falls_back_to_inline_svg():
    set_defaults()
    # A user card that declares an inline avatar_svg but no sidecar.
    out = result("card.save", {"data": {"spec": "chara_card_v3", "spec_version": "3.0",
        "data": {"name": "Inline", "extensions": {"lunamoth": {"avatar_svg": GOOD_SVG}}}}})
    read = result("card.avatar_read", {"path": out["path"]})
    assert read["data_uri"].startswith("data:image/svg+xml")


# ---- inline-avatar thumbnail (board payload shrink) + upload compression ----------

def _big_png(px=512):
    """A non-trivial PNG that compresses (gradient-ish), base64-encoded."""
    import base64
    import io

    from PIL import Image
    im = Image.new("RGBA", (px, px))
    im.putdata([((x * 7) % 256, (y * 5) % 256, (x + y) % 256, 255)
                for y in range(px) for x in range(px)])
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_avatar_upload_keeps_raw_aspect_non_destructive():
    # NON-DESTRUCTIVE: a rectangular avatar is stored with its aspect intact (squaring
    # happens at the DISPLAY layer via object-fit:cover), so the original is never baked.
    import base64 as _b
    import io
    from PIL import Image
    set_defaults()
    path = _make_user_card("Sq")
    rect = Image.new("RGB", (160, 60), (200, 30, 30))
    buf = io.BytesIO(); rect.save(buf, "PNG")
    out = result("card.avatar_upload", {"path": path, "data_b64": _b.b64encode(buf.getvalue()).decode(), "ext": "png"})
    assert ".avatar." in out["avatar_file"] and out["avatar_file"].endswith(".png")
    uri = result("card.avatar_read", {"path": path})["data_uri"]
    im = Image.open(io.BytesIO(_b.b64decode(uri.split(",", 1)[1])))
    assert im.size[0] != im.size[1]  # raw aspect preserved (not cropped to a square)


def test_list_cards_inline_avatar_is_small_thumbnail():
    """The board list embeds a TINY webp thumbnail, not the full sidecar — the
    full-res avatar still rides /asset & avatar_read."""
    import base64

    set_defaults()
    path = _make_user_card("ThumbCard")
    png_b64 = _big_png(512)
    result("card.avatar_upload", {"path": path, "data_b64": png_b64, "ext": "png"})
    cards = result("cards.list") if False else H.list_cards()
    entry = next(c for c in cards if c["path"] == path)
    uri = entry["avatar_uri"]
    assert uri.startswith("data:image/webp;base64,")
    inline_bytes = len(base64.b64decode(uri.split(",", 1)[1]))
    assert inline_bytes < 30_000          # tiny inline avatar
    # The full-res read is unchanged (still PNG, much larger than the thumb).
    full = result("card.avatar_read", {"path": path})["data_uri"]
    assert full.startswith("data:image/png;base64,")


def test_list_cards_skips_asset_sidecars():
    # avatar/sticker/sprite sidecars sit beside the card; the deck scan must NOT try to
    # load them as character cards (each would spam an "unreadable card" traceback).
    set_defaults()
    path = _make_user_card("Sidecarred")
    p = Path(path)
    p.with_name(p.stem + ".avatar.png").write_bytes(_PNG_1PX)
    p.with_name(p.stem + ".keyvisual.png").write_bytes(_PNG_1PX)
    for i in range(9):
        p.with_name(p.stem + f".sticker.{i}.png").write_bytes(_PNG_1PX)
    paths = [c["path"] for c in H.list_cards()]
    assert path in paths
    assert not any(".avatar." in pp or ".sticker." in pp or ".keyvisual." in pp for pp in paths)


def test_asset_save_compresses_large_upload():
    """A large uploaded sprite is re-compressed on save (cap + webp q82),
    without breaking the magic-byte validation."""
    set_defaults()
    path = _make_user_card("SpriteCard")
    import base64
    import io

    from PIL import Image
    im = Image.new("RGB", (3000, 1500))
    im.putdata([((x) % 256, (y) % 256, (x * y) % 256)
                for y in range(1500) for x in range(3000)])
    buf = io.BytesIO()
    im.save(buf, format="WEBP", quality=98)
    raw = buf.getvalue()
    out = result("card.asset_save", {"path": path, "kind": "sprite",
                                      "data_b64": base64.b64encode(raw).decode("ascii"),
                                      "ext": "webp"})
    sidecar = Path(os.path.dirname(path)) / out["file"]
    assert sidecar.stat().st_size < len(raw)   # shrank on save
    with Image.open(sidecar) as got:
        assert max(got.size) <= H.CAP_ART      # capped long side


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


def test_weixin_qr_encodes_the_scan_content_not_the_polling_token(monkeypatch):
    """The QR must encode qrcode_img_content (what the phone scans), while
    `qrcode` is only the polling token for qr_status. Encoding the polling
    token was the bug that made the QR 'scan to nothing'."""
    meta = wake_session()
    import lunamoth.messaging.weixin as W
    monkeypatch.setattr(W, "WeixinAPI", FakeWeixinAPI)
    out = result("weixin.qr", {"name": meta.name})
    assert out["qrcode"] == "QR-VALUE"            # polling token, used by qr_status
    assert out["scan_content"] == "aWNvbg==" and out["img"] == "aWNvbg=="  # scannable content
    assert "aWNvbg" in out["fallback_url"] and "QR-VALUE" not in out["fallback_url"]
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
    works_dir = meta.sandbox_dir / "workspace" / "works"
    works_dir.mkdir(parents=True, exist_ok=True)
    (works_dir / "poem.md").write_text("moth", encoding="utf-8")
    (works_dir / ".hidden.md").write_text("x", encoding="utf-8")
    works = result("works.list", {"name": meta.name})
    names = [w["name"] for w in works]
    assert "poem.md" in names and ".hidden.md" not in names


# ---- card.duplicate ----------------------------------------------------------------

def test_card_duplicate_is_distinct_and_never_default():
    # zh source (archived as an easter egg) → Chinese suffix; en source → English suffix
    src = str(H.bundled_cards_dir().parent / "archive" / "cards-zh" / "Quinn.card.zh.json")
    out = result("card.duplicate", {"path": src})
    dup = json.loads(open(out["path"], encoding="utf-8").read())
    assert dup["data"]["name"].endswith("（副本）")
    assert "default" not in [t.lower() for t in dup["data"]["tags"]]
    # original untouched
    orig = json.loads(open(src, encoding="utf-8").read())
    assert "default" in orig["data"]["tags"] and not orig["data"]["name"].endswith("（副本）")
    # an English card gets the English suffix
    out2 = result("card.duplicate", {"path": str(H.bundled_cards_dir() / "Quinn" / "card.json")})
    dup2 = json.loads(open(out2["path"], encoding="utf-8").read())
    assert dup2["data"]["name"].endswith(" (copy)")


def test_card_duplicate_missing_is_an_error():
    assert rpc_error("card.duplicate", {"path": "/nope/missing.json"})["code"] == -32035


def test_card_duplicate_copies_art_assets():
    # A card is a folder (card.json + sidecar art); duplicating must bring the
    # art across so the copy isn't pointing at the original's files (or broken).
    src_dir = H.user_cards_dir() / "Arty"
    src_dir.mkdir(parents=True)
    (src_dir / "Arty.avatar.png").write_bytes(b"PNG-AVATAR")
    (src_dir / "Arty.sprite.png").write_bytes(b"PNG-SPRITE")
    card = {
        "spec": "chara_card_v3", "spec_version": "3.0", "version": "1.0",
        "data": {
            "name": "Arty", "description": "an artist",
            "extensions": {"lunamoth": {
                "avatar_file": "Arty.avatar.png",
                "assets": {"sprite": "Arty.sprite.png"},
            }},
        },
    }
    (src_dir / "card.json").write_text(json.dumps(card), encoding="utf-8")

    out = result("card.duplicate", {"path": str(src_dir / "card.json")})
    dup_path = Path(out["path"])
    dup = json.loads(dup_path.read_text(encoding="utf-8"))
    lm = dup["data"]["extensions"]["lunamoth"]
    # the copy declares the same relative names AND the files exist beside it
    assert lm["avatar_file"] == "Arty.avatar.png"
    assert lm["assets"]["sprite"] == "Arty.sprite.png"
    assert (dup_path.parent / "Arty.avatar.png").read_bytes() == b"PNG-AVATAR"
    assert (dup_path.parent / "Arty.sprite.png").read_bytes() == b"PNG-SPRITE"
    # the copy lives in its OWN folder, not the source's
    assert dup_path.parent != src_dir


# ---- named keys (webui-needs #10) --------------------------------------------------

def test_keys_roundtrip_never_echoes_secrets():
    set_defaults()
    keys = result("keys.save", {"label": "work", "provider": "openrouter",
                                "base_url": "https://or.example/v1", "api_key": "sk-work-1"})
    work = next(k for k in keys if k["label"] == "work")
    assert work == {"label": "work", "provider": "openrouter", "base_url": "https://or.example/v1",
                    "model": "", "has_key": True, "active": False}
    # update without api_key keeps the stored secret
    keys = result("keys.save", {"label": "work", "model": "deepseek/v4"})
    work = next(k for k in keys if k["label"] == "work")
    assert work["model"] == "deepseek/v4" and work["has_key"] is True
    raw = json.loads(H.desktop_config_path().read_text(encoding="utf-8"))
    assert raw["keys"]["work"]["api_key"] == "sk-work-1"
    # a new label without api_key is a visible error
    assert rpc_error("keys.save", {"label": "empty"})["code"] == -32602
    # defaults.set must NOT wipe the keys store
    result("defaults.set", {"model": "other/model"})
    raw = json.loads(H.desktop_config_path().read_text(encoding="utf-8"))
    assert raw["keys"]["work"]["api_key"] == "sk-work-1"


def test_key_test_by_label_resolves_secret_and_validates_via_models(monkeypatch):
    # The 提供商 pane's per-row test: key.test with a `label` resolves THAT saved
    # key's secret server-side (the client never holds it) and — since a key row
    # stores no model — validates via the GET /models endpoint.
    set_defaults()
    H.save_key("vol", provider="volcano",
               base_url="https://ark.example/v3", api_key="ark-secret-xyz")
    calls: dict = {}

    def fake_http_json(url, api_key="", payload=None, timeout=30.0):
        calls.update(url=url, api_key=api_key, payload=payload)
        return {"data": [{"id": "doubao-x"}]}

    monkeypatch.setattr(H, "_http_json", fake_http_json)
    out = result("key.test", {"label": "vol"})
    assert out["ok"] is True
    assert calls["url"] == "https://ark.example/v3/models"   # no model → /models check
    assert calls["payload"] is None                          # a GET, not a completion
    assert calls["api_key"] == "ark-secret-xyz"              # stored secret resolved server-side


def test_key_test_unknown_label_is_a_clean_error():
    set_defaults()
    out = result("key.test", {"label": "nope"})
    assert out["ok"] is False and out["error"]["kind"] == "config"


def test_image_selection_and_key_unified_on_keyring():
    # The image SELECTION (provider + model) lives in the global defaults; the KEY
    # comes from the SAME provider keyring as text (unified path). has_image_key =
    # the ACTIVE image provider has a key. There is no legacy image_api_key field.
    set_defaults()
    H.save_key("火山", provider="volcano",
               base_url="https://ark.cn-beijing.volces.com/api/v3", api_key="ark-secret-1")
    pub = result("defaults.set", {"image_provider": "volcano", "image_model": "doubao-seedream-x"})
    assert pub["image_provider"] == "volcano"
    assert pub["image_model"] == "doubao-seedream-x"
    assert pub["has_image_key"] is True            # active provider has a keyring key
    assert "image_api_key" not in pub              # no legacy secret field at all
    # persisted to desktop.json (where _image_gen.py reads it)
    raw = json.loads(H.desktop_config_path().read_text(encoding="utf-8"))
    assert raw["image_provider"] == "volcano"
    assert raw["image_model"] == "doubao-seedream-x"
    assert "image_api_key" not in raw              # never written
    got = result("defaults.get")
    assert got["has_image_key"] is True and "image_api_key" not in got
    assert got["image_model"] == "doubao-seedream-x"
    # selecting a provider with NO key → has_image_key False (plain, no fallback)
    result("defaults.set", {"image_provider": "openai", "image_model": "gpt-image-1"})
    assert result("defaults.get")["has_image_key"] is False


def _await_visual(job_id, tries=300):
    """Poll card.visual_job until it leaves 'running' (generation is async now)."""
    import time
    for _ in range(tries):
        st = result("card.visual_job", {"job_id": job_id})
        if st["status"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError("visual job never finished")


def _await_visual_error(job_id, tries=300):
    """Poll card.visual_job, expecting the job to surface a structured RPC error."""
    import time
    for _ in range(tries):
        resp = dispatch("card.visual_job", {"job_id": job_id})
        if "error" in resp:
            return resp["error"]
        if resp["result"]["status"] != "running":
            raise AssertionError(f"expected error, got {resp['result']}")
        time.sleep(0.01)
    raise AssertionError("visual job never finished")


def test_card_visual_generate_autosaves(monkeypatch):
    # Async + AUTO-SAVE: the job generates AND writes the result to the card (so it
    # survives the client leaving); the poll is only for progress. Needs a WRITABLE
    # user-deck card (auto-save refuses bundled/builtin).
    card, _b64 = _user_card_copy(None)
    H.save_key("火山", provider="volcano",
               base_url="https://ark.cn-beijing.volces.com/api/v3", api_key="sk-img-test")
    H.save_defaults({"image_provider": "volcano", "image_model": "doubao-seedream-x"})
    monkeypatch.setattr(H, "_complete",
                        lambda *a, **k: '{"appearance":"a","palette":"p","world":"w","theme":"#1a2"}')
    from lunamoth.tools.builtin import _image_gen
    seen = {}

    def fake_ark(prompt, size, refs=None):
        seen["refs"] = refs
        return ["http://x/a.png"]

    monkeypatch.setattr(_image_gen, "ark_generate", fake_ark)
    monkeypatch.setattr(_image_gen, "download_bytes", lambda url: b"\x89PNG\r\n\x1a\nFAKE")
    sub = result("card.visual_generate", {"path": card, "kind": "avatar",
                                          "refs": ["data:image/png;base64,AAAA"]})
    assert sub["status"] == "running" and sub["job_id"]
    out = _await_visual(sub["job_id"])
    assert out["status"] == "ready" and out["saved"] is True and out["kind"] == "avatar"
    assert out["data_uri"].startswith("data:image/png;base64,")
    assert seen["refs"] == ["data:image/png;base64,AAAA"]  # user refs reach the generator
    # the card now points at a saved avatar sidecar, and the brief was PERSISTED
    raw = json.loads(open(card, encoding="utf-8").read())
    lm = raw["data"]["extensions"]["lunamoth"]
    assert ".avatar." in lm["avatar_file"] and lm["avatar_file"].endswith(".png")
    assert lm["assets"]["avatar"] == lm["avatar_file"]  # avatar is a gallery kind now
    assert "visual_brief" in lm

    # visual_brief now returns the STORED brief — no LLM re-pay (would throw if it built)
    monkeypatch.setattr(H, "_complete", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-briefed")))
    b = result("card.visual_brief", {"path": card})
    assert b["stored"] is True and b["brief"]["appearance"] == "a"

    # unknown kind is a clean param error (validated synchronously, before submit)
    assert rpc_error("card.visual_generate", {"path": card, "kind": "nope"})["code"] == -32602
    # no image key → the JOB fails; the poll surfaces a visible -32050 (no fake image)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("LUNAMOTH_HOME", str(os.path.join(os.environ["LUNAMOTH_HOME"], "no-img")))
    sub3 = result("card.visual_generate", {"path": card, "kind": "sprite", "brief": {"appearance": "x"}})
    assert _await_visual_error(sub3["job_id"])["code"] == -32050


_PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _user_card_copy(monkeypatch):
    """A writable user-deck JSON card (asset_save refuses bundled/builtin cards)."""
    import base64 as _b64
    set_defaults()
    src = H.bundled_cards_dir() / "Quinn" / "card.json"
    dst_dir = H.user_cards_dir() / "TestOC"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "card.json"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return str(dst), _b64


def test_card_asset_save_and_delete_sprite():
    import base64 as b64m
    set_defaults()
    card = _make_user_card("SpriteCard")  # clean card, no pre-existing assets
    b64 = b64m.b64encode(_PNG_1PX).decode("ascii")
    out = result("card.asset_save", {"path": card, "kind": "sprite", "data_b64": b64, "ext": "png"})
    assert out["kind"] == "sprite" and ".sprite." in out["file"] and out["file"].endswith(".png")
    assert out["url"].startswith("/asset?")
    # the card points at it, and it's in the candidate gallery
    raw = json.loads(open(card, encoding="utf-8").read())
    lm = raw["data"]["extensions"]["lunamoth"]
    assert lm["assets"]["sprite"] == out["file"]
    assert lm["assets"]["options"]["sprite"] == [out["file"]]
    assert (Path(card).with_name(out["file"])).is_file()
    # bad kind / bad ext / non-image body are clean param errors
    assert rpc_error("card.asset_save", {"path": card, "kind": "nope", "data_b64": b64, "ext": "png"})["code"] == -32602
    assert rpc_error("card.asset_save", {"path": card, "kind": "sprite", "data_b64": b64, "ext": "gif"})["code"] == -32602
    bad = b64m.b64encode(b"<html>nope</html>").decode("ascii")
    assert rpc_error("card.asset_save", {"path": card, "kind": "sprite", "data_b64": bad, "ext": "png"})["code"] == -32602
    # delete removes EVERY candidate + the pointer + the options list (idempotent)
    out = result("card.asset_delete", {"path": card, "kind": "sprite"})
    assert out["removed"] is True
    raw = json.loads(open(card, encoding="utf-8").read())
    assert "sprite" not in raw["data"]["extensions"]["lunamoth"].get("assets", {})
    assert "sprite" not in (raw["data"]["extensions"]["lunamoth"]["assets"].get("options") or {})
    assert result("card.asset_delete", {"path": card, "kind": "sprite"})["removed"] is False


def test_card_asset_gallery_caps_oldest_nonselected():
    # The candidate gallery is non-destructive but BOUNDED — repeated generation must not
    # grow options[kind] (each a multi-MB sidecar) without limit. Past the cap, the oldest
    # non-selected candidate (file + entry) is evicted; the selected one is always kept.
    import base64 as b64m
    from lunamoth.server.hub import avatars as A
    set_defaults()
    card = _make_user_card("CapCard")
    b64 = b64m.b64encode(_PNG_1PX).decode("ascii")
    cap = A._GALLERY_OPTIONS_MAX
    names: list[str] = []
    out = None
    for _ in range(cap + 4):
        out = result("card.asset_save", {"path": card, "kind": "sprite", "data_b64": b64, "ext": "png"})
        names.append(out["file"])
    # the gallery is bounded at the cap, and the newest is still selected
    lm = json.loads(open(card, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    opts = lm["assets"]["options"]["sprite"]
    assert len(opts) == cap
    assert lm["assets"]["sprite"] == names[-1] and names[-1] in opts
    # the 4 oldest candidates were evicted — entry AND sidecar file gone
    for evicted in names[:4]:
        assert evicted not in opts
        assert not (Path(card).with_name(evicted)).is_file()


def test_card_asset_gallery_select_and_remove():
    # Generation/upload is NON-destructive: each save is a new candidate; select
    # repoints; remove deletes one and re-selects another.
    import base64 as b64m
    set_defaults()
    card = _make_user_card("GalleryCard")  # clean card, no pre-existing assets
    b64 = b64m.b64encode(_PNG_1PX).decode("ascii")
    a = result("card.asset_save", {"path": card, "kind": "background", "data_b64": b64, "ext": "png"})["file"]
    b = result("card.asset_save", {"path": card, "kind": "background", "data_b64": b64, "ext": "png"})["file"]
    assert a != b  # unique candidate names, no overwrite
    lm = json.loads(open(card, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    assert lm["assets"]["options"]["background"] == [a, b] and lm["assets"]["background"] == b  # newest selected
    assert (Path(card).with_name(a)).is_file() and (Path(card).with_name(b)).is_file()
    # select the older one
    result("card.asset_select", {"path": card, "kind": "background", "name": a})
    assert json.loads(open(card, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]["assets"]["background"] == a
    assert rpc_error("card.asset_select", {"path": card, "kind": "background", "name": "nope.png"})["code"] == -32602
    # remove the selected (a) → falls back to the remaining (b); file gone
    out = result("card.asset_remove", {"path": card, "kind": "background", "name": a})
    assert out["selected"] == b
    assert not (Path(card).with_name(a)).is_file()
    lm = json.loads(open(card, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    assert lm["assets"]["options"]["background"] == [b] and lm["assets"]["background"] == b
    # list_cards surfaces the gallery
    entry = next(c for c in H.list_cards() if c["path"] == card)
    assert len(entry["bg_options"]) == 1


def test_card_asset_matte_adds_cut_candidate_keeping_raw(monkeypatch):
    # 去背景 is non-destructive: it adds a NEW cut candidate and keeps the raw.
    import base64 as b64m
    import io
    from PIL import Image
    from lunamoth.visuals import matte
    monkeypatch.setattr(matte, "deps_available", lambda: False)  # keyless white-bg fallback
    set_defaults()
    card = _make_user_card("MatteCard")
    img = Image.new("RGB", (40, 40), (255, 255, 255))
    for x in range(12, 28):
        for y in range(12, 28):
            img.putpixel((x, y), (200, 30, 30))
    buf = io.BytesIO(); img.save(buf, "PNG")
    raw = result("card.asset_save", {"path": card, "kind": "sprite",
                                     "data_b64": b64m.b64encode(buf.getvalue()).decode(), "ext": "png"})["file"]
    out = result("card.asset_matte", {"path": card, "kind": "sprite"})
    assert out["selected"] != raw and out["file"].endswith(".png")
    lm = json.loads(open(card, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    assert lm["assets"]["options"]["sprite"] == [raw, out["file"]]  # raw kept + cut added
    assert lm["assets"]["sprite"] == out["file"]                     # cut is now shown


def test_card_asset_remove_refuses_untracked_name(tmp_path):
    # Defence in depth: asset_remove only ever unlinks a file the gallery tracks. A
    # stray sidecar with the same stem must survive a remove of an unknown name.
    import base64 as b64m
    set_defaults()
    card = _make_user_card("RemoveGuard")
    b64 = b64m.b64encode(_PNG_1PX).decode("ascii")
    result("card.asset_save", {"path": card, "kind": "sprite", "data_b64": b64, "ext": "png"})
    bystander = Path(card).with_name(f"{Path(card).stem}.sprite.bystander.png")
    bystander.write_bytes(_PNG_1PX)
    assert rpc_error("card.asset_remove",
                     {"path": card, "kind": "sprite", "name": bystander.name})["code"] == -32602
    assert bystander.is_file()  # never touched


def test_card_asset_matte_noop_on_transparent(monkeypatch):
    # With no matte model the keyless fallback can't re-cut an already-transparent
    # image — it returns a clear error instead of cloning a duplicate candidate.
    import base64 as b64m
    import io
    from PIL import Image
    from lunamoth.visuals import matte
    monkeypatch.setattr(matte, "deps_available", lambda: False)
    set_defaults()
    card = _make_user_card("MatteNoop")
    img = Image.new("RGBA", (24, 24), (0, 0, 0, 0))  # fully transparent
    buf = io.BytesIO(); img.save(buf, "PNG")
    result("card.asset_save", {"path": card, "kind": "sprite",
                               "data_b64": b64m.b64encode(buf.getvalue()).decode(), "ext": "png"})
    assert rpc_error("card.asset_matte", {"path": card, "kind": "sprite"})["code"] == -32050


def test_card_stickers_save_and_delete():
    # The sticker SET is stored as a LIST of individually-named sidecars. Saves APPEND.
    import base64 as b64m
    set_defaults()
    card = _make_user_card("StickerCard")  # clean card, no bundled stickers
    cells = [b64m.b64encode(_PNG_1PX).decode("ascii") for _ in range(9)]
    out = result("card.stickers_save", {"path": card, "data_b64": cells})
    assert out["kind"] == "stickers" and len(out["files"]) == 9
    assert all(f.endswith(".png") and ".sticker." in f for f in out["files"])
    assert all(u.startswith("/asset?") for u in out["urls"])
    raw = json.loads(open(card, encoding="utf-8").read())
    names = raw["data"]["extensions"]["lunamoth"]["assets"]["stickers"]
    assert isinstance(names, list) and len(names) == 9
    assert all((Path(card).with_name(n)).is_file() for n in names)
    # a second save APPENDS (not replaces) — and dedups the auto-named slugs
    out2 = result("card.stickers_save", {"path": card, "data_b64": cells[:2]})
    assert len(out2["files"]) == 11 and len(out2["added"]) == 2
    assert len(set(out2["files"])) == 11  # no filename collisions
    # a non-list payload is a clean param error
    assert rpc_error("card.stickers_save", {"path": card, "data_b64": "notalist"})["code"] == -32602
    # delete drops every cell + the pointer (idempotent)
    assert result("card.asset_delete", {"path": card, "kind": "stickers"})["removed"] is True
    raw = json.loads(open(card, encoding="utf-8").read())
    assert "stickers" not in raw["data"]["extensions"]["lunamoth"].get("assets", {})
    assert result("card.asset_delete", {"path": card, "kind": "stickers"})["removed"] is False


def test_card_sticker_named_rename_remove():
    # Stickers carry their meaning in the filename; rename + per-image remove work,
    # and re-using a name dedups with a -1/-2 suffix.
    import base64 as b64m
    set_defaults()
    card = _make_user_card("StickerNames")
    cell = b64m.b64encode(_PNG_1PX).decode("ascii")
    out = result("card.stickers_save",
                 {"path": card, "data_b64": [cell, cell], "names": ["happy", "happy"]})
    f0, f1 = out["files"]
    assert f0.endswith(".sticker.happy.png") and f1.endswith(".sticker.happy-1.png")  # deduped
    # rename the first → a new slug; file renamed, list updated
    r = result("card.sticker_rename", {"path": card, "old": f0, "new": "Big Grin!"})
    assert r["new"].endswith(".sticker.big-grin.png") and r["old"] == f0
    assert not (Path(card).with_name(f0)).is_file() and (Path(card).with_name(r["new"])).is_file()
    # renaming to the SAME slug is a no-op (no -1 suffix bump)
    same = result("card.sticker_rename", {"path": card, "old": r["new"], "new": "big grin"})
    assert same["new"] == r["new"]
    # remove one → list shrinks, file gone; an untracked name is a clean error
    rm = result("card.sticker_remove", {"path": card, "name": f1})
    assert len(rm["files"]) == 1 and not (Path(card).with_name(f1)).is_file()
    assert rpc_error("card.sticker_remove", {"path": card, "name": "nope.png"})["code"] == -32602


def test_keyvisual_generation_receives_user_refs(monkeypatch):
    # User reference images must feed EVERY generation, the keyvisual MOST of all (it's
    # the identity anchor). Not an img2img concern — models like Seedream take
    # image + text → image together, so refs ride alongside the prompt.
    card, _b64 = _user_card_copy(None)
    H.save_key("火山", provider="volcano",
               base_url="https://ark.cn-beijing.volces.com/api/v3", api_key="sk-img-test")
    H.save_defaults({"image_provider": "volcano", "image_model": "doubao-seedream-x"})
    monkeypatch.setattr(H, "_complete",
                        lambda *a, **k: '{"appearance":"a","style":"anime","palette":"p","world":"w","theme":"#1a2"}')
    from lunamoth.tools.builtin import _image_gen
    seen = {}

    def fake_ark(prompt, size, refs=None):
        seen["refs"] = refs
        return ["http://x/kv.png"]

    monkeypatch.setattr(_image_gen, "ark_generate", fake_ark)
    monkeypatch.setattr(_image_gen, "download_bytes", lambda url: b"\x89PNG\r\n\x1a\nFAKE")
    sub = result("card.visual_generate", {"path": card, "kind": "keyvisual",
                                          "refs": ["data:image/png;base64,REF1"]})
    _await_visual(sub["job_id"])
    assert seen["refs"] == ["data:image/png;base64,REF1"]  # the user ref reached the keyvisual


def test_card_visual_generate_stickers_async(monkeypatch):
    # kind=stickers → one white sheet → sliced into 9 cells → AUTO-SAVED as a list,
    # the raw sheet kept for re-slicing; a 2x2 grid yields 4 cells.
    import io
    from PIL import Image
    set_defaults()
    card = _make_user_card("StickerGen")  # clean card, no bundled stickers
    H.save_key("火山", provider="volcano",
               base_url="https://ark.cn-beijing.volces.com/api/v3", api_key="sk-img-test")
    H.save_defaults({"image_provider": "volcano", "image_model": "doubao-seedream-x"})
    from lunamoth.tools.builtin import _image_gen
    from lunamoth.visuals import pipeline
    monkeypatch.setattr(pipeline._matte, "deps_available", lambda: False)
    sheet = Image.new("RGB", (300, 300), (255, 255, 255))
    buf = io.BytesIO(); sheet.save(buf, "PNG")
    monkeypatch.setattr(_image_gen, "generate_bytes", lambda prompt, size, refs=None: buf.getvalue())
    sub = result("card.visual_generate", {"path": card, "kind": "stickers",
                                          "brief": {"appearance": "a", "palette": "p", "theme": "#1a2"}})
    out = _await_visual(sub["job_id"])
    assert out["status"] == "ready" and out["saved"] is True and out["kind"] == "stickers"
    assert len(out["urls"]) == 9
    lm = json.loads(open(card, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    assert len(lm["assets"]["stickers"]) == 9
    sheets = lm["assets"]["sticker_sheets"]
    assert len(sheets) == 1 and (Path(card).with_name(sheets[0])).is_file()  # raw sheet kept
    # default names came from the expression tags
    assert any(".sticker.neutral.png" in n or ".sticker.happy.png" in n for n in lm["assets"]["stickers"])
    # re-slice the kept sheet at 2x2 → APPENDS 4 more cells
    rs = result("card.sticker_reslice", {"path": card, "sheet": sheets[0], "grid": 2})
    assert len(rs["files"]) == 13
    # a 2x2 generation yields exactly four cells
    sub2 = result("card.visual_generate", {"path": card, "kind": "stickers", "grid": 2,
                                           "brief": {"appearance": "a", "palette": "p", "theme": "#1a2"}})
    out2 = _await_visual(sub2["job_id"])
    assert len(out2["added"]) == 4


def test_card_asset_save_refuses_builtin_card():
    import base64 as _b64
    set_defaults()
    card = str(H.bundled_cards_dir() / "Quinn" / "card.json")
    b64 = _b64.b64encode(_PNG_1PX).decode("ascii")
    # bundled cards are read-only — only the user deck is writable (SEC).
    assert rpc_error("card.asset_save", {"path": card, "kind": "sprite", "data_b64": b64, "ext": "png"})["code"] in (-32031, -32035)


def test_card_patch_merges_field_preserving_rest():
    # Field-level patch: only the provided keys change; everything else is preserved
    # (deep-merge). This is the safe writer behind editing a card (incl. a running
    # chara's session card) without the whole-card-replace risk of card.save.
    set_defaults()
    card = _make_user_card("Patchy")
    out = result("card.patch", {"path": card, "fields": {"description": "new desc"}})
    assert out["path"] == card
    raw = json.loads(open(card, encoding="utf-8").read())
    assert raw["data"]["description"] == "new desc"
    assert raw["data"]["name"] == "Patchy"  # untouched
    # nested ext keys deep-merge (siblings preserved across two patches)
    result("card.patch", {"path": card, "fields": {"extensions": {"lunamoth": {"tagline": "t1"}}}})
    result("card.patch", {"path": card, "fields": {"extensions": {"lunamoth": {"polaris": "ideal"}}}})
    lm = json.loads(open(card, encoding="utf-8").read())["data"]["extensions"]["lunamoth"]
    assert lm["tagline"] == "t1" and lm["polaris"] == "ideal"
    # empty fields object + builtin card are clean errors
    assert rpc_error("card.patch", {"path": card, "fields": {}})["code"] == -32602
    builtin = str(H.bundled_cards_dir() / "Quinn" / "card.json")
    assert rpc_error("card.patch", {"path": builtin, "fields": {"description": "x"}})["code"] in (-32031, -32035)


def test_card_asset_library_list_upload_delete():
    # The 素材 tab: extra files of ANY format. Uploads land in the assets/ subdir; root
    # IMAGE strays also surface; managed art + card meta + session secrets never do.
    import base64 as b64m
    set_defaults()
    card = _make_user_card("AssetLib")
    folder = Path(card).parent
    # a managed sidecar + a stray root image are both on disk
    (folder / f"{Path(card).stem}.sprite.deadbeef.png").write_bytes(_PNG_1PX)
    (folder / "reference.png").write_bytes(_PNG_1PX)
    listed = result("card.assets_list", {"path": card})["assets"]
    rels = {a["rel"] for a in listed}
    assert "reference.png" in rels                          # stray root image shows
    assert not any(".sprite." in r for r in rels)           # managed art excluded
    assert "card.json" not in rels                          # card meta excluded
    # upload a non-image (any format) → lands in assets/, no url, kind=text
    up = result("card.asset_file_upload",
                {"path": card, "name": "Notes!", "data_b64": b64m.b64encode(b"hello world").decode(), "ext": "txt"})
    assert up["rel"] == "assets/notes.txt" and up["kind"] == "text" and up["url"] is None
    assert (folder / "assets" / "notes.txt").is_file()
    # read it back (preview/download path, since /asset can't serve a non-image here)
    rd = result("card.asset_file_read", {"path": card, "rel": up["rel"]})
    assert rd["kind"] == "text" and rd["content"] == "hello world"
    # an image upload also goes to assets/ but IS servable (has a url)
    img = result("card.asset_file_upload",
                 {"path": card, "name": "mood board", "data_b64": b64m.b64encode(_PNG_1PX).decode(), "ext": "png"})
    assert img["rel"] == "assets/mood-board.png" and img["url"].startswith("/asset?")
    # re-upload dedups
    dup = result("card.asset_file_upload",
                 {"path": card, "name": "mood board", "data_b64": b64m.b64encode(_PNG_1PX).decode(), "ext": "png"})
    assert dup["rel"] == "assets/mood-board-1.png"
    # delete by rel; traversal / managed / card-meta refused
    result("card.asset_file_delete", {"path": card, "rel": "reference.png"})
    assert not (folder / "reference.png").exists()
    result("card.asset_file_delete", {"path": card, "rel": "assets/notes.txt"})
    assert not (folder / "assets" / "notes.txt").exists()
    assert rpc_error("card.asset_file_delete", {"path": card, "rel": "../card.json"})["code"] == -32602
    assert rpc_error("card.asset_file_delete", {"path": card, "rel": "config.json"})["code"] == -32602
    assert rpc_error("card.asset_file_delete",
                     {"path": card, "rel": f"{Path(card).stem}.sprite.deadbeef.png"})["code"] == -32602


def test_card_asset_library_rejects_symlink_in_root(tmp_path):
    # A symlink in the card root must NOT be listed/served (it could point at an arbitrary
    # host image outside the card folder); the assets/ lane resolves-and-confines too.
    set_defaults()
    card = _make_user_card("SymCard")
    folder = Path(card).parent
    outside = tmp_path / "secret.png"
    outside.write_bytes(_PNG_1PX)
    try:
        (folder / "link.png").symlink_to(outside)
    except (OSError, NotImplementedError):
        import pytest as _pt
        _pt.skip("symlinks not supported here")
    rels = {a["rel"] for a in result("card.assets_list", {"path": card})["assets"]}
    assert "link.png" not in rels  # symlink stray not surfaced
    assert rpc_error("card.asset_file_delete", {"path": card, "rel": "link.png"})["code"] == -32602


def test_set_aspiration_writes_live_store_and_card():
    # 理想/aspiration: the live polaris.json (next turn) + the frozen card field (next wake).
    meta = wake_session()
    out = result("chara.set_aspiration", {"name": meta.name, "text": "become a great cook"})
    assert out["applies"] == "next_turn" and out["polaris"] == "become a great cook"
    assert json.loads((meta.sandbox_dir / "polaris.json").read_text())["polaris"] == "become a great cook"
    card = json.loads((meta.root / "card.json").read_text(encoding="utf-8"))
    assert card["data"]["extensions"]["lunamoth"]["polaris"] == "become a great cook"
    # clearing empties both the live store and the card field
    result("chara.set_aspiration", {"name": meta.name, "text": ""})
    assert json.loads((meta.sandbox_dir / "polaris.json").read_text())["polaris"] == ""
    assert "polaris" not in json.loads((meta.root / "card.json").read_text())["data"]["extensions"]["lunamoth"]


def test_card_patch_dirty_flag_and_apply_for_running_chara():
    # Editing a RUNNING chara's card flags it dirty (待应用); apply clears it.
    meta = wake_session()
    sc = str(meta.root / "card.json")
    # not running → no dirty flag
    result("card.patch", {"path": sc, "fields": {"description": "d1"}})
    assert not json.loads((meta.root / "config.json").read_text()).get("card_dirty")
    # simulate a live child (pid file points at this process)
    meta.mark_running()
    out = result("card.patch", {"path": sc, "fields": {"description": "d2"}})
    assert out.get("card_dirty") is True
    assert json.loads((meta.root / "config.json").read_text())["card_dirty"] is True
    assert any(e["name"] == meta.name and e["card_dirty"] for e in result("sessions.list"))
    # apply (no supervisor) clears the flag; the next start would read the new card
    ap = result("chara.apply_card", {"name": meta.name})
    assert ap["restarted"] is False
    assert not json.loads((meta.root / "config.json").read_text()).get("card_dirty")
    meta.clear_running()


def test_card_visual_jobs_and_wake_inflight_guard():
    import threading
    import time
    from lunamoth.visuals import jobs
    jobs._reset()
    set_defaults()
    card = _make_user_card("InFlight")
    ev = threading.Event()
    jid = jobs.submit(lambda: ev.wait(3), meta={"path": card})
    # a running generation is reported for the card + blocks an un-forced wake
    assert result("card.visual_jobs", {"path": card})["running"] == 1
    err = rpc_error("session.wake", {"card": card})
    assert err["code"] == -32050 and err["data"]["kind"] == "visual_in_flight"
    # force wakes anyway
    assert result("session.wake", {"card": card, "force": True})["name"]
    ev.set()
    for _ in range(100):
        if jobs.status(jid)["status"] != "running":
            break
        time.sleep(0.02)
    assert result("card.visual_jobs", {"path": card})["running"] == 0


def test_card_visual_brief(monkeypatch):
    set_defaults()
    card = str(H.bundled_cards_dir() / "Quinn" / "card.json")
    monkeypatch.setattr(H, "_complete",
                        lambda *a, **k: '{"appearance":"a","palette":"p","world":"w","theme":"#222"}')
    out = result("card.visual_brief", {"path": card})
    assert out["brief"]["appearance"] == "a" and out["brief"]["world"] == "w"


def test_matte_status_use_and_guards(monkeypatch):
    # R11: matte.status reports models + deps; matte.use persists the active id to
    # desktop.json (read by visuals.matte.selected_model); guards reject bad input.
    from lunamoth.visuals import matte as M
    monkeypatch.setenv("U2NET_HOME", str(os.path.join(os.environ["LUNAMOTH_HOME"], "u2net")))
    # A real model install would pip-install rembg INTO the test interpreter and hit
    # the network — which both fails offline and pollutes deps_available() for the
    # other matte tests in this process. Stub the background job so this stays a pure
    # wiring test; the install/download mechanics are covered in test_matte.py.
    started: list[str] = []
    monkeypatch.setattr(M, "download_async", lambda mid: started.append(mid) or {"state": "preparing"})

    st = result("matte.status")
    assert "models" in st and st["deps"] in (True, False)
    # an unknown model id is a clear param error (rejected before the job starts)
    assert rpc_error("matte.download", {"model": "ghost"})["code"] == -32602
    assert rpc_error("matte.use", {"model": "ghost"})["code"] == -32602
    assert started == []  # the rejected download never reached download_async
    # picking a valid model persists matte_model into the global defaults store
    st = result("matte.use", {"model": "birefnet-general-lite"})
    assert st["active"] == "birefnet-general-lite"
    raw = json.loads(H.desktop_config_path().read_text(encoding="utf-8"))
    assert raw["matte_model"] == "birefnet-general-lite"
    # one-click install: a valid download kicks off the background job and returns
    # the status payload — there is no separate deps step / -32050 error any more.
    st = result("matte.download", {"model": "birefnet-general"})
    assert "models" in st and started == ["birefnet-general"]


def test_use_key_activates_and_delete_removes():
    set_defaults()
    result("keys.save", {"label": "home", "provider": "openrouter",
                         "base_url": "https://or.example/v1", "api_key": "sk-home-9", "model": "m/x"})
    pub = result("defaults.use_key", {"label": "home"})
    assert pub["has_key"] is True and "api_key" not in pub and pub["model"] == "m/x"
    rows = result("keys.list")
    assert next(r for r in rows if r["label"] == "home")["active"] is True
    result("keys.delete", {"label": "home"})
    assert "home" not in [r["label"] for r in result("keys.list")]
    assert rpc_error("defaults.use_key", {"label": "home"})["code"] == -32035


def test_wake_with_named_key_uses_its_credentials():
    set_defaults()
    result("keys.save", {"label": "alt", "provider": "openrouter",
                         "base_url": "https://alt.example/v1", "api_key": "sk-alt-2", "model": "alt/model"})
    entry = result("session.wake", {"card": str(H.bundled_cards_dir() / "Quinn" / "card.json"), "key": "alt"})
    cfg = json.loads((S.load_session(entry["name"]).root / "config.json").read_text(encoding="utf-8"))
    # SEC-2: the named key's ROUTE is written (so load resolves the right key from
    # the global keyring by route), but the secret itself is never embedded.
    assert not cfg.get("api_key")
    assert cfg["base_url"] == "https://alt.example/v1"
    assert cfg["model"] == "alt/model"  # key's model fills in when wake didn't pick one
    err = rpc_error("session.wake", {"card": str(H.bundled_cards_dir() / "Quinn" / "card.json"), "key": "ghost"})
    assert err["code"] == -32035


# ---- toolpacks.list ----------------------------------------------------------------

def test_toolpacks_list_enumerates_bundled_packs():
    packs = result("toolpacks.list")
    names = [p["name"] for p in packs]
    assert "sandbox" in names
    sandbox = next(p for p in packs if p["name"] == "sandbox")
    assert sandbox["tools"] and sandbox["description"]


# ---- list_cards shadow semantics (webui-needs #11) ---------------------------------

def test_user_card_shadows_builtin_with_annotation_but_never_other_user_cards():
    builtin = json.loads((H.bundled_cards_dir() / "Quinn" / "card.json").read_text(encoding="utf-8"))
    deck = H.user_cards_dir()
    deck.mkdir(parents=True, exist_ok=True)
    (deck / "my-quinn.json").write_text(json.dumps(builtin, ensure_ascii=False), encoding="utf-8")
    (deck / "my-quinn-2.json").write_text(json.dumps(builtin, ensure_ascii=False), encoding="utf-8")
    cards = result("cards.list")
    same_name = [c for c in cards if c["name"] == builtin["data"]["name"] and c["lang"] == "en"]
    # both user files appear; the builtin is hidden but the shadow is declared
    assert len(same_name) == 2
    assert all(not c["builtin"] for c in same_name)
    assert any(c.get("shadows", "").endswith("card.json") for c in same_name)


# ---- generation helpers use the system default model (no per-task aux) -------------

def test_generation_helpers_use_system_default_model(monkeypatch):
    set_defaults()
    seen = []

    def fake_complete(defaults, system, user, model="", **kw):
        seen.append(model)
        return "rephrased line"

    monkeypatch.setattr(H, "_complete", fake_complete)
    # rewrite passes NO per-task model — _complete gets "" and fills the default.
    result("card.rewrite_field", {"field": "tagline", "value": "x"})
    assert seen == [""]

    # cards.draft likewise uses the default (model="") — even if a model is passed in.
    seen.clear()
    canned = json.dumps({"name": "N", "user_name": "friend", "description": "x" * 200, "first_mes": "hi",
                         "world_entries": [{"keys": ["a"], "content": "c", "constant": False},
                                           {"keys": ["b"], "content": "d", "constant": False}],
                         "polaris": "g", "tagline": "t", "theme_color": "#5B9FD4"})
    monkeypatch.setattr(H, "_complete", lambda *a, **k: (seen.append(k.get("model", "")) or canned))
    result("cards.draft", {"inspiration": "a moth", "model": "ignored/now"})
    assert seen == [""]


def test_validate_world_entries_keeps_up_to_ten_and_a_few_constants():
    """A richer world book: the draft validator now keeps up to 10 entries (was 4)
    and a few constants (was at most one), so generated cards aren't world-starved."""
    raw = [{"keys": [f"k{i}"], "content": f"content {i}", "constant": True} for i in range(12)]
    out = H._validate_world_entries(raw)
    assert len(out) == 10
    assert sum(1 for e in out if e["constant"]) == H.card_draft._MAX_WORLD_CONSTANTS


def test_generate_worldbook_fresh_and_expand(monkeypatch):
    set_defaults()
    canned = json.dumps({"entries": [
        {"keys": ["Aria", "sister"], "content": "Aria is the protagonist's botanist sister.", "constant": False},
        {"keys": ["The Vault"], "content": "The sealed archive beneath the city.", "constant": True},
    ]})
    seen = {}

    def fake_complete(defaults, system, user, model="", **kw):
        seen["user"] = user
        seen["model"] = model
        return canned

    monkeypatch.setattr(H, "_complete", fake_complete)
    # fresh: returns the structured entries; uses the system default model (model="").
    r = result("card.generate_worldbook", {"name": "Hero", "description": "a long enough persona to pass the guard"})
    assert [e["keys"] for e in r["entries"]] == [["Aria", "sister"], ["The Vault"]]
    assert seen["model"] == ""

    # expand: the existing entries are shown to the model so it won't duplicate them.
    result("card.generate_worldbook", {
        "name": "Hero", "description": "persona text here",
        "existing": [{"keys": ["Aria"], "content": "the sister"}], "mode": "expand",
    })
    assert "Aria" in seen["user"] and "do NOT repeat" in seen["user"]


def test_generate_worldbook_needs_content_and_surfaces_empty(monkeypatch):
    set_defaults()
    # no character content at all → a clear request error, never a fabricated book.
    err = rpc_error("card.generate_worldbook", {})
    assert err["code"] == -32602
    # an empty model reply surfaces as an error (no-fallback rule), not an empty book.
    monkeypatch.setattr(H, "_complete", lambda *a, **k: "")
    err2 = rpc_error("card.generate_worldbook", {"description": "enough persona content here"})
    assert "world book" in err2["message"].lower()


def test_defaults_no_longer_carries_aux_models():
    set_defaults()
    pub = result("defaults.set", {"model": "main/model", "aux_models": {"avatar": "x"}})
    assert "aux_models" not in pub          # aux machinery removed
    assert pub["model"] == "main/model"     # normal fields still persist


def test_default_flag_survives_tag_display_truncation():
    """The deck/welcome key on the `default` flag, which must survive the
    4-tag display cap — Quinn carries 'default' as its 5th tag, and the bug
    was that tags[:4] dropped it so the welcome fell back to LunaMoth."""
    cards = result("cards.list")
    quinn = [c for c in cards if c["name"] in ("小Q", "Quinn")]
    luna = [c for c in cards if c["name"] in ("月蛾", "LunaMoth")]
    assert quinn and all(c["default"] is True for c in quinn)
    assert luna and all(c["default"] is False for c in luna)
    # the displayed tag list is still capped at 4 (default need not appear there)
    assert all(len(c["tags"]) <= 4 for c in cards)


def test_session_card_is_writable_else_refused(tmp_path):
    """The in-chat Visuals editor edits the chara's FROZEN session card, so asset
    writes to <sessions>/<name>/card.json must be allowed — not just the user deck.
    A path outside both roots is refused (-32031)."""
    uc = H.user_cards_dir() / "Foo"
    uc.mkdir(parents=True)
    (uc / "card.json").write_text("{}", encoding="utf-8")
    assert H._writable_card_path(str(uc / "card.json")).name == "card.json"  # deck card: ok

    sc = S.sessions_dir() / "quinn"
    sc.mkdir(parents=True)
    (sc / "card.json").write_text("{}", encoding="utf-8")
    assert H._writable_card_path(str(sc / "card.json")).name == "card.json"  # session card: ok (the fix)

    bogus = tmp_path / "outside.json"  # sibling of LUNAMOTH_HOME → under neither root
    bogus.write_text("{}", encoding="utf-8")
    with pytest.raises(Exception):
        H._writable_card_path(str(bogus))


def test_image_catalog_lists_providers_and_key_presence():
    """image.catalog enumerates every image provider with its models and whether a
    usable key is set (reusing the named provider keyring), marking the active one."""
    # save a DashScope provider key in the named keyring + select DashScope
    H.save_key("阿里云", provider="dashscope",
               base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key="sk-ali")
    H.save_defaults({"image_provider": "dashscope", "image_model": "wan2.6-image"})

    cat = result("image.catalog")["providers"]
    by_id = {c["id"]: c for c in cat}
    assert set(by_id) == {"volcano", "dashscope", "openai", "openrouter"}
    assert by_id["dashscope"]["active"] is True
    assert by_id["dashscope"]["has_key"] is True          # from the keyring
    assert by_id["openai"]["has_key"] is False
    assert by_id["dashscope"]["models"]                    # has selectable models


def test_defaults_persist_image_provider_and_model():
    H.save_defaults({"image_provider": "openrouter",
                     "image_model": "google/gemini-2.5-flash-image-preview"})
    got = result("defaults.get")
    assert got["image_provider"] == "openrouter"
    assert got["image_model"] == "google/gemini-2.5-flash-image-preview"


def test_safe_extensions_secondary_only_theme_is_dropped():
    # A card whose theme has a secondary color but no valid primary used to crash
    # the card view with KeyError 'primary' ("handler error: 'primary'"). Root fix:
    # a theme REQUIRES a primary, so _clean_theme drops a primary-less {secondary}
    # entirely — no theme, no theme_color, no crash.
    out = H._safe_extensions_for_ui({"lunamoth": {"theme": {"secondary": "#abcdef"}}})
    assert "theme" not in out["lunamoth"]
    assert "theme_color" not in out["lunamoth"]
    # A primary present survives and still mirrors into the legacy field.
    out2 = H._safe_extensions_for_ui(
        {"lunamoth": {"theme": {"primary": "#112233", "secondary": "#445566"}}}
    )
    assert out2["lunamoth"]["theme"] == {"primary": "#112233", "secondary": "#445566"}
    assert out2["lunamoth"]["theme_color"] == "#112233"


# ---- keyring durability (2026-07 audit: desktop.json is the ONE secret store) ------

def test_concurrent_save_key_never_loses_an_entry():
    """keys.save runs on the RPC worker pool: two interleaved read-modify-write
    cycles used to silently drop each other's entry. The module lock serializes
    the whole RMW, so every thread's key survives."""
    import threading

    from lunamoth.server.hub import config as C

    n = 12
    errors = []

    def save(i):
        try:
            C.save_key(f"key-{i}", provider="openrouter",
                       base_url="https://example.invalid/v1", api_key=f"sk-{i}")
        except Exception as exc:  # noqa: BLE001 - the assertion target
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    labels = {k["label"] for k in C.list_keys()}
    assert labels == {f"key-{i}" for i in range(n)}


def test_keyring_write_is_atomic_temp_plus_replace(monkeypatch):
    """The keyring write must be temp-file + os.replace (a mid-write crash can
    never truncate every provider key), and the destination file must be 0600."""
    import os
    import stat

    from lunamoth.server.hub import config as C

    replaced = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", spy_replace)
    C.save_key("atomic", provider="openrouter",
               base_url="https://example.invalid/v1", api_key="sk-a")
    path = C.desktop_config_path()
    # The keyring landed via a temp sibling swapped into place — never a direct write.
    assert any(dst == str(path) and src != str(path) for src, dst in replaced)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # holds API keys
    # And the content is whole.
    assert any(k["label"] == "atomic" for k in C.list_keys())
