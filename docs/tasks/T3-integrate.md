# Task: integrate the two feature branches and verify the whole

You are the integrator. Two branches were built in parallel from the same
main commit, in separate worktrees, with disjoint file scopes:

- `ctx-design` — three-zone prompt assembly, card-first world info, compaction
  persistence (touches core/, content/, tests/, docs/, READMEs)
- `server-gateway` — `lunamoth serve` JSON-RPC gateway (touches
  src/lunamoth/server/ NEW, front/cli.py, pyproject.toml, tests/test_server.py,
  READMEs)

Their reports are docs/tasks/T1-REPORT.md and T2-REPORT.md on their branches.

## Steps

1. Read `CLAUDE.md` first (binding rules, commit format — note the
   Co-Authored-By convention; use `Co-Authored-By: Codex <noreply@openai.com>`).
2. You are in the MAIN worktree on branch `main`. Confirm `git status` is
   clean. Run the suite once: `uv sync && uv run python -m pytest -q`.
3. `git merge ctx-design` — expected clean or near-clean. Resolve conflicts
   minimally (likely only README*.md / CLAUDE.md prose: keep BOTH branches'
   roadmap items). Run the full suite + `uvx ruff check --select F
   src/lunamoth tests`; fix what broke, smallest change possible.
4. `git merge server-gateway` — same procedure. The only real overlap risk is
   front/cli.py (the serve subcommand) and README files.
5. Cross-check the seams the branches share:
   - the server's dispatch calls CharaHandle — if ctx-design changed any
     CharaHandle behavior the server tests rely on, reconcile (CharaHandle's
     surface itself was NOT supposed to change).
   - `tests/test_architecture.py` must pass (layer rules).
6. Full verification: entire pytest suite green, ruff clean, plus smoke:
   `uv run lunamoth version` and `uv run lunamoth doctor`.
7. Write `docs/tasks/INTEGRATION-REPORT.md`: merge order, conflicts resolved,
   final test count, anything you had to fix, anything still open. Commit on
   main. Do NOT push. Do NOT delete the worktrees or branches.

If a merge reveals a genuine design clash you cannot resolve with a small
fix, STOP that merge (git merge --abort), keep the other branch's merge, and
document precisely what clashes in the report — do not force it.
