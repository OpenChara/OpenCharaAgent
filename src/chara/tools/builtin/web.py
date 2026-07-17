"""web_search + web_extract — faithful port of hermes-agent ``tools/web_tools.py``
(the registered ``web_search`` / ``web_extract`` surface), re-implemented against
OpenCharaAgent's runtime.

Schema, caps, guards, and return shapes are hermes-identical (the model is
post-trained on these shapes). The two infra couplings hermes has and OpenCharaAgent
lacks are mapped per ``.codex-fleet/seam-chara.md``:

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

logger = logging.getLogger("chara.tools.web")

# ---- size thresholds (hermes web_tools.py:367-371, verbatim) ----
MAX_CONTENT_SIZE = 2_000_000        # refuse to process above this
CHUNK_THRESHOLD = 500_000           # chunk above this
CHUNK_SIZE = 100_000                # chars per chunk
MAX_OUTPUT_SIZE = 5000              # hard cap on final summarized output
DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION = 5000

_HTTP_TIMEOUT = 30                  # seconds per fetch
_USER_AGENT = "OpenCharaAgent-web/1.0 (+https://github.com/lunamos)"


# ===========================================================================
# Backend resolution (env / Settings) — pick ONE, no fake results.
# ===========================================================================

def _env(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


def _searxng_url() -> str:
    return _env("CHARA_SEARXNG_URL") or _env("SEARXNG_URL")


def _serper_key() -> str:
    return _env("CHARA_SERPER_API_KEY") or _env("SERPER_API_KEY")


def _brave_key() -> str:
    return _env("CHARA_BRAVE_API_KEY") or _env("BRAVE_API_KEY") or _env("BRAVE_SEARCH_API_KEY")


def _resolve_search_backend() -> str:
    """Return the active search backend name. DuckDuckGo (keyless) is the default
    fallback so web_search ALWAYS works with no configuration; a configured
    SearXNG/Serper/Brave backend takes precedence when present.

    Honors an explicit ``CHARA_WEB_SEARCH_BACKEND`` override; otherwise
    auto-detects in a stable order (searxng → serper → brave)."""
    forced = _env("CHARA_WEB_SEARCH_BACKEND").lower()
    if forced:
        return forced if _backend_configured(forced) else "duckduckgo"
    for backend in ("searxng", "serper", "brave"):
        if _backend_configured(backend):
            return backend
    return "duckduckgo"  # keyless default — always available


def _backend_configured(backend: str) -> bool:
    if backend == "searxng":
        return bool(_searxng_url())
    if backend == "serper":
        return bool(_serper_key())
    if backend == "brave":
        return bool(_brave_key())
    if backend == "duckduckgo":
        return True
    return False


def check_web_api_key() -> bool:
    """check_fn: web tools are always available (DuckDuckGo needs no key; a
    configured SearXNG/Serper/Brave backend just upgrades search quality).
    Network-off is handled at call time, not by hiding the tool."""
    return True


def _missing_config_error() -> str:
    return tool_error(
        "No web search provider configured. Set one of: CHARA_SEARXNG_URL "
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
            "Network is off. Ask the operator to enable it (/net on) before "
            "searching the web.",
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

    try:
        if backend == "searxng":
            results = _search_searxng(query, limit)
        elif backend == "serper":
            results = _search_serper(query, limit)
        elif backend == "brave":
            results = _search_brave(query, limit)
        else:  # keyless default (and the fallback for an unknown override)
            results = _search_duckduckgo(query, limit)
    except urllib.error.HTTPError as e:
        return tool_error(f"Error searching web: HTTP {e.code} from {backend} backend")
    except Exception as e:  # noqa: BLE001
        logger.warning("web_search failed: %s", e)
        return tool_error(f"Error searching web: {type(e).__name__}: {e}")

    return tool_result({"success": True, "data": {"web": results[:limit]}})


# DuckDuckGo serves a 202 JS-challenge page (zero parseable results) to the bare
# library User-Agent; a realistic browser UA gets the real HTML. Still best-effort
# — DDG can rate-limit/challenge at any time, which is why a configured backend
# (SearXNG/Serper/Brave) is the reliable path.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _search_duckduckgo(query: str, limit: int) -> list[dict]:
    """Keyless web search via DuckDuckGo's HTML endpoints (no bs4 dep) — the
    default when no SearXNG/Serper/Brave backend is set.

    Tries the full HTML endpoint, then the lite endpoint. RAISES RuntimeError
    when DDG is blocked/unparseable (HTTP 202 challenge, page-shape change, rate
    limit) so the tool surfaces a VISIBLE error — never a misleading empty
    'success'. Returns [] ONLY for a genuine zero-hit results page."""
    qs = urllib.parse.urlencode({"q": query})
    last_status = 0
    for url, parse in (
        (f"https://html.duckduckgo.com/html/?{qs}", _parse_ddg_html),
        (f"https://lite.duckduckgo.com/lite/?{qs}", _parse_ddg_lite),
    ):
        try:
            status, body, _ = _http_get(url, headers={"User-Agent": _BROWSER_UA})
        except urllib.error.HTTPError as e:
            last_status = e.code  # 429/403 on one endpoint — try the next, then raise
            continue
        last_status = status
        if status != 200:
            continue  # 202 challenge — try the next endpoint
        html = body.decode("utf-8", errors="replace")
        results = parse(html, limit)
        if results:
            return results
        if _ddg_says_no_results(html):
            return []  # genuine zero-result page, honestly empty
        # HTTP 200 but nothing parseable and no "no results" marker → challenged
        # or the page shape changed; fall through to the next endpoint, then raise.
    raise RuntimeError(
        f"DuckDuckGo returned no parseable results (last HTTP {last_status}) — it is "
        "likely rate-limiting or challenging requests. Configure a search backend "
        "(CHARA_SEARXNG_URL, SERPER_API_KEY, or BRAVE_API_KEY) for reliable search."
    )


def _parse_ddg_html(html: str, limit: int) -> list[dict]:
    """Parse the full html.duckduckgo.com result markup (result__a / result__snippet)."""
    link_re = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    snip_re = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.S)
    snippets = snip_re.findall(html)
    out: list[dict] = []
    for i, (href, title) in enumerate(link_re.findall(html)):
        if i >= limit:
            break
        out.append({
            "title": _strip_html(title),
            "url": _ddg_unwrap(href),
            "description": _strip_html(snippets[i]) if i < len(snippets) else "",
            "position": i + 1,
        })
    return out


def _parse_ddg_lite(html: str, limit: int) -> list[dict]:
    """Parse the lite.duckduckgo.com fallback markup (anchors with class result-link)."""
    link_re = re.compile(r'<a[^>]*class=[\'"]result-link[\'"][^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    snip_re = re.compile(r'<td[^>]*class=[\'"]result-snippet[\'"][^>]*>(.*?)</td>', re.S)
    snippets = snip_re.findall(html)
    out: list[dict] = []
    for i, (href, title) in enumerate(link_re.findall(html)):
        if i >= limit:
            break
        out.append({
            "title": _strip_html(title),
            "url": _ddg_unwrap(href),
            "description": _strip_html(snippets[i]) if i < len(snippets) else "",
            "position": i + 1,
        })
    return out


def _ddg_says_no_results(html: str) -> bool:
    """True when DDG's page explicitly reports zero hits (vs a challenge/shape change)."""
    low = html.lower()
    return "no-results" in low or "no results." in low or "no results found" in low


