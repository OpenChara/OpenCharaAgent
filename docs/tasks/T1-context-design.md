# Task: implement docs/context-design.md (three-zone prompt, card-first world info, compaction finish)

You are an autonomous senior engineer working ALONE on branch `ctx-design` in
this git worktree. Another agent works on an unrelated branch in a different
worktree; an integrator merges later. Work only here, commit only here.

## Before writing any code (mandatory)

1. Read `CLAUDE.md` (project rules — they are binding), `docs/refactor-plan.md`
   (the architecture you must respect), and `docs/context-design.md` (THE
   REQUIREMENTS — its §6 checklist is your acceptance criteria).
2. Study the references under `reference/` (symlinked, read-only):
   - `reference/hermes-agent/agent/system_prompt.py` (~line 347-369): the
     stable→context→volatile split this design copies.
   - hermes's context compaction (search for their compaction/summarization
     module): how they prune tool outputs BEFORE summarizing, and how summaries
     persist. Our `src/lunamoth/core/compaction.py` was adapted from it.
   - `reference/SillyTavern/public/scripts/world-info.js`: `world_info_depth`
     (default 2; we use ~4), the 25% budget cap (~line 73), sticky timed
     effects, and `@Depth` tail injection (~line 855).
3. Run the suite once to see green: `uv sync && uv run python -m pytest -q`
   (105 tests). Lint: `uvx ruff check --select F src/lunamoth tests`.

## Hard rules (owner's, non-negotiable)

- NO specific character may appear anywhere in src/ — not in code, comments,
  or default prompt text. The engine ships ZERO default flavor text.
- NO failure fallbacks, NO fallback models, NO fabricated output — a failed
  request fails in the open. (Compaction is the one sanctioned best-effort
  no-op, because trim is its backstop.)
- Frontends (front/) may not import core/ or tools/ — `tests/test_architecture.py`
  enforces all layer rules; it must stay green. Do not touch front/ at all.
- Keep every existing test green. Behavior not covered by the requirements
  must not change.
- Commit in small steps with clear messages ending:
  `Co-Authored-By: Codex <noreply@openai.com>`

## The implementation plan (agreed with the owner — follow it)

### Step 1 — cards.py PHI fix (independent, do first)

`src/lunamoth/content/cards.py` `render_system()` (~line 162) wrongly appends
`post_history_instructions` into the persona block at the TOP of the prompt.
Remove that; expose the PHI (already a field) for the post-history slot.
SillyTavern renders PHI as the LAST system message after the whole history.

### Step 2 — world info two-tier (content/worldinfo.py)

- `WorldEntry.constant` already exists. Split the API: constant entries →
  a `constant_blocks(char, user)` (stable, never flaps); keyword entries →
  activation for the volatile tail only.
- Shallow scan: activation text = the LAST ~4 history messages + the current
  user text — NOT the whole context (today `core/llm.py _messages` joins the
  entire context; that is the bug that makes entries never deactivate).
- Sticky: an activated keyword entry stays active for 4 turns. The counter
  state lives in `Session` (core/agent.py dataclass), NOT in Lorebook (which
  is shared/static). Entries need stable ids (index position is fine).
- Budget cap: total activated world-info ≤ ~25% of the model's context window;
  truncate by entry order beyond it.
- Non-goals (do NOT implement): recursion, per-entry probability, @Depth
  numbers, inclusion groups.

### Step 3 — three-zone assembly (the core change)

Target call shape, every API request:

```
[system] STABLE PREFIX   — byte-identical within a session
[ ... ]  HISTORY         — ContextBuffer (append-only; compaction = sanctioned rewrite)
（ephemeral user message, only when in_context=False — the idle timestamp tick）
[system] VOLATILE TAIL   — recomputed per turn, NEVER persisted, post-history slot LAST
```

- Split `core/agent.py _build_system_messages` into:
  - `_stable_prefix()` — card identity (render_system, now PHI-free) · rules
    layer · the static tool nudge sentence (WITHOUT the env facts — those are
    volatile) · toolpack note · frozen memory snapshot · frozen SKILLS index
    (freeze at session start like memory; create_skill's tool reply already
    confirms inclusion) · constant world-info blocks.
    Compute ONCE per session and cache the list object; invalidate only on
    make_session / reconfigure / reset. Byte-identity follows by construction.
  - `_volatile_tail(scan_text, session)` — env facts line (isolation, network,
    operator presence, day-level date) · keyword-activated world info (shallow
    scan + sticky + cap) · goals block (`goals.render_block()` — the chara
    mutates goals mid-session, so they are volatile by nature) · LAST: the
    post-history slot.
  - Post-history slot content, single non-empty winner in priority order:
    card `post_history_instructions` > card `extensions.lunamoth.rules_closer`
    > bundled `rules.closer()`. The rules closer stays tool-gated as today;
    a tool-less pure-roleplay chara gets the card PHI only (or nothing).
