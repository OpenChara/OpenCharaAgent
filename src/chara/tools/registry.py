"""Central registry for all OpenCharaAgent builtin tools — ported from hermes-agent
(reference/hermes-agent/tools/registry.py), apple-to-apple, adapted to
OpenCharaAgent's runtime in two ways only:

1. Handlers take ``(args: dict, ctx: ToolContext) -> str`` and return a JSON
   STRING (hermes' handler contract). The runtime touchpoints (sandbox, state,
   shell, llm, transcript, memory, polaris, task, skills, mcp) ride on ``ctx`` injected
   at dispatch time — hermes threads a ``task_id`` and looks up a per-task
   environment; OpenCharaAgent is one-process-one-chara so there is exactly one ctx.
2. The dispatch error sanitizer and the default result-size are local (hermes
   imported them from model_tools / budget_config).

Each builtin tool file under ``tools/builtin/`` calls ``registry.register(...)``
at module import; ``discover_builtin_tools()`` AST-scans + imports them.
"""
from __future__ import annotations

import ast
import importlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("chara.tools.registry")

# Global default cap on a tool result's character length (hermes:
# budget_config.DEFAULT_RESULT_SIZE_CHARS). Tools may override per-entry.
DEFAULT_RESULT_SIZE_CHARS = 100_000


# ---------------------------------------------------------------------------
# Self-registration discovery (hermes registry.py:29-74)
# ---------------------------------------------------------------------------