def _ddg_unwrap(href: str) -> str:
    """DuckDuckGo HTML links are //duckduckgo.com/l/?uddg=<encoded-target>&… —
    pull the real target out; pass through anything already absolute."""
    if "uddg=" in href:
        try:
            q = urllib.parse.urlparse(href if "//" in href else "https:" + href).query
            uddg = urllib.parse.parse_qs(q).get("uddg", [""])[0]
            if uddg:
                return urllib.parse.unquote(uddg)
        except Exception:  # noqa: BLE001
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&amp;", "&").replace("&#x27;", "'").replace("&quot;", '"').strip()


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
            "Network is off. Ask the operator to enable it (/net on) before "
            "extracting web content.",
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

# SHELVED 2026-06-17 (owner): the web tools are intentionally NOT registered, so
# no toolpack can surface them and the agent never sees them. They want a proper
# search backend/key set up first, which we're deferring. The implementations
# above (web_search / web_extract / the backends) stay intact — flip this flag to
# True to bring them back (and re-add them to a toolpack).
_WEB_TOOLS_ENABLED = False

if _WEB_TOOLS_ENABLED:
    registry.register(
        "web_search", "web", WEB_SEARCH_SCHEMA, web_search,
        check_fn=check_web_api_key, emoji="🔍", max_result_size_chars=100_000,
    )
    registry.register(
        "web_extract", "web", WEB_EXTRACT_SCHEMA, web_extract,
        check_fn=check_web_api_key, emoji="📄", max_result_size_chars=100_000,
    )