- `core/llm.py`: REPLACE the `system_provider` callback with explicit
  per-call zone lists (e.g. `stable: list[str]`, `volatile: list[str]`)
  threaded from agent through `stream_complete`/`stream_agent`/`raw_complete`
  paths as needed. The agent computes scan_text itself from
  `session.context.messages[-4:]` + the user text.
- `stream_agent` tool loop: keep its growing `messages` list WITHOUT the
  volatile tail; each `_stream_turn` call sends `messages + volatile`. That way
  tool-result messages never land AFTER the volatile tail, and the post-history
  slot is always literally last. Volatile is computed once per user turn (not
  re-scanned between tool steps).
- VOLATILE content must NEVER reach ContextBuffer or the transcript. (Presence
  event lines and the time-gap notes are HISTORY — they already go through
  `session.context.add`; leave them as they are.)

### Step 4 — compaction finish (core/compaction.py + core/transcript.py)

Be careful here; the owner suspects latent issues — re-verify the whole module
against hermes while you are in it.

1. Persist the summary: `compact()` currently rewrites `ctx.messages` directly
   (line ~127), which bypasses the persist hook — the summary never reaches the
   transcript, so a restart re-compacts from scratch (one wasted LLM call and a
   worse summary). Write the summary to the transcript when compaction
   succeeds (a `kind="summary"` row fits the existing schema's kind column),
   and change restore so it loads "[latest summary row] + everything after it"
   instead of the raw tail. `core/transcript.py load()` and
   `core/agent.py make_session()` are the touch points. Full history must stay
   on disk. `/reset` epochs must keep working.
2. Cheap pre-pruning: before the summarization LLM call, prune old tool
   OUTPUTS in the head being summarized (keep ~1 line each — hermes does this
   as a separate zero-cost phase). Only the copy being serialized for the
   summary prompt; never mutate history destructively beyond what compact
   already replaces.
3. Do NOT add a concurrency lock (single stream worker today; documented).

### Step 5 — card as the ONE external file

- New: `extensions.lunamoth.goals` (list of strings) — the card's seed
  beliefs/mission. Seed them into the GoalStore ONCE (first boot of a chara:
  goals.json absent/empty), by="card". After that they belong to the chara.
  Touch points: `content/cards.py .defaults()`, `core/agent.py` init,
  `tools/goals.py` (by-values are free-form already).
- `worlds/` files keep working as optional shared libraries (SillyTavern
  compat unchanged); the embedded `character_book` is simply primary now.

### Step 6 — tests (tests/test_zones.py + extend existing)

Turn `docs/context-design.md` §6 into tests:
- hash of the assembled stable prefix is identical across consecutive turns;
- `/net on`, presence flips, and WI activation changes alter ONLY the volatile
  tail (stable hash unchanged);
- card PHI is the LAST message of an assembled call; the old top-of-prompt
  placement is gone (assert it's absent from render_system output);
- constant entries appear in stable; keyword entries only in the tail; an
  entry triggered by a message older than the scan window does NOT activate;
  sticky keeps an entry for 4 turns; the 25% cap truncates;
- compact → new agent + make_session → context[0] is the summary, and NO new
  summarization LLM call happened (count raw_complete calls via monkeypatch);
- volatile text never appears in transcript rows.
Use the existing test idioms (mock provider env, tmp_path sandbox env vars —
note CLAUDE.md's warning: config paths are pinned at import time).

### Step 7 — docs

Update the bilingual READMEs (README.md + README.zh-CN.md roadmap: one new
checked item describing this work, same style as neighbors), tick the boxes in
`docs/context-design.md` §6, and refresh the "prompt stack" section of
CLAUDE.md to describe the three zones.

## Definition of done

All tests green (old + new), ruff clean, committed on `ctx-design` in logical
steps. Do not merge into main; do not push. Finish by writing a short
`docs/tasks/T1-REPORT.md` (what changed, decisions taken, anything left).
