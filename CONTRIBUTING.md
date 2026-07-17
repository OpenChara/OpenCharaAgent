# Contributing to OpenCharaAgent

Thanks for your interest. This guide is the contributor's view of how to build,
test, and submit changes. (Security issues: please follow [SECURITY.md](SECURITY.md)
instead of opening a public issue.)

## Project layout

- `src/chara/` — the Python runtime (agent core, tools + OS sandbox, the
  JSON-RPC hub/supervisor, messaging gateways, the Textual TUI). Domain
  subpackages with an **enforced** dependency direction — see below.
- `apps/web/` — the React + TypeScript desktop SPA (built into the wheel).
- `tests/` — the backend test suite (pytest).
- `protocol/` — the cross-layer contract (events + JSON wire codec); frontends
  speak only this, never the backend internals.

## Dev setup

Backend (needs [uv](https://docs.astral.sh/uv/)):

```bash
uv sync --extra dev --extra server   # plain `uv sync` drops pytest — always use the extras
uv run chara                       # the web/desktop hub
uv run chara tui                   # the terminal UI
```

Frontend (needs Node 22):

```bash
cd apps/web
npm install
npm run dev      # Vite dev server; proxies /rpc + ws to a running `chara desktop`
```

## Before you open a PR

Run the same gates CI runs — a green local run is the bar.

Backend:

```bash
uv run python -m pytest -q                         # full suite
uvx ruff check --select F src/chara tests       # lint (unused imports / undefined names)
```

Frontend:

```bash
cd apps/web
npm run lint        # eslint (fails on errors)
npm run typecheck   # tsc --noEmit
npm run test        # vitest
```

CI additionally runs the backend suite on macOS (the strongest OS-jail tests are
platform-gated) and an **architecture boundary test** (`tests/test_architecture.py`)
that fails if the layering is violated.

## Conventions

- **Commit messages**: conventional-commit style — `type(scope): summary`
  (e.g. `fix(hub): …`, `feat(visuals): …`, `docs: …`). Keep the summary
  imperative and under ~72 chars; explain the *why* in the body.
- **Layering is enforced**: nothing outside `front/` may import `front/` or
  Textual/Rich; `front/` reaches the backend only through `protocol/`; `protocol/`
  has zero internal dependencies. If your change needs to cross a boundary,
  it probably belongs at a different layer — `test_architecture.py` will tell you.
- **READMEs are bilingual**: update both `README.md` (English) and
  `README.zh-CN.md` (中文) when you change user-facing docs.
- **No silent failures**: a failed request surfaces a real error; we don't
  fabricate output or fall back to a different model. UI actions respond
  instantly and show progress; a swallowed error is a bug.
- Keep new code in the idiom of the file around it (naming, comment density,
  structure). Add a test for the behavior you change.

## Submitting

1. Branch from `main` (don't commit directly to it).
2. Make the change + add/adjust tests; run the gates above.
3. Open a PR using the template; describe what changed and why, and note any
   user-visible or security-relevant impact.

By contributing you agree your contributions are licensed under the project's
[Apache-2.0 License](LICENSE).
