# Task: the card is the ONE file — retire the standalone world channel + themes dir

You are a senior engineer on LunaMoth. Work in THIS worktree (branch
`cards-one-file`). Read `CLAUDE.md` (binding) — the first design principle is
the spec here: "The card is the soul — and the ONE external file… world
(embedded `character_book`) … all live in the card."

Today the engine fully supports the embedded `character_book` (see
`core/agent.py` stable/volatile world assembly), but a parallel legacy channel
survives: `Settings.world_path` + `LUNAMOTH_WORLD`, the card's
`extensions.lunamoth.world` path pointer, the same-stem pairing convention,
`persona.default_world_path()`, wizard/welcome world pickers, and split
bundled content (`characters/*.json` + `worlds/*.json`). Kill the channel,
keep ST compatibility as an IMPORT path.

## 0. DISCIPLINE (non-negotiable)

Do NOT merge into main, do NOT push, do NOT touch other worktrees or delete
branches. Commit on YOUR branch, write the .done flag, stop — the supervisor
Fable reviews and integrates. When your final summary is written you are DONE:
no self-directed extra acceptance runs, no extra commits.

## 1. Bundled content: merge and rename

- For each bundled pair, embed the world into the card as a standard ST
  `character_book` (V3 shape: `{name, entries: [...]}` — convert the current
  dict-keyed entries; preserve keys/content/constant/insertion fields exactly;
  `Lorebook.from_dict` must round-trip it). Pairs:
  `LunaMoth.{en,zh}` ← `worlds/LunaMoth.{en,zh}.json`;
  `SCP-079.{en,zh}` ← `worlds/SCP-Foundation.{en,zh}.json`.
  `Quinn.{en,zh}` (新加入的默认助理卡) has no paired world — it only moves
  in the rename; do not invent a book for it.
- Rename `characters/` → `cards/` (the name every other surface already
  uses). Move `worlds/LICENSE-CC-BY-SA-3.0.md` attribution needs into
  `cards/` if its text mentions world content; then DELETE `worlds/`.
- Update every reference: `persona.py` (`default_character_path`,
  DELETE `default_world_path`), `.gitignore` allowlist, `server/hub.py`
  (`bundled_cards_dir`), READMEs (both), CLAUDE.md (module map + content
  line + prompt-stack wording), `docs/archive/context-design.md` (world sources:
  "the card's embedded book" only).
- `cards/Quinn.{en,zh}.json` already ship with embedded books (the exemplar
  of the one-file format) — no merge needed, just move with the rename.
- **Default-card selection becomes a tag convention** (no character name in
  src/, ever): `persona._localized_json` prefers, among localized candidates,
  a card whose `data.tags` contains `"default"`; falls back to the current
  alphabetical-first rule. Quinn carries the tag; LunaMoth no longer does —
  so this change is what flips the bundled default persona to Quinn. Test:
  default resolves to Quinn for both langs; with the tag removed, falls back
  alphabetically. READMEs: the "default character" paragraphs now describe
  Quinn the intern (keep LunaMoth 月蛾 as a featured bundled card).

## 1b. The default card becomes Quinn 小Q via the "default" tag convention

Owner decision (2026-06-12): `cards/Quinn.{en,zh}.json` (already on main,
owner-authored, carries the `"default"` tag; LunaMoth's tag was already
removed) is the bundled DEFAULT persona; LunaMoth 月蛾 stays bundled as the
flagship example card.

- `persona.default_character_path()`: among bundled cards, prefer the
  localized card whose `data.tags` contains `"default"`; fall back to the
  current sorted-order behavior when no card carries the tag. Tag reading
  must tolerate missing/non-list tags.
- Tests: default resolution picks Quinn for zh and en; removing the tag
  falls back to sorted order.
- Update the CLAUDE.md history line and both READMEs where the default card
  is named (CLAUDE.md may already say Quinn — keep it true either way).

## 2. Engine: one world source

- `core/agent.py`: delete `self.world` and the whole wpath resolution —
  world blocks come ONLY from `self.character.character_book` (both constant
  and keyword paths; keep the sticky/namespace behavior, namespace can become
  `book:{card name}`).
- Delete `Settings.world_path`, the `LUNAMOTH_WORLD`/`LUNAMOSS_WORLD` env
  mapping, and `world_path=` at every construction site (wizard, welcome,
  tests). `load_settings` already ignores unknown keys in old config files —
  verify with a test (old config with world_path loads cleanly).
- Remove the `extensions.lunamoth.world` hook from cards.py defaults and from
  the CLAUDE.md hook list (a path pointer violates one-file; embedded book
  replaces it).
- `front/wizard.py` + `front/tui/welcome.py`: remove world pickers/fields.
- Session activation (`session/sessions.py` / wherever config is read): if a
  session's `config.json` still has a non-empty `world_path` pointing at an
  existing file, perform a ONE-TIME migration: merge those entries into the
  card file the session uses (if the card is a shared/bundled path, write a
  merged copy into the session dir and repoint `character_path`), log one
  clear line, then rewrite config without `world_path`. Never lose a user's
  world silently.

## 3. ST compat becomes an import

- Standalone world-book JSON remains importable: add hub RPC
  `card.merge_world {card_path, world: <parsed json|path>}` → parses with
  `Lorebook` rules, merges entries into the card's `character_book`
  (append; collision on identical keys+content = skip), saves via the
  existing card-save path (extensions sanitization included). The web
  `/upload` endpoint: a `.json` that parses as a world book (has `entries`,
  no card `data`) should be stored and surfaced so the deck can offer
  "merge into card X" — minimal: return `{kind: "world"}` so the frontend
  knows; the deck UI itself is another branch's scope, don't build UI.
- `lunamoth` CLI: nothing new needed; document that worlds import via the
  deck.

## 4. Retire the `themes/` content dir (TUI is frozen — default theme only)

- DELETE the top-level `themes/` directory (scp-079.json, seraphina.json, its
  LICENSE copy). The TUI stays in the tree but ships only its built-in
  default theme.
- Do NOT touch TUI code (`front/tui/` is frozen): `content/themes.py` keeps
  the built-in theme + LUNAMOTH_BANNER (terminal.py needs it),
  `Settings.tui_theme_path` stays (an operator can still point at their own
  file; with no bundled dir `/theme` just lists none — acceptable frozen
  behavior).
- Update references: CLAUDE.md content line (now: `cards/` `toolpacks/`),
  README dir tables + the license section in BOTH READMEs (SCP themes are
  gone — the CC-BY-SA note now covers only the SCP cards with their embedded
  books under `cards/`), README feature lines that advertise "with themes".
  `content/themes.py` module docstring: themes are user-supplied files now,
  not a repo dir.

## 5. Tests

- Update the ~16 test files that pass `world_path=` (mostly delete the kwarg).
- New tests: bundled cards each load with a non-empty character_book and the
  constant entries reach `_stable_prefix`; old-config-with-world_path loads +
  migration merges and strips the key; `card.merge_world` merges and
  dedupes; a world-book upload is recognized as `kind: "world"`.
- Full suite `uv run python -m pytest -q` green;
  `uvx ruff check --select F src/lunamoth tests` green.

## Constraints

- ST V2/V3 card compatibility is sacred: `character_book` stays the standard
  field, PNG/JSON import unchanged.
- No flavor text added anywhere; engine stays silent when a card declares
  nothing.
- Both READMEs; user-facing strings bilingual or English-only.
- Commit in logical steps; you sign your own Co-Authored-By.

When done: write `.codex-fleet/cards-one-file.done` with a summary + test
counts + what a user with an old world_path config will see on first launch.
