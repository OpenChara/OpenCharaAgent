# Context design — three-zone prompt, card-first world info, compaction finish

The prompt/context requirements and their implementation status (all shipped).

## 1. Three-zone API shape

Each model call is assembled as:

1. **Stable prefix** — cached per session and byte-identical until a session boundary: card identity, rules/tool nudge, toolpack note, frozen memory snapshot, frozen skill index, and constant world-info blocks.
2. **History** — `ContextBuffer` messages only. It is append-only except for sanctioned compaction rewrites.
3. **Volatile tail** — recomputed per user turn and appended after history: env facts, keyword-activated world info, goals, and the post-history slot as the final system message.

Volatile tail text is never written to the context buffer or transcript.

## 2. Card-first content

- `CharacterCard.render_system()` renders identity/persona only; card `post_history_instructions` is reserved for the final post-history slot.
- Post-history priority is: card PHI > card `extensions.lunamoth.rules_closer` > bundled rules closer. Bundled closer remains tool-gated.
- `extensions.lunamoth.goals` seeds persistent goals only once, when the per-chara goal store is empty.

## 3. World info

- The ONE world source is the card's embedded `character_book` — there is no
  standalone world channel (no `Settings.world_path`, no
  `extensions.lunamoth.world` pointer, no same-stem pairing). Standalone ST
  world books are an import format: merged into a card via `card.merge_world`.
- `constant=true` entries render into the stable prefix.
- Keyword entries render only into the volatile tail (sticky namespace
  `book:{card name}`).
- Scan text is the last ~4 history messages plus the current user/ephemeral text.
- Activated keyword entries stay sticky for 4 turns in `Session.wi_sticky`.
- Activated world-info output is capped at approximately 25% of the model context window, truncating by insertion order.

Non-goals remain recursion, probability, `@Depth` insertion numbers, and inclusion groups.

## 4. Compaction

- Successful compaction writes a `kind="summary"` transcript row and re-appends the protected tail after it, preserving the full raw history on disk.
- Transcript restore loads the latest summary row plus everything after it in the current epoch.
- Old tool outputs are pre-pruned to one-line summaries only in the copy sent to the summarizer; live history is not destructively pruned beyond the normal compaction replacement.
- No concurrency lock is added; the current stream worker is single-threaded for compaction.

## 6. Acceptance checklist

- [x] Hash of the assembled stable prefix is identical across consecutive turns.
- [x] `/net on`, presence flips, and world-info activation changes alter only the volatile tail; stable hash is unchanged.
- [x] Card PHI is the last message of an assembled call; the old top-of-prompt placement is gone from `render_system()`.
- [x] Constant entries appear in stable; keyword entries only in the tail.
- [x] An entry triggered only by a message older than the scan window does not activate.
- [x] Sticky keeps an entry for 4 turns.
- [x] The 25% world-info cap truncates by order.
- [x] `compact()` then new agent/session restores `context[0]` as the summary without a new summarization LLM call.
- [x] Volatile text never appears in transcript rows.
- [x] Card `extensions.lunamoth.goals` seeds goals once and then persistent goals belong to the chara.
