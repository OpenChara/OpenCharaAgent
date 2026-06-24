<!-- Thanks for contributing! Keep this short but concrete. -->

## What & why

<!-- What does this change, and what problem does it solve? Link any issue. -->

## How it was verified

<!-- The gates you ran locally — tick what applies. -->

- [ ] `uv run python -m pytest -q` (backend suite green)
- [ ] `uvx ruff check --select F src/lunamoth tests` (lint clean)
- [ ] `cd apps/web && npm run lint && npm run typecheck && npm run test` (if the SPA changed)
- [ ] Added/updated tests for the behavior I changed

## Impact

- [ ] User-visible change (docs updated — **both** `README.md` and `README.zh-CN.md` if user-facing)
- [ ] Security-relevant (sandbox / secrets / RPC surface) — explain below
- [ ] Crosses a layer boundary (`tests/test_architecture.py` still passes)

<!-- Anything reviewers should pay special attention to. -->
