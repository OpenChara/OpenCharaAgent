"""session_search — long-term conversation recall over the chara's transcript,
ported from hermes-agent (reference/hermes-agent/tools/session_search_tool.py).

Faithful to the hermes calling-shape contract (four shapes, no mode parameter;
zero LLM calls; every shape returns actual DB messages), re-implemented against
OpenCharaAgent's per-chara transcript SQLite (core/transcript.py).

KEY ADAPTATION (per .codex-fleet/spec-general-tools.md §4 + seam):
  - OpenCharaAgent has ONE chara per process and uses **epochs** (the /reset boundary),
    not multi-session lineage. So a hermes "session" maps to a transcript
    **epoch**, and ``session_id`` here is the epoch number (as a string).
  - The hermes multi-profile layer (profile=, @session: links, cross-profile DB
    scan) and parent/child lineage are STRIPPED — OpenCharaAgent has no profile
    registry and no child sessions; the current-session guard maps to the
    current epoch.
  - No FTS5 virtual table exists on the transcript, and this tool may not edit
    core/transcript.py. Discovery therefore uses a SQLite LIKE scan over the
    message text (AND across whitespace-split terms, OR via the literal token
    ``OR``, quoted phrases honoured) plus snippet extraction — same observable
    behaviour (keyword recall, ranked-ish, no LLM), simpler index.

The four shapes, mode inferred from which args are set:
  DISCOVERY  pass ``query``
  SCROLL     pass ``session_id`` + ``around_message_id``
  READ       pass ``session_id`` only
  BROWSE     pass nothing
Precedence: scroll > read > discovery > browse.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..registry import registry, tool_error

logger = logging.getLogger("chara.tools.session_search")

# Roles surfaced by default in discovery (tool output is usually noise).
_DEFAULT_ROLES = ["user", "assistant"]
# Message kinds that count as readable conversation (mirror transcript.load()).
_READABLE_KINDS = ("chat", "think", "struct", "summary")


def _format_timestamp(ts: Any) -> str:
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts).strftime("%B %d, %Y at %I:%M %p")
        if isinstance(ts, str):
            if ts.replace(".", "").replace("-", "").isdigit():
                return datetime.fromtimestamp(float(ts)).strftime("%B %d, %Y at %I:%M %p")
            return ts
    except (ValueError, OSError, OverflowError):
        logger.debug("failed to format timestamp %s", ts, exc_info=True)
    return str(ts)


def _decode_content(content: str, kind: str) -> str:
    """Recover readable text from a transcript row. ``struct`` rows store a full
    message dict as JSON; pull its ``content`` (string, or joined text parts)."""
    if kind != "struct":
        return content or ""
    try:
        msg = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content or ""
    if not isinstance(msg, dict):
        return content or ""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [p.get("text", "") for p in c if isinstance(p, dict)]
        return " ".join(t for t in parts if t)
    # tool-call-only assistant turns have no prose content.
    return ""


def _shape_row(row: sqlite3.Row, anchor_id: Optional[int] = None) -> Dict[str, Any]:
    kind = row["kind"]
    entry: Dict[str, Any] = {
        "id": row["id"],
        "role": row["role"],
        "content": _decode_content(row["content"], kind),
        "timestamp": row["ts"],
    }
    if anchor_id is not None and row["id"] == anchor_id:
        entry["anchor"] = True
    return entry


def _connect_ro(path) -> Optional[sqlite3.Connection]:
    """Open the transcript DB read-only (no write lock; safe on a live chara)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        logger.debug("session_search: cannot open transcript %s", path, exc_info=True)
        return None


