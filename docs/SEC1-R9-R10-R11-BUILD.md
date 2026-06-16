# Build log — SEC-1 + R10 + R11 + R9 (branch worktree-sec1-r9-r10-r11)

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

## R10 — global key management in Settings (multi-key, text + image)  [TODO]
## R11 — matte (抠像) model download + load from Settings  [TODO]
## R9 — bring the visuals pipeline into the app (UX decisions made pragmatically)  [TODO]
