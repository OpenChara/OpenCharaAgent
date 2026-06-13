# Multimodal attachments — the wire/Python contract (branch 多模态适配)

Goal: a user (web chat AND WeChat) can send images and files to the chara; the
model receives them correctly. Follows hermes's shape: images that the model can
*see* are injected DIRECTLY into the user message content (OpenAI `image_url`
parts) — NOT via a tool call; files and oversized/unsupported images are copied
into the chara's `workspace/uploads/` and referenced by a text note, so the
chara reads them with its own tools (terminal/read_file).

This doc is the contract three workstreams share. Keep them consistent.

## 1. Raw attachment (over the wire, web → backend)

The web `send` RPC params gain an optional `attachments` array (omit when empty;
old clients keep sending `{text}` and still work):

```json
{
  "text": "what is this?",
  "attachments": [
    { "name": "photo.png", "mime": "image/png", "size": 12345, "data": "<base64, NO data: prefix>" }
  ]
}
```

`data` is base64 of the raw file bytes. `mime` is the browser-reported type
(fallback by extension). `size` is the original byte length (advisory).

## 2. Python ingest — `core/attachments.py` (NEW, owned by CORE)

```python
INLINE_IMAGE_MAX_BYTES = 1_500_000   # ~1.5 MB raw; small images inline as data URL

@dataclass
class RawAttachment:
    name: str
    mime: str
    data: bytes
    @classmethod
    def from_wire(cls, d: dict) -> "RawAttachment | None": ...  # decode base64; None if invalid

@dataclass
class IngestResult:
    content_parts: list[dict]   # OpenAI image_url parts for INLINE images (may be empty)
    notes: list[str]            # text lines appended to the user's message text
    notices: list[str]          # user-facing Notice texts (kind="attachment")
    saved: list[str]            # workspace-relative paths written

def ingest_attachments(raws: list[RawAttachment], *, sandbox, vision_ok: bool) -> IngestResult: ...
```

Per-attachment rules (is_image = mime.startswith("image/")):
- image + vision_ok + `len(data) <= INLINE_IMAGE_MAX_BYTES`
  → content_parts += `{"type":"image_url","image_url":{"url":"data:<mime>;base64,<b64>"}}`
  → note `[图片 / image: <name>]` (so the text transcript still records it arrived)
- image + vision_ok + too big
  → save to `uploads/<name>`; note `[图片已保存到 workspace/<path>（过大未内联，可用工具查看） / image saved (too large to inline)]`
- image + NOT vision_ok
  → save; notice `当前模型不支持图像；图片已存到 workspace/<path> / current model has no vision; image saved to <path>`; note the path
- non-image file
  → save to `uploads/<name>`; note `[用户上传文件 / file: <name> → workspace/<path>]`

Saving uses `sandbox.write_bytes(rel, data)` (NEW) which writes under
`workspace/uploads/`, de-duplicates name collisions (`name (2).png`), and returns
the workspace-relative path string (e.g. `uploads/photo.png`).

## 3. Building the user message (CORE, in `agent.stream_handle`)

`stream_handle(self, text, session, attachments=None)` — `attachments` is the
list of raw wire dicts. When present:
1. `raws = [RawAttachment.from_wire(d) for d in attachments]` (drop Nones)
2. `res = ingest_attachments(raws, sandbox=self.sandbox, vision_ok=self.llm.vision_supported())`
3. `body = text + ("\n" + "\n".join(res.notes) if res.notes else "")`
4. if `res.content_parts`: `content = [{"type":"text","text": body}] + res.content_parts`
   else: `content = body`
5. `session.context.add_message({"role":"user","content": content})`
   (replaces the plain `context.add("user", text)`)
6. `for n in res.notices: yield Notice("attachment", n)` BEFORE the model stream
World-info scan + audit still use the plain `text` (never the base64).

## 4. Vision capability — `llm.vision_supported()` (CORE)

Heuristic over `cfg.model` (a capability, not a preference), with an env safety
valve `LLM_VISION` = `auto|on|off` (default auto) on `LLMConfig.vision`:
known multimodal families → True. See `_VISION_HINTS` in llm.py.

## 5. Persistence (CORE) — `core/context.py` + `core/transcript.py`

- `context.pairs()` must flatten list content to its text part (UIs/tests).
- `transcript.append_message`: treat list-content messages as `struct`
  (`isinstance(msg.get("content"), list)`) so they serialize as JSON and reload
  intact (the struct load path already `json.loads`es).
- `context.render()` already passes `content` through verbatim — OpenAI accepts
  both string and list. No change there.

## 6. Server dispatch (CORE) — `server/dispatch.py._send`

Accept optional `attachments = params.get("attachments")` (must be a list of
dicts or absent; reject other types). Pass to `handle.stream_user(text, attachments)`.

## 7. Protocol (CORE) — `protocol/api.py`

`CharaHandle.stream_user(text, attachments=None)` → `self._agent.stream_handle(text, self._session, attachments)`.
Backward compatible: default None. Other callers (gateway) pass attachments too.

## 8. Web frontend (WEB workstream)

- A `+` button at the LEFT of the composer opens a hidden `<input type=file multiple>`
  (accept images + common files). Selected files → read as base64 (FileReader),
  staged as pending chips above the composer (thumbnail for images, name+icon for
  files), each removable. Drag-and-drop onto the chat surface stages the same way
  (dragover highlight; drop reads files).
- On send: include `attachments` in the `send` params; clear staging. Empty text
  WITH attachments is allowed (send anyway). Optimistic: the user bubble shows the
  thumbnails/file chips immediately. Honor the binding UI principle: instant click
  feedback + the existing "thinking" state covers the round-trip.
- Render incoming Notice(kind="attachment") as a secondary line.
- i18n keys (zh/en) for: attach button title, "unsupported file", drag hint,
  the model-has-no-vision notice mirror. Reuse `t()`.
- rpc.js: `CharaClient.send(text, attachments)` threads the array through.

## 9. WeChat gateway (WECHAT workstream)

- `InboundMessage` gains `attachments: tuple[dict, ...] = ()` (each dict =
  `{name, mime, data?(bytes), url?, kind}`; `kind` ∈ image|file|sticker).
- `weixin.item_list_to_text` → also recognize image (type 2?), file, emoji/sticker
  iLink item types and RETURN structured notes; full CDN-image *decryption* stays
  out of scope (the existing comment says so — no live creds), so produce a clear
  text marker (`[图片]`/`[文件: name]`/`[表情]`) and carry any directly-available
  url/path. Never silently drop a media item again.
- `messaging_host._process`: when `msg.attachments` carry inlineable bytes, pass
  them through `stream_user(text, attachments=[...])`; otherwise fold the media
  markers into the text so the chara knows media arrived. `emit_peer_message`
  shows the marker in the app window too.
- Outbound: if the chara's reply references a workspace image/file path, best-effort
  send via the adapter if it supports media; else send text. Document the limit
  honestly — do not fake delivery.

## Boundaries / honesty
- WeChat inbound pixel ingestion (CDN-encrypted images) is NOT fully implemented
  here — recognition + markers + a clean seam are. Say so in code comments.
- No failure fallbacks: a model that can't see images gets the workspace+notice
  path, never a silent drop or a fabricated description.