def _is_registry_register_call(node: ast.AST) -> bool:
    """True when *node* is a top-level ``registry.register(...)`` call."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "register"
        and isinstance(func.value, ast.Name)
        and func.value.id == "registry"
    )


def _module_registers_tools(module_path: Path) -> bool:
    try:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    except (OSError, SyntaxError):
        return False
    return any(_is_registry_register_call(stmt) for stmt in tree.body)


def discover_builtin_tools(tools_dir: Optional[Path] = None) -> list[str]:
    """Import self-registering tool modules under ``tools/builtin/`` and return
    their imported module names. Modules that fail to import are logged and
    skipped (one bad tool never takes down the whole tool layer)."""
    base = Path(tools_dir) if tools_dir is not None else Path(__file__).resolve().parent / "builtin"
    if not base.is_dir():
        return []
    names = [
        f"chara.tools.builtin.{path.stem}"
        for path in sorted(base.glob("*.py"))
        if path.name != "__init__.py" and _module_registers_tools(path)
    ]
    imported: list[str] = []
    for mod in names:
        try:
            importlib.import_module(mod)
            imported.append(mod)
        except Exception as e:  # noqa: BLE001 - one bad tool must not break the rest
            logger.warning("could not import tool module %s: %s", mod, e)
    return imported


# ---------------------------------------------------------------------------
# Tool entry + check_fn TTL cache (hermes registry.py:77-148)
# ---------------------------------------------------------------------------

class ToolEntry:
    """Metadata for a single registered tool."""

    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn",
        "requires_env", "description", "emoji",
        "max_result_size_chars", "dynamic_schema_overrides",
    )

    def __init__(self, name, toolset, schema, handler, check_fn,
                 requires_env, description, emoji,
                 max_result_size_chars=None, dynamic_schema_overrides=None):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.requires_env = requires_env or []
        self.description = description
        self.emoji = emoji
        self.max_result_size_chars = max_result_size_chars
        self.dynamic_schema_overrides = dynamic_schema_overrides


_CHECK_FN_TTL_SECONDS = 30.0
_check_fn_cache: dict[Callable, tuple[float, bool]] = {}
_check_fn_cache_lock = threading.Lock()


def _check_fn_cached(fn: Callable) -> bool:
    """bool(fn()), TTL-cached ~30 s. check_fns probe external state (docker
    daemon, the agent-browser/Chromium binary) so caching amortizes the probe;
    the TTL keeps a flipped capability propagating within a turn or two."""
    now = time.monotonic()
    with _check_fn_cache_lock:
        cached = _check_fn_cache.get(fn)
        if cached is not None and now - cached[0] < _CHECK_FN_TTL_SECONDS:
            return cached[1]
    try:
        value = bool(fn())
    except Exception:  # noqa: BLE001
        value = False
    with _check_fn_cache_lock:
        _check_fn_cache[fn] = (now, value)
    return value


def invalidate_check_fn_cache() -> None:
    with _check_fn_cache_lock:
        _check_fn_cache.clear()


# ---------------------------------------------------------------------------
# The registry singleton (hermes registry.py:151-544)
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Collects tool schemas + handlers from builtin tool files."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._toolset_checks: dict[str, Callable] = {}
        self._lock = threading.RLock()
        self._generation = 0

    def _snapshot(self) -> list[ToolEntry]:
        with self._lock:
            return list(self._tools.values())

    # ---- registration ----
    def register(
        self, name: str, toolset: str, schema: dict, handler: Callable,
        check_fn: Callable | None = None, requires_env: list | None = None,
        description: str = "", emoji: str = "",
        max_result_size_chars: int | float | None = None,
        dynamic_schema_overrides: Callable | None = None, override: bool = False,
    ) -> None:
        """Register a tool. Called at module-import time by each tool file.
        A shadowing registration from a different toolset is rejected unless
        ``override=True`` (or both are MCP toolsets — server refresh)."""
        with self._lock:
            existing = self._tools.get(name)
            if existing and existing.toolset != toolset:
                both_mcp = existing.toolset.startswith("mcp-") and toolset.startswith("mcp-")
                if not (both_mcp or override):
                    logger.error(
                        "tool registration REJECTED: '%s' (toolset '%s') would shadow "
                        "toolset '%s'; pass override=True if intentional", name, toolset, existing.toolset)
                    return
            self._tools[name] = ToolEntry(
                name=name, toolset=toolset, schema=schema, handler=handler,
                check_fn=check_fn, requires_env=requires_env,
                description=description or schema.get("description", ""),
                emoji=emoji, max_result_size_chars=max_result_size_chars,
                dynamic_schema_overrides=dynamic_schema_overrides,
            )
            if check_fn and toolset not in self._toolset_checks:
                self._toolset_checks[toolset] = check_fn
            self._generation += 1

    def deregister(self, name: str) -> None:
        with self._lock:
            entry = self._tools.pop(name, None)
            if entry is None:
                return
            if not any(e.toolset == entry.toolset for e in self._tools.values()):
                self._toolset_checks.pop(entry.toolset, None)
            self._generation += 1

    # ---- schema retrieval ----
    def get_definitions(self, tool_names, quiet: bool = False) -> list[dict]:
        """OpenAI-format function schemas for the requested names, filtered by
        each tool's (TTL-cached) check_fn and with dynamic schema overrides
        applied. Output: ``[{"type":"function","function":{...,"name":...}}]``."""
        result: list[dict] = []
        check_results: dict[Callable, bool] = {}
        entries = {e.name: e for e in self._snapshot()}
        for name in sorted(tool_names):
            entry = entries.get(name)
            if not entry:
                continue
            if entry.check_fn:
                if entry.check_fn not in check_results:
                    check_results[entry.check_fn] = _check_fn_cached(entry.check_fn)
                if not check_results[entry.check_fn]:
                    if not quiet:
                        logger.debug("tool %s unavailable (check failed)", name)
                    continue
            schema = {**entry.schema, "name": entry.name}
            if entry.dynamic_schema_overrides is not None:
                try:
                    ov = entry.dynamic_schema_overrides()
                    if isinstance(ov, dict):
                        schema.update(ov)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("dynamic_schema_overrides for %s raised %s", name, exc)
            result.append({"type": "function", "function": schema})
        return result

    # ---- dispatch ----
    def dispatch(self, name: str, args: dict, ctx) -> str:
        """Execute a handler by name; returns its JSON string. Unknown tool or
        any exception becomes a ``{"error": ...}`` JSON string — a tool never
        raises into the turn (OpenCharaAgent's gateway adds the audit + loop guard on
        top of this)."""
        entry = self.get_entry(name)
        if not entry:
            return tool_error(f"Unknown tool: {name}")
        try:
            return entry.handler(args, ctx)
        except Exception as e:  # noqa: BLE001
            logger.exception("tool %s dispatch error: %s", name, e)
            return tool_error(f"Tool execution failed: {type(e).__name__}: {e}")

    # ---- queries ----
    def get_entry(self, name: str) -> Optional[ToolEntry]:
        with self._lock:
            return self._tools.get(name)

    def get_all_tool_names(self) -> list[str]:
        return sorted(e.name for e in self._snapshot())

    def get_schema(self, name: str) -> Optional[dict]:
        entry = self.get_entry(name)
        return entry.schema if entry else None

    def get_max_result_size(self, name: str, default: int | float | None = None) -> int | float:
        entry = self.get_entry(name)
        if entry and entry.max_result_size_chars is not None:
            return entry.max_result_size_chars
        return default if default is not None else DEFAULT_RESULT_SIZE_CHARS

    def get_emoji(self, name: str, default: str = "⚙") -> str:
        entry = self.get_entry(name)
        return entry.emoji if entry and entry.emoji else default

    def is_toolset_available(self, toolset: str) -> bool:
        with self._lock:
            check = self._toolset_checks.get(toolset)
        if not check:
            return True
        try:
            return bool(check())
        except Exception:  # noqa: BLE001
            return False


# Module-level singleton — builtin tool files import THIS name.
registry = ToolRegistry()


# ---------------------------------------------------------------------------
# Handler response helpers (hermes registry.py:563-589). Every handler returns
# a JSON string; these kill the json.dumps boilerplate.
# ---------------------------------------------------------------------------

# Explicit failure sentinel. Tool success/failure used to be inferred purely from
# JSON shape (a non-empty top-level "error" key), which misread legitimate results
# that merely carry "error": null (e.g. a background-launch result) as failures.
# tool_error now stamps this namespaced key so the gateway can judge status
# UNAMBIGUOUSLY; the shape heuristic remains only as a fallback for raw error
# dicts built outside tool_error (MCP, dispatch internals). Two-step migration:
# write the sentinel, recognize both — so replayed transcripts stay valid.
TOOL_ERROR_KEY = "__tool_error__"


def tool_error(message, **extra) -> str:
    result = {TOOL_ERROR_KEY: True, "error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    if data is not None:
        return json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    return json.dumps(kwargs, ensure_ascii=False)
