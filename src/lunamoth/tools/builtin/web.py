"""web_search + web_extract — faithful port of hermes-agent ``tools/web_tools.py``
(the registered ``web_search`` / ``web_extract`` surface), re-implemented against
LunaMoth's runtime.

Schema, caps, guards, and return shapes are hermes-identical (the model is
post-trained on these shapes). The two infra couplings hermes has and LunaMoth
lacks are mapped per ``.codex-fleet/seam-lunamoth.md``:

- **Multi-vendor search plugin registry** → one backend resolved from env /
  Settings (SearXNG, Serper, or Brave). No key configured → a clear tool_error
  naming the missing config; NEVER fabricated results.
- **Auxiliary summarizer LLM** → the chara's own one OpenAI-compatible client
  (``ctx.llm.raw_complete``, non-streaming). When no LLM is configured the
  extract degrades to raw-truncation — the one allowed backstop (like
  compaction→trim), with the 5000-char cap / 2M refuse constants kept verbatim.

Network is gated: if ``not ctx.network_on()`` both tools return a tool_error
telling the model network is off (``/net on`` / request_permission).
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from ..registry import registry, tool_error, tool_result
from ._url_safety import contains_secret, is_safe_url

logger = logging.getLogger("lunamoth.tools.web")

# ---- size thresholds (hermes web_tools.py:367-371, verbatim) ----
MAX_CONTENT_SIZE = 2_000_000        # refuse to process above this
CHUNK_THRESHOLD = 500_000           # chunk above this
CHUNK_SIZE = 100_000                # chars per chunk
MAX_OUTPUT_SIZE = 5000              # hard cap on final summarized output
DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION = 5000

_HTTP_TIMEOUT = 30                  # seconds per fetch
_USER_AGENT = "LunaMoth-web/1.0 (+https://github.com/lunamos)"


# ===========================================================================
# Backend resolution (env / Settings) — pick ONE, no fake results.
# ===========================================================================

def _env(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


def _searxng_url() -> str:
    return _env("LUNAMOTH_SEARXNG_URL") or _env("SEARXNG_URL")


def _serper_key() -> str:
    return _env("LUNAMOTH_SERPER_API_KEY") or _env("SERPER_API_KEY")


def _brave_key() -> str:
    return _env("LUNAMOTH_BRAVE_API_KEY") or _env("BRAVE_API_KEY") or _env("BRAVE_SEARCH_API_KEY")


def _resolve_search_backend() -> str:
    """Return the active search backend name, or "" when none is configured.

    Honors an explicit ``LUNAMOTH_WEB_SEARCH_BACKEND`` override; otherwise
    auto-detects in a stable order (searxng → serper → brave)."""
    forced = _env("LUNAMOTH_WEB_SEARCH_BACKEND").lower()
    if forced:
        return forced if _backend_configured(forced) else ""
    for backend in ("searxng", "serper", "brave"):
        if _backend_configured(backend):
            return backend
    return ""


def _backend_configured(backend: str) -> bool:
    if backend == "searxng":
        return bool(_searxng_url())
    if backend == "serper":
        return bool(_serper_key())
    if backend == "brave":
        return bool(_brave_key())
    return False


def check_web_api_key() -> bool:
    """check_fn: a web search backend is configured (any of the supported)."""
    return bool(_resolve_search_backend())


def _missing_config_error() -> str:
    return tool_error(
        "No web search provider configured. Set one of: LUNAMOTH_SEARXNG_URL "
        "(a SearXNG instance), SERPER_API_KEY (serper.dev), or BRAVE_API_KEY "
        "(Brave Search API).",
        success=False,
    )


# ---- the raw HTTP GET (honors isolation by going through urllib in-process) --

def _http_get(url: str, headers: dict | None = None, timeout: int = _HTTP_TIMEOUT) -> tuple[int, bytes, str]:
    """GET *url*. Returns (status, body_bytes, content_type). Raises on transport
    error. The caller is responsible for SSRF / secret guards BEFORE calling."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "") or ""
        return resp.status, body, ctype


# ===========================================================================
# web_search
# ===========================================================================