def _epoch_meta(conn: sqlite3.Connection, epoch: int) -> Dict[str, Any]:
    """First/last timestamp + message count for an epoch (the 'session' meta)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(ts) AS started, MAX(ts) AS last "
        "FROM messages WHERE epoch=? AND kind IN (?,?,?,?)",
        (epoch, *_READABLE_KINDS),
    ).fetchone()
    return {
        "started_at": row["started"] if row else None,
        "last_active": row["last"] if row else None,
        "message_count": int(row["n"]) if row and row["n"] else 0,
    }


def _parse_terms(query: str) -> List[List[str]]:
    """Split a query into OR-groups of AND-terms. Quoted phrases stay whole.

    ``alpha beta`` → [[alpha, beta]] (AND). ``a OR b`` → [[a], [b]] (OR).
    ``"docker net" deploy`` → [["docker net", "deploy"]].
    """
    tokens: List[str] = []
    buf = ""
    in_quote = False
    for ch in query:
        if ch == '"':
            in_quote = not in_quote
            if not in_quote and buf.strip():
                tokens.append(buf.strip())
                buf = ""
            continue
        if ch.isspace() and not in_quote:
            if buf.strip():
                tokens.append(buf.strip())
            buf = ""
            continue
        buf += ch
    if buf.strip():
        tokens.append(buf.strip())

    or_groups: List[List[str]] = []
    current: List[str] = []
    for tok in tokens:
        if tok == "OR":
            if current:
                or_groups.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        or_groups.append(current)
    return [g for g in or_groups if g]


def _snippet(text: str, term: str, span: int = 120) -> str:
    """A short excerpt centred on the first match of *term* (case-insensitive)."""
    low = text.lower()
    idx = low.find(term.lower())
    if idx < 0:
        return (text[:span] + ("…" if len(text) > span else "")).strip()
    start = max(0, idx - span // 2)
    end = min(len(text), idx + len(term) + span // 2)
    out = text[start:end].strip()
    if start > 0:
        out = "…" + out
    if end < len(text):
        out = out + "…"
    return out


def _discover(conn, query, role_filter, limit, sort, current_epoch) -> str:
    roles = role_filter or _DEFAULT_ROLES
    role_placeholders = ",".join("?" for _ in roles)
    or_groups = _parse_terms(query)
    if not or_groups:
        return json.dumps({
            "success": True, "mode": "discover", "query": query,
            "results": [], "count": 0, "message": "No matching sessions found.",
        }, ensure_ascii=False)

    try:
        rows = conn.execute(
            f"SELECT id, epoch, role, content, kind, ts FROM messages "
            f"WHERE kind IN (?,?,?,?) AND role IN ({role_placeholders}) "
            f"ORDER BY id",
            (*_READABLE_KINDS, *roles),
        ).fetchall()
    except sqlite3.Error as e:
        logger.error("session_search discovery failed: %s", e, exc_info=True)
        return tool_error(f"Search failed: {e}", success=False)

    # First match per epoch (the 'session'), honouring AND/OR term logic.
    hits: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        epoch = row["epoch"]
        if current_epoch is not None and epoch == current_epoch:
            continue  # already in active context
        if epoch in hits:
            continue
        text = _decode_content(row["content"], row["kind"])
        if not text:
            continue
        low = text.lower()
        matched_term = None
        for group in or_groups:  # OR across groups
            if all(t.lower() in low for t in group):  # AND within group
                matched_term = group[0]
                break
        if matched_term is None:
            continue
        hits[epoch] = {"row": row, "snippet": _snippet(text, matched_term)}

    ordered = sorted(hits.items(), key=lambda kv: kv[0],
                     reverse=(sort != "oldest"))
    if sort is None:
        # relevance-only would need a real index; default to newest-first.
        ordered = sorted(hits.items(), key=lambda kv: kv[0], reverse=True)
    ordered = ordered[:limit]

    results = []
    for epoch, hit in ordered:
        row = hit["row"]
        msg_id = row["id"]
        meta = _epoch_meta(conn, epoch)
        view = _anchored_view(conn, epoch, msg_id, window=5, bookend=3, roles=roles)
        results.append({
            "session_id": str(epoch),
            "when": _format_timestamp(meta.get("started_at")),
            "source": "transcript",
            "model": "unknown",
            "title": None,
            "matched_role": row["role"],
            "match_message_id": msg_id,
            "snippet": hit["snippet"],
            "bookend_start": view["bookend_start"],
            "messages": view["window"],
            "bookend_end": view["bookend_end"],
            "messages_before": view["messages_before"],
            "messages_after": view["messages_after"],
        })

    return json.dumps({
        "success": True, "mode": "discover", "query": query,
        "results": results, "count": len(results),
        "sessions_searched": len(hits),
    }, ensure_ascii=False)


def _anchored_view(conn, epoch, anchor_id, window, bookend, roles) -> Dict[str, Any]:
    """±window messages around an anchor + first/last `bookend` user+assistant
    messages of the epoch + the before/after remaining counts."""
    rows = conn.execute(
        "SELECT id, epoch, role, content, kind, ts FROM messages "
        "WHERE epoch=? AND kind IN (?,?,?,?) ORDER BY id",
        (epoch, *_READABLE_KINDS),
    ).fetchall()
    ids = [r["id"] for r in rows]
    if anchor_id in ids:
        pos = ids.index(anchor_id)
    else:
        pos = min(range(len(ids)), key=lambda i: abs(ids[i] - anchor_id)) if ids else 0
    lo = max(0, pos - window)
    hi = min(len(rows), pos + window + 1)
    win_rows = rows[lo:hi]
    bookend_rows = [r for r in rows if r["role"] in ("user", "assistant")]
    return {
        "window": [_shape_row(r, anchor_id) for r in win_rows],
        "bookend_start": [_shape_row(r) for r in bookend_rows[:bookend]],
        "bookend_end": [_shape_row(r) for r in bookend_rows[-bookend:]] if bookend_rows else [],
        "messages_before": lo,
        "messages_after": max(0, len(rows) - hi),
    }


def _scroll(conn, session_id, around_message_id, window, current_epoch) -> str:
    try:
        epoch = int(str(session_id).strip())
    except (TypeError, ValueError):
        return tool_error("scroll requires a numeric session_id (the epoch)", success=False)
    try:
        around_message_id = int(around_message_id)
    except (TypeError, ValueError):
        return tool_error("scroll requires integer around_message_id", success=False)
    if not isinstance(window, int):
        try:
            window = int(window)
        except (TypeError, ValueError):
            window = 5
    window = max(1, min(window, 20))

    if current_epoch is not None and epoch == current_epoch:
        return tool_error(
            "scroll rejected: that epoch is your current session (already in your active context)",
            success=False,
        )

    meta = _epoch_meta(conn, epoch)
    if meta["message_count"] == 0:
        return tool_error(f"session_id not found: {session_id}", success=False)

    rows = conn.execute(
        "SELECT id, epoch, role, content, kind, ts FROM messages "
        "WHERE epoch=? AND kind IN (?,?,?,?) ORDER BY id",
        (epoch, *_READABLE_KINDS),
    ).fetchall()
    ids = [r["id"] for r in rows]
    if around_message_id not in ids:
        return tool_error(
            f"around_message_id {around_message_id} not in session_id {session_id}",
            success=False,
        )
    pos = ids.index(around_message_id)
    lo = max(0, pos - window)
    hi = min(len(rows), pos + window + 1)
    win_rows = rows[lo:hi]

    return json.dumps({
        "success": True, "mode": "scroll", "session_id": str(epoch),
        "around_message_id": around_message_id,
        "session_meta": {
            "when": _format_timestamp(meta.get("started_at")),
            "source": "transcript", "model": "unknown", "title": None,
        },
        "window": window,
        "messages": [_shape_row(r, around_message_id) for r in win_rows],
        "messages_before": lo,
        "messages_after": max(0, len(rows) - hi),
    }, ensure_ascii=False)


def _read_session(conn, session_id, head=20, tail=10) -> str:
    try:
        epoch = int(str(session_id).strip())
    except (TypeError, ValueError):
        return tool_error(f"session_id not found: {session_id}", success=False)
    meta = _epoch_meta(conn, epoch)
    if meta["message_count"] == 0:
        return tool_error(f"session_id not found: {session_id}", success=False)

    rows = conn.execute(
        "SELECT id, epoch, role, content, kind, ts FROM messages "
        "WHERE epoch=? AND kind IN (?,?,?,?) ORDER BY id",
        (epoch, *_READABLE_KINDS),
    ).fetchall()
    shaped = [_shape_row(r) for r in rows]
    total = len(shaped)
    truncated = total > head + tail
    window = shaped[:head] + shaped[-tail:] if truncated else shaped

    response = {
        "success": True, "mode": "read", "session_id": str(epoch),
        "session_meta": {
            "when": _format_timestamp(meta.get("started_at")),
            "source": "transcript", "model": "unknown", "title": None,
        },
        "message_count": total, "truncated": truncated, "messages": window,
    }
    if truncated:
        response["message"] = (
            f"Session has {total} messages; showing first {head} + last {tail}. "
            "Pass around_message_id (any id above) to scroll the middle."
        )
    return json.dumps(response, ensure_ascii=False)


def _browse(conn, limit, current_epoch) -> str:
    epochs = conn.execute(
        "SELECT epoch, COUNT(*) AS n, MIN(ts) AS started, MAX(ts) AS last "
        "FROM messages WHERE kind IN (?,?,?,?) GROUP BY epoch "
        "ORDER BY last DESC",
        _READABLE_KINDS,
    ).fetchall()
    results = []
    for row in epochs:
        epoch = row["epoch"]
        if current_epoch is not None and epoch == current_epoch:
            continue
        preview_row = conn.execute(
            "SELECT content, kind FROM messages "
            "WHERE epoch=? AND role IN ('user','assistant') AND kind IN (?,?,?,?) "
            "ORDER BY id LIMIT 1",
            (epoch, *_READABLE_KINDS),
        ).fetchone()
        preview = ""
        if preview_row:
            preview = _decode_content(preview_row["content"], preview_row["kind"])[:160]
        results.append({
            "session_id": str(epoch),
            "title": None,
            "source": "transcript",
            "started_at": _format_timestamp(row["started"]),
            "last_active": _format_timestamp(row["last"]),
            "message_count": int(row["n"]),
            "preview": preview,
        })
        if len(results) >= limit:
            break
    return json.dumps({
        "success": True, "mode": "browse", "results": results, "count": len(results),
        "message": (
            f"Showing {len(results)} most recent sessions. Pass a query= to search, "
            "or session_id+around_message_id to scroll."
        ),
    }, ensure_ascii=False)


def session_search(args: dict, ctx) -> str:
    """Single-shape tool; mode inferred from which args are set."""
    transcript = ctx.transcript
    if transcript is None or not getattr(transcript, "available", False):
        return tool_error(
            "Session search is unavailable: no transcript database for this chara.",
            success=False,
        )

    conn = _connect_ro(transcript.path)
    if conn is None:
        return tool_error(
            "Session search is unavailable: could not open the transcript database.",
            success=False,
        )
    try:
        current_epoch = transcript.epoch()

        session_id = args.get("session_id")
        around_message_id = args.get("around_message_id")
        query = args.get("query") or ""
        sid_set = isinstance(session_id, str) and session_id.strip() or isinstance(session_id, int)

        # Scroll shape — explicit anchor beats any query.
        if sid_set and around_message_id is not None:
            return _scroll(conn, session_id, around_message_id,
                           args.get("window", 5), current_epoch)

        # Read shape — session_id with no anchor → dump the whole epoch.
        if sid_set:
            return _read_session(conn, session_id)

        # Limit clamp [1, 10].
        limit = args.get("limit", 3)
        if not isinstance(limit, int):
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = 3
        limit = max(1, min(limit, 10))

        # Browse shape — no query → recent epochs.
        if not isinstance(query, str) or not query.strip():
            return _browse(conn, limit, current_epoch)

        role_filter = args.get("role_filter")
        role_list: Optional[List[str]] = None
        if isinstance(role_filter, str) and role_filter.strip():
            role_list = [r.strip() for r in role_filter.split(",") if r.strip()]

        sort = args.get("sort")
        sort_norm: Optional[str] = None
        if isinstance(sort, str):
            candidate = sort.strip().lower()
            if candidate in ("newest", "oldest"):
                sort_norm = candidate

        return _discover(conn, query.strip(), role_list, limit, sort_norm, current_epoch)
    finally:
        conn.close()


def check_session_search_requirements() -> bool:
    """No hard external requirement — gated at call time on transcript presence."""
    return True


SESSION_SEARCH_SCHEMA = {
    "description": (
        "Search past sessions stored in the local session DB, or scroll inside one. "
        "Keyword retrieval over the SQLite message store. No LLM calls — every "
        "shape returns actual messages from the DB. (OpenCharaAgent: a 'session' is a "
        "transcript epoch — the /reset boundary; session_id is the epoch number.)\n\n"
        "FOUR CALLING SHAPES\n\n"
        "  1) DISCOVERY — pass `query`:\n"
        "     session_search(query=\"auth refactor\", limit=3)\n"
        "     Searches messages, dedupes hits by session, returns the top N sessions. "
        "Each result carries:\n"
        "       - session_id, title, when, source\n"
        "       - snippet: match excerpt\n"
        "       - bookend_start: first 3 user+assistant messages of the session "
        "(the goal / kickoff)\n"
        "       - messages: ±5 messages around the match, with the anchor message "
        "flagged (the hit in context)\n"
        "       - bookend_end: last 3 user+assistant messages of the session "
        "(the resolution / decisions)\n"
        "       - match_message_id, messages_before, messages_after\n"
        "     Bookends + window together let you reconstruct goal → match → resolution "
        "without paying for the whole transcript.\n\n"
        "  2) SCROLL — pass `session_id` + `around_message_id`:\n"
        "     session_search(session_id=\"...\", around_message_id=12345, window=10)\n"
        "     Returns a window of ±`window` messages centered on the anchor. No "
        "bookends — just the slice. Use after a discovery call when you need more "
        "context than the ±5 default window.\n"
        "       - To scroll FORWARD: pass messages[-1].id back as around_message_id.\n"
        "       - To scroll BACKWARD: pass messages[0].id back as around_message_id.\n"
        "       - When messages_before or messages_after is < window, you're at the "
        "start or end of the session.\n\n"
        "  3) READ — pass `session_id` only (no around_message_id):\n"
        "     session_search(session_id=\"...\")\n"
        "     Dumps the whole session by id (first 20 + last 10 messages when "
        "large).\n\n"
        "  4) BROWSE — no args:\n"
        "     session_search()\n"
        "     Returns recent sessions chronologically: titles, previews, timestamps. "
        "Use when the user asks \"what was I working on\" without naming a topic.\n\n"
        "QUERY SYNTAX\n\n"
        "  AND is the default — multi-word queries require all terms. Use OR explicitly "
        "for broader recall (`alpha OR beta OR gamma`) or quoted phrases for exact "
        "match (`\"docker networking\"`).\n\n"
        "WHEN TO USE\n\n"
        "  Reach for this on any \"what did we do about X\" / \"where did we leave Y\" / "
        "\"find the session where Z\" question — before web search or filesystem "
        "inspection. The session DB carries what was said when; external tools show "
        "current world state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query (discovery shape). Keywords, phrases, or boolean "
                    "expressions to find in past sessions. Omit to browse recent "
                    "sessions. Ignored when session_id + around_message_id are set "
                    "(scroll shape)."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Discovery shape only. Max sessions to return (default 3, max 10). "
                    "Bump to 5–10 when the topic likely spans several sessions and you "
                    "want to pick the right one to scroll into."
                ),
                "default": 3,
            },
            "sort": {
                "type": "string",
                "enum": ["newest", "oldest"],
                "description": (
                    "Discovery shape only. Temporal bias. Set 'newest' for recency-shaped "
                    "questions (\"where did we leave X\"), 'oldest' for origin-shaped "
                    "questions (\"how did X start\"). Ignored in scroll and browse shapes."
                ),
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Scroll/read shape. Session (epoch) to read inside. Use the "
                    "session_id returned from a prior discovery call. Pair with "
                    "around_message_id to scroll, or pass alone to read the whole session."
                ),
            },
            "around_message_id": {
                "type": "integer",
                "description": (
                    "Scroll shape. Message id to center the window on. From a discovery "
                    "result use match_message_id, or any id seen in a prior window. To "
                    "scroll forward pass the last window message's id; to scroll "
                    "backward pass the first."
                ),
            },
            "window": {
                "type": "integer",
                "description": (
                    "Scroll shape only. Messages to return on each side of the anchor "
                    "(anchor itself always included). Clamped to [1, 20]. Default 5."
                ),
                "default": 5,
            },
            "role_filter": {
                "type": "string",
                "description": (
                    "Optional. Comma-separated roles to include. Discovery defaults to "
                    "'user,assistant' (tool output is usually noise). Pass "
                    "'user,assistant,tool' to include tool output or 'tool' to search "
                    "tool output only."
                ),
            },
        },
        "required": [],
    },
}


registry.register(
    "session_search", "session_search", SESSION_SEARCH_SCHEMA, session_search,
    check_fn=check_session_search_requirements, emoji="🔍",
)
