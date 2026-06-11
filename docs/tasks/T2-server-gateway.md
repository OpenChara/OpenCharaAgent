# Task: the remote gateway — `lunamoth serve` (JSON-RPC over stdio + WebSocket)

You are an autonomous senior engineer working ALONE on branch `server-gateway`
in this git worktree. Another agent works on core/content in a different
worktree — to avoid merge pain you may ONLY touch:

    src/lunamoth/server/        (new package — yours)
    src/lunamoth/front/cli.py   (one new `serve` subcommand only)
    pyproject.toml              (optional-dependency extra only)
    tests/test_server.py        (new)
    README.md / README.zh-CN.md (tick the Remote TUI roadmap item at the END)

Do NOT modify core/, content/, tools/, protocol/, obs/, session/, presence/,
or any other front/ file. If you believe a backend change is required, write
the need into docs/tasks/T2-REPORT.md instead of making it.

## Before writing any code (mandatory)

1. Read `CLAUDE.md` (binding project rules), `docs/refactor-plan.md` §4
   (protocol design — the contract you are exposing) and
   `src/lunamoth/protocol/` (events.py, codec.py, api.py: `CharaHandle` is the
   complete backend surface; `to_dict`/`from_dict` are the event wire format).
2. Study the reference (symlinked, read-only): `reference/hermes-agent` —
   `hermes_cli/web_server.py` and the `tui_gateway` dispatch: ONE dispatch
   served over stdio AND WebSocket, clients are thin. That's the model.
3. `uv sync && uv run python -m pytest -q` (must be green before and after).

## Hard rules (owner's, non-negotiable)

- NO character names/flavor anywhere in src/. NO failure fallbacks — errors
  surface as JSON-RPC errors, never fabricated results.
- `tests/test_architecture.py` must stay green (server/ may import protocol/
  + obs/ + session/; it must NOT import front/ or textual/rich).
- Keep every existing test green. Commits end:
  `Co-Authored-By: Codex <noreply@openai.com>`

## Design (agreed with the owner)

**One serve process hosts ONE chara.** This is a hard constraint, not a
simplification: session paths (LUNAMOTH_SANDBOX/CONFIG_DIR) are resolved at
import time from env (see CLAUDE.md), so a process is bound to one chara.
`lunamoth serve NAME` activates that session env BEFORE importing the runtime
(copy the `_activate` pattern in front/cli.py). Multi-chara hosting later =
one subprocess per chara; out of scope.

### Protocol

- Newline-delimited JSON-RPC 2.0 over the transport. Add
  `{"protocol_version": 1}` to the handshake/hello.
- Requests (map 1:1 onto CharaHandle — do not invent backend behavior):
  - `attach {present}` → AttachInfo as a dict
  - `send {text}` → starts a stream; events arrive as notifications (below);
    reply when the turn ends
  - `idle {}` → one unattended cycle, same event streaming
  - `interrupt {}` → abandon the in-flight stream (server-side worker thread +
    threading.Event, the same pattern front/tui/app.py uses — read it)
  - `command {line}` → Reply as a dict
  - `snapshot {}` → StateSnapshot as a dict
  - `permission_reply {id, granted}` → answers a pending `permission_ask`
    notification (wire CharaHandle.set_permission_hook to a queue + event;
    timeout = deny, exactly like the TUI's hook)
  - `detach {}` → presence bookkeeping, then the server may exit (stdio) or
    close the connection (ws)
- Notifications (server→client, no id): `event {…}` using
  `protocol.codec.to_dict` verbatim for stream events; `permission_ask
  {id, kind, reason, detail, wait_seconds}`.
- One client at a time is fine for v1; reject a second concurrent attach
  cleanly.

### Transports

- stdio: stdlib only. `lunamoth serve NAME --stdio` speaks JSON-RPC on
  stdin/stdout; ALL logging stays in obs/ files (never stdout — it would
  corrupt the protocol).
- WebSocket: `lunamoth serve NAME --host 127.0.0.1 --port 8137`
  using the `websockets` library, added as an OPTIONAL extra in pyproject
  (`[project.optional-dependencies] server = ["websockets>=12"]`).
  Missing dependency → a clear error telling the operator to
  `uv sync --extra server`. Token auth: `--token` or auto-generate and print
  once at startup; require it as the first message (`auth {token}`) or a
  query param — reject otherwise. Bind 127.0.0.1 by default; document that
  exposing it publicly is the operator's decision (README line).
- Both transports share ONE dispatch module (that is the entire point).

### Suggested layout

    src/lunamoth/server/__init__.py    (docstring)
    src/lunamoth/server/dispatch.py    (method table; CharaHandle calls; worker thread)
    src/lunamoth/server/stdio.py
    src/lunamoth/server/ws.py

### Tests (tests/test_server.py)

No network flakiness: test the dispatch directly (feed request dicts, collect
response + notification dicts) with the mock provider env (copy the agent
fixture pattern from tests/test_living.py — tmp_path env vars). At minimum:
attach→send roundtrip with TextDelta notifications, command(), snapshot(),
interrupt mid-stream (use a slow fake stream via monkeypatch), permission ask
→ reply granted, auth rejection for ws (can be a unit test of the auth check
function). A real end-to-end stdio subprocess test is a bonus, not required.

## Definition of done

All tests green, ruff clean (`uvx ruff check --select F src/lunamoth tests`),
committed on `server-gateway` in logical steps, README roadmap items updated
(EN+zh: tick/describe Remote TUI gateway). Do not merge into main; do not
push. Finish with `docs/tasks/T2-REPORT.md` (what works, how to try it, e.g.
`uv run lunamoth serve home --stdio`, anything deferred).