def web_search(args, ctx) -> str:
    """Search the web. Returns metadata only (title/url/description/position)."""
    if not ctx.network_on():
        return tool_error(
            "Network is off. Ask the operator to enable it (/net on) or use "
            "request_permission before searching the web.",
            success=False,
        )

    query = str(args.get("query") or "").strip()
    if not query:
        return tool_error("query is required", success=False)

    # limit coerced to int, clamped [1,100] (hermes :818-822)
    try:
        limit = int(args.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(100, limit))

    backend = _resolve_search_backend()
    if not backend:
        return _missing_config_error()

    try:
        if backend == "searxng":
            results = _search_searxng(query, limit)
        elif backend == "serper":
            results = _search_serper(query, limit)
        elif backend == "brave":
            results = _search_brave(query, limit)
        else:  # configured override naming an unknown backend
            return _missing_config_error()
    except urllib.error.HTTPError as e:
        return tool_error(f"Error searching web: HTTP {e.code} from {backend} backend")
    except Exception as e:  # noqa: BLE001
        logger.warning("web_search failed: %s", e)
        return tool_error(f"Error searching web: {type(e).__name__}: {e}")

    return tool_result({"success": True, "data": {"web": results[:limit]}})


def _search_searxng(query: str, limit: int) -> list[dict]:
    base = _searxng_url().rstrip("/")
    qs = urllib.parse.urlencode({"q": query, "format": "json"})
    status, body, _ = _http_get(f"{base}/search?{qs}")
    payload = json.loads(body.decode("utf-8", errors="replace"))
    out: list[dict] = []
    for i, r in enumerate(payload.get("results", [])[:limit]):
        out.append({
            "title": r.get("title", "") or "",
            "url": r.get("url", "") or "",
            "description": r.get("content", "") or "",
            "position": i + 1,
        })
    return out


