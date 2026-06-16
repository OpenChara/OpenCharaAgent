# Build log — SEC-1 + R10 + R11 + R9 (branch worktree-sec1-r9-r10-r11)

## STATUS: ALL FOUR DONE ✅ (SEC-1, R10, R11, R9 — backend + UI)
Full suite 834 passed / 1 skipped, ruff clean, node --check clean on all web JS.
Each feature was implemented → tested → subagent-audited in this worktree → committed
→ pushed. Commits: SEC-1 b5f48fa · R10 b6aa4c2 · R11 fa5bc6e · R9 backend d323724 ·
R9 UI 9faa993 · R9 ext-fix f4e380a. NOT merged to main (awaiting owner review).
LIVE-VERIFY remaining (can't unit-test here): SEC-1 Electron cookie on real /asset
image loads; R9/R10/R11 end-to-end with a real ARK image key + a downloaded matte model.


One branch, one worktree. Order chosen by dependency + risk:
SEC-1 (bounded, security) → R10 (global key mgmt; keyring mostly exists) →
R11 (matte download/load) → R9 (in-app visuals pipeline; depends on R10+R11).

Each feature: design → implement → test (`uv run python -m pytest -q`, ruff,
`node --check`) → subagent audit → commit. Keep going across /loop wakeups until
all four are done. Small refactors allowed; keep the repo maintainable.

## SEC-1 — authenticate the /asset GET  [DONE]
Shipped: SameSite=Strict `lm_asset` cookie (= session token), set by both server
(HttpOnly, every response) and renderer (rpc.js BOOT); `/asset` GET accepts cookie OR
`?token=`, else 403; no server token → open (dev). Leak-closure (config.json/transcript
→404) intact behind the gate. Audited (no bypass; cross-site blocked; same-origin image
loading preserved by early cookie-set + server Set-Cookie + ?token). Tests: auth required,
cookie/query unlock, wrong/malformed cookie + dev-open. Full suite 799 green.
LIVE-VERIFY (can't unit-test): real Electron renders avatars/backgrounds with a token set.
Approach: a SameSite=Strict `lm_asset` cookie = the session token. The server sets it
on every response (HttpOnly) and the renderer also sets it via JS (belt-and-suspenders,
since the token lives in the URL hash, client-side). `/asset` GET accepts the token from
the `lm_asset` cookie OR the `?token=` query; mismatch → 403; no server token (dev) → open.
Same-origin renderer <img>/background requests carry the cookie; a cross-site page can't
(SameSite=Strict) and can't know the token → blocked. Zero /asset-URL changes needed.
Acceptance: cookie→200, ?token→200, neither→403, no-token→open; existing leak-closure
(config.json/transcript still 404) intact; full suite green. LIVE caveat: real Electron
cookie behavior can't be unit-tested here — the JS-set cookie + ?token fallback de-risk it.

## R10 — global key management in Settings (multi-key, text + image)  [DONE]
Shipped: the named multi-key store (add/label/delete + "make default") now has a UI —
Settings·模型 lists saved keys with the active one badged, an add form, and a delete
button (hub keys.list/save/delete/use_key already existed; secrets never echo — only
has_key/active). A new Settings·生图 pane sets the GLOBAL image key + image model;
both persist into desktop.json defaults (image_api_key/image_model, added to
_DEFAULT_FIELDS; image_api_key joins _SECRET_FIELDS so _public_defaults reduces it to
has_image_key — never echoed). tools/builtin/_image_gen.py now resolves the image key
(env ARK_API_KEY → desktop.json image_api_key → bare ark_api_key file) and the image
model (env → desktop.json → DEFAULT_MODEL) by reading desktop.json DIRECTLY (stdlib
only — tools/ never imports server/). The JS sends image_api_key only when non-empty,
so saving model-only keeps the stored secret. Tests: 5 _image_gen resolution/precedence
cases + 1 hub defaults case (secret never echoes, image fields persist, text-key save
doesn't disturb the image secret). Audited (no secret leak, no layering break, instant
UI). Full suite 806 green, ruff clean.
## R11 — matte (抠像) model download + load from Settings  [DONE]
Shipped: a new in-app package src/lunamoth/visuals/ with matte.py — the matting
model manager. A pinned MODELS registry (birefnet-general / -lite / isnet-general-use
/ u2net) with the EXACT rembg release URLs + byte sizes (verified against the
~/.u2net cache: birefnet-general=972666916, isnet-general-use=178648008). Functions:
matte_home (U2NET_HOME/~/.u2net — the SAME dir rembg reads, so a download here is
reused at runtime), selected_model (env → desktop.json matte_model → default),
is_installed (exact-size check — a truncated file is never "ready"), deps_available
(rembg importable), download (stream to .onnx.part → verify size → atomic rename; bad
size/short body rejected + cleaned up, no fabrication), download_async (threaded,
lock-guarded _progress polled by the UI), delete, status, and cut (lazy rembg/PIL/numpy
import — the cutout step R9 will call). rembg/onnxruntime are a new OPTIONAL `visuals`
extra (kept out of the base install). Hub RPC: matte.status/download/delete/use
(download guarded -32602 unknown / -32050 deps-absent; use persists matte_model). UI:
Settings·生图 gains a 抠像模型 section — model list with size/note, installed/active
badges, a download button with a live progress bar (poll matte.status while busy),
make-default + delete, and a "install the extra" hint with disabled controls when deps
are absent. Layering clean (visuals imports only stdlib; rembg lazy). Tests: 13 matte
unit cases (registry/paths/precedence/install/download streaming+mismatch+async/delete/
status/cut-without-deps) + 1 hub case (status/use/guards). Full suite 820 green, ruff
clean. Audited: integrity verified, no fabrication, no poll/thread leak.
## R9 — bring the visuals pipeline into the app  [BACKEND DONE · UI pending owner UX]
Backend shipped: src/lunamoth/visuals/pipeline.py — card → visual brief (LLM) →
Seedream image → optional matte → preview bytes. Ports the prompt craft of
visuals/cardbrief.py + visuals/genviz.py (BRIEF_SYSTEM, STYLE_REAL/CHIBI/WHITE_BG,
per-kind prompts for avatar/sprite/background) but closes both gaps the OPEN-WORK
entry calls out: (a) the brief is NO LONGER hard-coded to gemini + a separate
openrouter_key — it runs through an INJECTED llm_call so the module never imports
server/, and the hub passes a closure over _complete(load_defaults()) = the GLOBAL
default text model + key (same seam as card drafting); (b) the image key/model come
from R10 (_image_gen) and the matte from R11 (visuals.matte). generate() is honest:
no image key / empty result / non-image body all raise; a requested-but-unavailable
matte returns the image un-matted with matted=False + a note (never a fake cut).
Hub RPC card.visual_generate {path, kind, matte} returns the preview (base64 +
brief + matted + note) — UNOPINIONATED about placement (saving via avatar_upload or
a future sprite path), so the UI decision stays open. Maps RuntimeError/ValueError →
visible -32050; unknown kind → -32602 before any spend; only ever called explicitly
(no auto-trigger on load/wake → no surprise cost). Tests: 13 pipeline cases + 1 hub
case. Layering clean (architecture test green; pipeline imports work without the
visuals extra). Audited: no hard-coded model/key, no fabrication, no implicit spend.

UI — DONE (owner UX answered 2026-06-16): entry = the card editor's 视觉 section
(openVisualsEditor, replacing the old avatar editor); flow = step-by-step + confirm;
assets = avatar + 立绘(sprite) + 背景(background), each with generate / upload /
replace / delete; a one-click "生成全部 (N)" that states the image-credit cost and
asks confirm(); user-supplied reference images (≤3) guide generation (threaded as
refs → Seedream `image`). Per owner: the OLD avatar pipeline (card.avatar_generate
SVG + the dual theme-color pickers) was DELETED — hub RPC + avatar_generate()/
_persona_summary_for_avatar()/_AVATAR_GENERATE_SYSTEM/_strip_svg_fence removed, and
buildAvatarControls/openAvatarEditor replaced. New backend: card.visual_brief (build
the brief once, reused across the set so "generate all" pays for ONE brief),
card.asset_save / card.asset_delete (generic sprite/background/keyvisual sidecars,
16MB cap, magic-byte checked, user-deck only), card.visual_generate now takes
brief+refs, ark_generate/pipeline.generate gained refs. Avatars are downscaled to
512² client-side before avatar_upload (it's inlined into hub.state, must stay tiny);
sprite/background save full-size via asset_save (cacheable /asset URLs). Create flow
keeps an upload-only avatar (buildAvatarUpload) since generation needs a saved card.
Tests: asset_save/delete (+ builtin refusal), visual_brief, visual_generate refs/
brief-reuse, refs passthrough. Full suite 833 green, ruff clean, node --check clean.
Audited (subagent, in-worktree): caught + fixed a dangling chat.js openAvatarEditor
call; security on asset_save confirmed (no traversal/builtin/oversize); no fabrication;
generate-all cost-gated. NOTE: manual theme-color editing was removed with the old
pipeline per owner; theme now rides the card / the brief's theme color.
