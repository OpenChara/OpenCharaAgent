"""Context compaction — Hermes-style, adapted to LunaMoth.

When the conversation approaches the model's real context window, summarize the
OLD portion into one compact note and keep the recent tail verbatim, instead of
hard-dropping the oldest messages (which is amnesia). The full conversation is
never lost — transcript.py keeps it all on disk; compaction only reshapes the
in-memory window the model actually sees.

Design (agreed):
- Trigger at ~75% of the **real model window** (providers.context_window).
- Protect the recent tail (~1/4 of the window) verbatim.
- Summarize everything before it — in a **neutral, factual voice**, NOT the
  chara's (a roleplay summary would distort facts; we want ground truth). The
  previous summary sits at messages[0], so it's folded into the next summary for
  free (iterative update without extra bookkeeping).
- For an artist/maker chara: the summary must record **what was actually created**
  (workspace file paths), matching the rules layer's "your work must be real".
- Offline/mock or any LLM failure → no-op (the buffer's own trim() is the safety
  net). Never raises, never blocks the turn.

ContextBuffer can't call the LLM (it's a dumb data structure), so the agent drives
this and passes its LLMClient in.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from ..obs import get_logger
from .context import ContextBuffer, _msg_text, estimate_tokens

_log = get_logger("compaction")

THRESHOLD_RATIO = 0.75       # compact once the window is this full
_TAIL_RATIO = 0.25           # keep this fraction of the window as verbatim tail
_TAIL_MIN_TOKENS = 2000
_TOOL_RESULT_CLIP = 240      # one-line old tool output summaries for the summarizer

# Live-window tool-output pruning (audit #13, hermes context_compressor
# _prune_old_tool_results pass 1+2): old tool results carry full multi-KB
# payloads in the LIVE window until whole-message trim/compaction. Replacing
# them with one-line factual summaries — and dedup'ing identical results (the
# same file read five times) — shrinks the window with ZERO LLM cost, so it
# runs as the cheap first step of compact(); the full results stay on disk in
# the transcript, this only narrows the in-memory API view (same contract as
# trim()).
_LIVE_PRUNE_MIN_CHARS = 200  # only substantial tool outputs are worth pruning
_LIVE_PRUNE_PROTECT_MSGS = 6  # always keep the last N messages verbatim (floor)
_DUP_TOOL_RESULT = "[duplicate tool output — identical to a more recent call]"

# Anti-thrashing guard (audit #10, hermes context_compressor scar #40803):
# should_compact() re-fires every turn once over threshold, so a failing or
# non-shrinking summary call would burn one LLM call per turn FOREVER — the
# same failure family as the burned-key patience incident.
_MIN_SHRINK = 0.10              # a compaction saving less than this is "ineffective"
_INEFFECTIVE_LIMIT = 2          # consecutive ineffective compactions before backing off
_REGROW_STEP = 1.10             # resume once the window grows 10% past where the guard engaged
_FAILURE_COOLDOWN = 300.0       # seconds without retry after a summarizer error

_now = time.monotonic  # patchable in tests


@dataclass
class _Guard:
    ineffective: int = 0
    cooldown_until: float = 0.0
    resume_above: float = 0.0   # token level the window must grow past to re-arm

    def allows(self, tokens: int) -> bool:
        if _now() < self.cooldown_until:
            return False
        if self.ineffective >= _INEFFECTIVE_LIMIT:
            if tokens > self.resume_above:
                # The window genuinely grew past the next step — re-arm.
                self.ineffective = 0
                self.resume_above = 0.0
                return True
            return False
        return True

    def record_failure(self) -> None:
        self.cooldown_until = _now() + _FAILURE_COOLDOWN
        _log.warning("summarizer failed — compaction paused for %.0fs (trim backstop remains)", _FAILURE_COOLDOWN)

    def record_ineffective(self, tokens: int) -> None:
        self.ineffective += 1
        if self.ineffective >= _INEFFECTIVE_LIMIT:
            self.resume_above = tokens * _REGROW_STEP
            _log.warning(
                "last %d compactions saved <%d%% each — pausing until the window grows past ~%d tokens",
                self.ineffective, int(_MIN_SHRINK * 100), int(self.resume_above),
            )

    def record_success(self) -> None:
        self.ineffective = 0
        self.cooldown_until = 0.0
        self.resume_above = 0.0


def _guard(ctx: ContextBuffer) -> _Guard:
    """One guard per live ContextBuffer, stored on the buffer itself so its
    lifecycle (sessions, /reset, tests) follows the window it guards."""
    g = getattr(ctx, "_compaction_guard", None)
    if g is None:
        g = _Guard()
        ctx._compaction_guard = g  # type: ignore[attr-defined]
    return g

_HEADER = "[Earlier conversation — a summary of everything before the recent messages]\n"

# English instruction (the engine prompt layer is English). The summary is the
# ONE piece of model-generated text that gets persisted back into the context,
# so it must follow the conversation's language even though this instruction is
# English — hence the explicit language guard on the last line.
_INSTRUCTION = (
    "You are a precise note-taker compressing an agent's conversation+work log so it can "
    "continue without losing the thread. Write a TERSE, factual third-person summary — NOT in "
    "any character's voice. Capture, with concrete detail:\n"
    "- the operator's standing requests / goals still in play\n"
    "- what was actually DONE, and specifically what files/works were CREATED in the workspace "
    "(give real paths); never credit work that wasn't actually produced\n"
    "- key facts, decisions, and constraints established\n"
    "- open threads / what is unfinished\n"
    "Preserve any earlier-summary content that is still relevant. Omit chit-chat. Be compact. "
    "Write the summary in the same language as the conversation being summarized."
)


_PRUNED_MARK = "output pruned: "  # identifies an already-one-lined tool result


def _tool_output_summary(tool_name: str, content: str) -> str:
    one = " ".join((content or "").split())
    if len(one) > _TOOL_RESULT_CLIP:
        one = one[: _TOOL_RESULT_CLIP - 1] + "…"
    lines = content.count("\n") + 1 if content.strip() else 0
    label = tool_name or "tool"
    return f"[{label} {_PRUNED_MARK}{len(content)} chars, {lines} line(s)] {one}"


def _already_pruned(content: str) -> bool:
    return content.startswith("[") and _PRUNED_MARK in content[:64]


def _prune_tool_outputs_for_summary(messages: list[dict]) -> list[dict]:
    """Cheap zero-LLM pass: summarize old tool outputs in the copy sent to the
    summarizer. The live ContextBuffer is not mutated by this pruning."""
    call_names = _call_names(messages)

    pruned: list[dict] = []
    for msg in messages:
        if msg.get("role") != "tool":
            pruned.append(dict(msg))
            continue
        content = str(msg.get("content") or "")
        tool_name = call_names.get(str(msg.get("tool_call_id") or ""), "tool")
        pruned.append({**msg, "content": _tool_output_summary(tool_name, content)})
    return pruned


def _call_names(messages: list[dict]) -> dict[str, str]:
    """tool_call_id → declaring tool name, scanned from assistant messages."""
    names: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            call_id = str(tc.get("id") or "")
            name = str((tc.get("function") or {}).get("name") or "")
            if call_id:
                names[call_id] = name
    return names


def _live_prune_boundary(msgs: list[dict]) -> int:
    """Index marking the start of the protected tail: tool results AT OR AFTER
    this index keep their full content; everything BEFORE it is "old" and
    prunable. Walks back accumulating ~_TAIL_MIN_TOKENS of recent messages.

    The message that pushes the accumulator OVER budget is itself treated as
    old (boundary = i + 1) — otherwise a single giant tool result that alone
    exceeds the tail budget would protect itself forever (exactly the payload
    #13 most wants gone). A message-count floor (hermes min_protect) keeps the
    last _LIVE_PRUNE_PROTECT_MSGS verbatim regardless, so a recent tool pair is
    never one-lined out from under an in-progress turn."""
    floor = max(0, len(msgs) - _LIVE_PRUNE_PROTECT_MSGS)
    budget = _TAIL_MIN_TOKENS
    acc = 0
    boundary = 0
    for i in range(len(msgs) - 1, -1, -1):
        acc += estimate_tokens(_msg_text(msgs[i])) + 2
        if acc >= budget:
            boundary = min(i + 1, len(msgs) - 1)
            break
    return min(boundary, floor)


def prune_live_tool_outputs(ctx: ContextBuffer) -> bool:
    """Shrink the LIVE window by one-lining and dedup'ing OLD tool outputs
    (audit #13). Mutates ctx.messages in place; persists NOTHING — the full
    results remain on disk in the transcript, exactly as trim() narrows the
    in-memory view without rewriting history. Returns True if it changed
    anything. Zero LLM cost.

    Two passes, ported from hermes _prune_old_tool_results:
      1. Dedup — an identical tool result (same file read repeatedly) older
         than the most recent copy becomes a one-line back-reference.
      2. One-line — a substantial tool result before the protected tail is
         replaced by `[tool output pruned: N chars, M line(s)] <head>`.
    Already-pruned rows (recognizable markers) are skipped so repeated calls
    are idempotent and don't re-summarize a summary."""
    msgs = ctx.messages
    if len(msgs) < 2:
        return False
    boundary = _live_prune_boundary(msgs)
    if boundary <= 0:
        return False  # the whole window is protected tail — nothing old to prune
    names = _call_names(msgs)
    dup_marker = _DUP_TOOL_RESULT
    changed = False

    # Pass 1: dedup identical results across the WHOLE window, newest kept.
    seen: set[str] = set()
    for i in range(len(msgs) - 1, -1, -1):
        msg = msgs[i]
        if msg.get("role") != "tool":
            continue
        content = str(msg.get("content") or "")
        if len(content) < _LIVE_PRUNE_MIN_CHARS:
            continue
        if content == dup_marker or _already_pruned(content):
            continue
        h = hashlib.md5(content.encode("utf-8", "replace")).hexdigest()[:12]
        if h in seen:
            # An older duplicate — but only collapse it if it is OUTSIDE the
            # protected tail (recent identical reads stay verbatim).
            if i < boundary:
                msgs[i] = {**msg, "content": dup_marker}
                changed = True
        else:
            seen.add(h)

    # Pass 2: one-line substantial tool results older than the protected tail.
    for i in range(boundary):
        msg = msgs[i]
        if msg.get("role") != "tool":
            continue
        content = str(msg.get("content") or "")
        if len(content) < _LIVE_PRUNE_MIN_CHARS:
            continue
        if _already_pruned(content) or content == dup_marker:
            continue
        tool_name = names.get(str(msg.get("tool_call_id") or ""), "tool")
        msgs[i] = {**msg, "content": _tool_output_summary(tool_name, content)}
        changed = True

    return changed


def _serialize(messages: list[dict]) -> str:
    """Flatten the head into plain text for the summarizer.

    Old tool outputs are pre-pruned to one line in this serialized copy so the
    summary call spends budget on facts and file/command anchors, not bulk logs.
    """
    out: list[str] = []
    for m in _prune_tool_outputs_for_summary(messages):
        content = str(m.get("content") or "")
        if m.get("kind") == "summary":
            out.append(f"[earlier summary]\n{content}")
        elif m.get("role") == "tool":
            out.append(f"[tool result] {content}")
        elif m.get("tool_calls"):
            names = ", ".join(tc.get("function", {}).get("name", "?") for tc in m["tool_calls"])
            out.append(f"{m.get('role','assistant')} (ran: {names}) {content}".strip())
        else:
            out.append(f"{m.get('role','')}: {content}")
    return "\n\n".join(s for s in out if s.strip())


def _budget(ctx: ContextBuffer) -> int:
    """The usable prompt budget = the same target trim() uses (window minus the
    reply/tool headroom). Tying compaction to this guarantees it fires BEFORE
    trim() hard-drops anything."""
    return max(0, ctx.max_tokens - ctx.trim_buffer_tokens)


def _window_tokens(ctx: ContextBuffer, llm) -> int:
    """Window size for the compaction trigger (audit #8, hermes
    context_compressor): prefer the provider's REAL prompt_tokens from the
    most recent stream over the char heuristic. The real number sees the whole
    request (stable prefix, tool schemas, volatile tail) and is exact on
    CJK-heavy text; the heuristic sees only the history and drifts both ways
    (thrash or overflow).

    Plausibility gate: trust the real number only while the live history is a
    substantial share of the request it measured (heur·4 ≥ real). The same
    client survives /reset and make_session — without the gate, stale usage
    from a window that no longer exists would drive compaction of a
    near-empty buffer."""
    heur = ctx.token_count()
    if not getattr(llm, "usage_fresh", False):
        return heur
    real = int(getattr(llm, "last_prompt_tokens", 0) or 0)
    if real > 0 and heur * 4 >= real:
        return real
    return heur


def should_compact(ctx: ContextBuffer, llm) -> bool:
    budget = _budget(ctx)
    if not (llm and llm.is_live()) or budget <= 0:
        return False
    tokens = _window_tokens(ctx, llm)
    if tokens < THRESHOLD_RATIO * budget:
        return False
    return _guard(ctx).allows(tokens)


def _align_tail_cut(msgs: list[dict], cut: int) -> int:
    """The tail must not OPEN with orphaned tool results (audit #11, hermes
    _align_boundary_backward): render() silently drops a role:"tool" message
    whose declaring assistant got summarized away — the freshest work would
    vanish from the API view. Pull the cut back to the assistant that made the
    calls so the whole group stays verbatim. If there is no parent in the
    window (already orphaned), push forward instead so the orphans fold into
    the summary rather than stranding at the tail head."""
    if cut < len(msgs) and msgs[cut].get("role") == "tool":
        j = cut - 1
        while j >= 0 and msgs[j].get("role") == "tool":
            j -= 1
        if j >= 0 and msgs[j].get("role") == "assistant" and msgs[j].get("tool_calls"):
            return j
        while cut < len(msgs) and msgs[cut].get("role") == "tool":
            cut += 1
    return cut


def _anchor_last_user(msgs: list[dict], cut: int) -> int:
    """Keep the operator's most recent user message OUT of the summary (audit
    #11, hermes _ensure_last_user_message_in_tail, scar #10896): a summarized
    active request effectively disappears — the model stalls on it, repeats
    finished work, or drops it. A user message is itself a clean boundary (no
    tool-pair splitting risk), so anchoring is a plain pull-back."""
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            return min(cut, i)
    return cut


def compact(ctx: ContextBuffer, llm, *, force: bool = False) -> bool:
    """Replace the old head of the window with one summary message. Returns True
    if it changed anything. Safe to call any time; no-ops when not worth it.

    Guarded against thrash (audit #10): consecutive ineffective compactions
    back it off until the window regrows; a summarizer failure pauses retries
    for _FAILURE_COOLDOWN. The buffer's own trim() remains the sanctioned
    backstop either way. `force` (the operator's explicit /compact) bypasses
    and clears the guard — hermes lets manual compression through the cooldown."""
    budget = _budget(ctx)
    if not (llm and llm.is_live()) or budget <= 0:
        return False
    tokens_before = ctx.token_count()
    guard = _guard(ctx)
    if force:
        guard.cooldown_until = 0.0
    else:
        # Trigger on real usage when fresh (audit #8); the shrink measurement
        # below stays on the heuristic — it is the only measure available for
        # the just-rewritten window, and before/after must share a scale.
        trigger_tokens = _window_tokens(ctx, llm)
        if trigger_tokens < THRESHOLD_RATIO * budget:
            return False
        if not guard.allows(trigger_tokens):
            return False

    msgs = ctx.messages
    if len(msgs) < 4:
        return False

    # Cheap zero-LLM first pass (audit #13): one-line / dedup OLD tool outputs
    # in the LIVE window. This routinely reclaims multi-KB results before the
    # expensive summarize call — and if it alone drops the window back under
    # threshold, the LLM summary is skipped entirely (an effective, free
    # compaction). Persists nothing; the full results stay in the transcript.
    if prune_live_tool_outputs(ctx) and not force:
        # The pre-prune real-usage number (audit #8) now overstates the rewritten
        # window, so the skip test goes on the heuristic, which reflects the
        # actual post-prune size — and shrink is measured on the same scale.
        tokens_after = ctx.token_count()
        if tokens_after < THRESHOLD_RATIO * budget:
            if tokens_before > 0 and (tokens_before - tokens_after) / tokens_before >= _MIN_SHRINK:
                guard.record_success()
            else:
                guard.record_ineffective(tokens_after)
            return True

    # Walk back from the end, protecting a verbatim tail of ~tail_budget tokens.
    tail_budget = max(_TAIL_MIN_TOKENS, int(budget * _TAIL_RATIO))
    acc = 0
    cut = None
    for i in range(len(msgs) - 1, -1, -1):
        acc += estimate_tokens(_msg_text(msgs[i])) + 2
        if acc >= tail_budget:
            cut = i
            break
    if cut is not None:
        # Boundary hygiene (audit #11): the token walk is blind to structure.
        cut = _align_tail_cut(msgs, cut)
        cut = _anchor_last_user(msgs, cut)
    if cut is None or cut < 2:   # whole thing fits in the tail → nothing old to fold
        # Over threshold but nothing compactable: without counting this the
        # guard never fires and every turn re-walks a no-op (#40803).
        if not force:
            guard.record_ineffective(tokens_before)
        return False

    summary = _summarize(msgs[:cut], budget, llm)
    if not summary:
        guard.record_failure()
        return False

    summary_msg = {"role": "system", "content": _HEADER + summary, "kind": "summary"}
    tail = [dict(m) for m in msgs[cut:]]
    ctx.messages = [summary_msg] + tail
    stale = getattr(llm, "mark_usage_stale", None)
    if callable(stale):
        stale()  # the captured usage described the pre-compaction window
    tokens_after = ctx.token_count()
    if tokens_before > 0 and (tokens_before - tokens_after) / tokens_before < _MIN_SHRINK:
        guard.record_ineffective(tokens_after)
    else:
        guard.record_success()
    if ctx.persist is not None:
        try:
            ctx.persist(summary_msg)
            # The transcript is append-only. Re-append the protected tail after
            # the summary checkpoint so restore can load "latest summary + rows
            # after it" without losing recent verbatim context; the older raw
            # rows remain on disk for the full historical record.
            for msg in tail:
                ctx.persist(msg)
        except Exception:
            pass
    return True


def _summarize(head: list[dict], budget: int, llm) -> str:
    convo = _serialize(head)
    if not convo:
        return ""
    out_budget = min(2048, max(512, budget // 8))
    messages = [
        {"role": "system", "content": _INSTRUCTION},
        {"role": "user", "content": convo},
    ]
    return llm.raw_complete(messages, max_tokens=out_budget)