def _search_serper(query: str, limit: int) -> list[dict]:
    data = json.dumps({"q": query, "num": min(limit, 100)}).encode("utf-8")
    req = urllib.request.Request(
        "https://google.serper.dev/search", data=data,
        headers={"X-API-KEY": _serper_key(), "Content-Type": "application/json",
                 "User-Agent": _USER_AGENT}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    out: list[dict] = []
    for i, r in enumerate(payload.get("organic", [])[:limit]):
        out.append({
            "title": r.get("title", "") or "",
            "url": r.get("link", "") or "",
            "description": r.get("snippet", "") or "",
            "position": r.get("position", i + 1),
        })
    return out


def _search_brave(query: str, limit: int) -> list[dict]:
    qs = urllib.parse.urlencode({"q": query, "count": min(limit, 20)})
    status, body, _ = _http_get(
        f"https://api.search.brave.com/res/v1/web/search?{qs}",
        headers={"X-Subscription-Token": _brave_key(), "Accept": "application/json"},
    )
    payload = json.loads(body.decode("utf-8", errors="replace"))
    out: list[dict] = []
    for i, r in enumerate((payload.get("web", {}) or {}).get("results", [])[:limit]):
        out.append({
            "title": r.get("title", "") or "",
            "url": r.get("url", "") or "",
            "description": r.get("description", "") or "",
            "position": i + 1,
        })
    return out


# ===========================================================================
# web_extract
# ===========================================================================

def web_extract(args, ctx) -> str:
    """Extract page content as markdown. PDF best-effort. Per-page LLM summary
    when large; raw-truncation backstop when no LLM is configured."""
    if not ctx.network_on():
        return tool_error(
            "Network is off. Ask the operator to enable it (/net on) or use "
            "request_permission before extracting web content.",
            success=False,
        )

    raw_urls = args.get("urls")
    urls = [u for u in raw_urls if isinstance(u, str)] if isinstance(raw_urls, list) else []
    urls = urls[:5]
    if not urls:
        return tool_error("urls is required (a list of up to 5 page URLs)", success=False)

    results: list[dict] = []
    for url in urls:
        results.append(_extract_one(url, ctx))

    if not any(r.get("content") for r in results):
        # All failed/blocked — hermes returns a tool_error here (:1151-1154).
        return tool_error("Content was inaccessible or not found")

    return tool_result({"results": results})


def _extract_one(url: str, ctx) -> dict:
    url = (url or "").strip()
    trimmed = {"url": url, "title": "", "content": "", "error": None}

    # 1. Secret-in-URL guard BEFORE any fetch (hermes :920-938)
    if contains_secret(url):
        trimmed["error"] = (
            "Blocked: URL contains what appears to be an API key or token. "
            "Refusing to send it on the wire."
        )
        trimmed["blocked_by_policy"] = True
        return trimmed

    # 2. SSRF guard (hermes :960-970)
    if not is_safe_url(url):
        trimmed["error"] = "Blocked: URL targets a private/internal address or uses an unsupported scheme."
        trimmed["blocked_by_policy"] = True
        return trimmed

    # 3. Fetch
    try:
        status, body, ctype = _http_get(url)
    except urllib.error.HTTPError as e:
        trimmed["error"] = f"HTTP {e.code}"
        return trimmed
    except Exception as e:  # noqa: BLE001
        trimmed["error"] = f"Fetch failed: {type(e).__name__}: {e}"
        return trimmed

    ctype_l = ctype.lower()
    if "application/pdf" in ctype_l or url.lower().endswith(".pdf"):
        raw, title = _pdf_to_markdown(body)
    else:
        raw, title = _html_to_markdown(body)

    trimmed["title"] = title

    # 4. LLM summarization / truncation (hermes process_content_with_llm)
    processed = _process_content(raw, url=url, title=title, ctx=ctx)
    trimmed["content"] = processed if processed is not None else raw
    return trimmed


# ---- html / pdf → markdown (best-effort, no hard third-party dep) ----------

def _html_to_markdown(body: bytes) -> tuple[str, str]:
    text = body.decode("utf-8", errors="replace")
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        title = _html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())

    # Drop non-content blocks entirely.
    for tag in ("script", "style", "noscript", "template", "svg", "head"):
        text = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", text, flags=re.IGNORECASE | re.DOTALL)

    # Lightweight block → newline conversion so paragraphs survive.
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)\s*[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    # Strip remaining tags.
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    # Collapse whitespace; keep paragraph breaks.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, title


def _pdf_to_markdown(body: bytes) -> tuple[str, str]:
    """Best-effort PDF → text. Uses pypdf/PyPDF2 if importable, else a marker."""
    import io
    for modname, cls in (("pypdf", "PdfReader"), ("PyPDF2", "PdfReader")):
        try:
            mod = __import__(modname, fromlist=[cls])
            reader = getattr(mod, cls)(io.BytesIO(body))
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001
                    continue
            text = "\n\n".join(p.strip() for p in pages if p.strip())
            if text:
                return text, ""
        except Exception:  # noqa: BLE001
            continue
    return ("[PDF content could not be extracted — no PDF reader available in "
            "this environment. Try the browser tool or terminal for this URL.]", "")


# ---- content processing (hermes process_content_with_llm, :339-436) --------

def _process_content(content: str, *, url: str, title: str, ctx) -> str | None:
    """Return processed markdown, or None to signal 'use raw as-is'.

    - content < 5000 → None (return raw, hermes :382-384)
    - content > 2M → refusal marker string (hermes :367,376-379)
    - else → single LLM summarization pass, output hard-capped at 5000 chars;
      on LLM failure / no LLM → raw-truncation backstop (hermes :418-436).
    """
    content_len = len(content)
    if content_len > MAX_CONTENT_SIZE:
        size_mb = content_len / 1_000_000
        return f"[Content too large to process: {size_mb:.1f}MB. Try a more focused source URL.]"
    if content_len < DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION:
        return None

    summary = _summarize(content, url=url, title=title, ctx=ctx)
    if summary:
        if len(summary) > MAX_OUTPUT_SIZE:
            summary = summary[:MAX_OUTPUT_SIZE] + "\n\n[... summary truncated for context management ...]"
        return summary

    # Backstop: truncated raw content (the one allowed degrade — like
    # compaction→trim). Never a fake success, never a useless error string.
    truncated = content[:MAX_OUTPUT_SIZE]
    if content_len > MAX_OUTPUT_SIZE:
        truncated += (
            f"\n\n[Content truncated — showing first {MAX_OUTPUT_SIZE:,} of "
            f"{content_len:,} chars. LLM summarization was unavailable. "
            f"Use the browser or terminal for the full page.]"
        )
    return truncated


_SUMMARY_SYSTEM = (
    "You are an expert content analyst. Your job is to process web content and "
    "create a comprehensive yet concise summary that preserves all important "
    "information while dramatically reducing bulk.\n\n"
    "Create a well-structured markdown summary that includes:\n"
    "1. Key excerpts (quotes, code snippets, important facts) in their original format\n"
    "2. Comprehensive summary of all other important information\n"
    "3. Proper markdown formatting with headers, bullets, and emphasis\n\n"
    "Your goal is to preserve ALL important information while reducing length. "
    "Never lose key facts, figures, insights, or actionable information."
)


def _summarize(content: str, *, url: str, title: str, ctx) -> str | None:
    """One non-streaming summarization pass through ctx.llm.raw_complete.
    Returns None when no LLM is configured or the call fails (caller degrades
    to truncation)."""
    llm = getattr(ctx, "llm", None)
    if llm is None or not hasattr(llm, "raw_complete"):
        return None

    # Chunk very large content so a single request stays bounded; the chunk
    # summaries are concatenated and the final cap is applied by the caller.
    if len(content) > CHUNK_THRESHOLD:
        parts: list[str] = []
        for start in range(0, min(len(content), MAX_CONTENT_SIZE), CHUNK_SIZE):
            chunk = content[start:start + CHUNK_SIZE]
            piece = _summarize_chunk(chunk, url=url, title=title, ctx=ctx)
            if piece:
                parts.append(piece)
        return "\n\n".join(parts) if parts else None

    return _summarize_chunk(content, url=url, title=title, ctx=ctx)


def _summarize_chunk(content: str, *, url: str, title: str, ctx) -> str | None:
    ctx_info = []
    if title:
        ctx_info.append(f"Title: {title}")
    if url:
        ctx_info.append(f"Source: {url}")
    ctx_str = ("\n".join(ctx_info) + "\n\n") if ctx_info else ""
    user = (
        "Please process this web content and create a comprehensive markdown "
        f"summary:\n\n{ctx_str}CONTENT TO PROCESS:\n{content}\n\n"
        "Create a markdown summary that captures all key information in a "
        "well-organized, scannable format. Include important quotes and code "
        "snippets in their original formatting."
    )
    try:
        out = ctx.llm.raw_complete(
            [{"role": "system", "content": _SUMMARY_SYSTEM},
             {"role": "user", "content": user}],
            max_tokens=4096,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("web_extract summarization failed: %s", e)
        return None
    out = (out or "").strip()
    return out or None


# ===========================================================================
# Registration
# ===========================================================================

WEB_SEARCH_SCHEMA = {
    "description": (
        "Search the web for information. Returns up to 5 results by default with "
        "titles, URLs, and descriptions. The query is passed through to the "
        "configured backend, so operators such as site:domain, filetype:pdf, "
        "intitle:word, -term, and \"exact phrase\" may work when the backend "
        "supports them."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The search query to look up on the web. You may include "
                    "backend-supported operators such as site:example.com, "
                    "filetype:pdf, intitle:word, -term, or \"exact phrase\"."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

WEB_EXTRACT_SCHEMA = {
    "description": (
        "Extract content from web page URLs. Returns page content in markdown "
        "format. Also works with PDF URLs (arxiv papers, documents, etc.) — pass "
        "the PDF link directly and it converts to markdown text. Pages under 5000 "
        "chars return full markdown; larger pages are LLM-summarized and capped at "
        "~5000 chars per page. Pages over 2M chars are refused. If a URL fails or "
        "times out, use the browser tool to access it instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5,
            },
        },
        "required": ["urls"],
    },
}

registry.register(
    "web_search", "web", WEB_SEARCH_SCHEMA, web_search,
    check_fn=check_web_api_key, emoji="🔍", max_result_size_chars=100_000,
)
registry.register(
    "web_extract", "web", WEB_EXTRACT_SCHEMA, web_extract,
    check_fn=check_web_api_key, emoji="📄", max_result_size_chars=100_000,
)
