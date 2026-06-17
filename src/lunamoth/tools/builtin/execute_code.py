"""execute_code — Programmatic Tool Calling (PTC), ported from hermes-agent
(reference/hermes-agent/tools/code_execution_tool.py) apple-to-apple, re-shaped
onto LunaMoth's one-process-one-chara runtime.

The model writes a Python script that runs in a *child process inside the chara
isolation* (``ctx.run_terminal`` → sandbox/admin) and calls a whitelisted
subset of the chara's real tools over a local RPC channel. Only the script's
STDOUT returns to the model — intermediate tool results never enter the context
window. This collapses multi-step tool chains into one inference turn.

Divergences from hermes (documented honestly, never a fake success):
  * Transport: hermes' default is a Unix-domain socket; under macOS
    ``sandbox-exec`` with ``(deny network*)`` a UDS connect can be refused. So we use hermes'
    *file-based RPC* transport (its own remote-backend path): request/response
    files under the chara workspace, which every isolation can always read/write
    and which needs no network permission. The RPC server polls in a parent
    thread and dispatches via ``ctx.dispatch(name, args)``.
  * Tool dispatch: hermes re-enters ``model_tools.handle_function_call``; we call
    back into the SAME chara's gateway via ``ctx.dispatch`` (one process, one
    gateway — feasible exactly because LunaMoth is one-process-one-chara).
  * Dropped: the multi-terminal-backend env map, per-profile HOME isolation,
    Windows TCP fallback (macOS/Linux only). Kept verbatim: the sandbox tool
    allowlist ∩ enabled, the 50KB/300s/50-call limits, the stub module with
    json_parse/shell_quote/retry, ANSI strip + secret redaction, the JSON return
    shape ``{status, output, tool_calls_made, duration_seconds, error?}``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from ..registry import registry, tool_error

logger = logging.getLogger("lunamoth.tools.execute_code")

# The tools allowed inside the sandbox (hermes SANDBOX_ALLOWED_TOOLS,
# code_execution_tool.py:61-69). The intersection of this list and the
# session's enabled tools determines which stubs are generated.
SANDBOX_ALLOWED_TOOLS = frozenset([
    "web_search",
    "web_extract",
    "read_file",
    "write_file",
    "search_files",
    "patch",
    "terminal",
])

# Resource limit defaults (hermes code_execution_tool.py:72-75).
DEFAULT_TIMEOUT = 300        # 5 minutes
DEFAULT_MAX_TOOL_CALLS = 50
MAX_STDOUT_BYTES = 50_000    # 50 KB
MAX_STDERR_BYTES = 10_000    # 10 KB

# Terminal params that must not be used from ephemeral sandbox scripts
# (hermes _TERMINAL_BLOCKED_PARAMS, :465).
_TERMINAL_BLOCKED_PARAMS = {"background", "pty", "notify_on_complete", "watch_patterns"}

# ANSI escape stripper (hermes tools/ansi_strip.py shape).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Secret redaction: bearer tokens / common API-key prefixes. Mirrors the intent
# of hermes' agent.redact._PREFIX_RE — never let a leaked secret enter context.
_SECRET_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}"
    r"|sk-ant-[A-Za-z0-9_-]{16,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{20,})\b"
)


def check_sandbox_requirements() -> bool:
    """Code execution sandbox requires POSIX (hermes :200-204). macOS/Linux only
    per CLAUDE.md."""
    return os.name == "posix"


# ---------------------------------------------------------------------------
# hermes_tools.py stub generator (hermes :211-291)
# ---------------------------------------------------------------------------

# Per-tool stubs: (func_name, signature, docstring, args_dict_expr).
_TOOL_STUBS = {
    "web_search": (
        "web_search",
        "query: str, limit: int = 5",
        '"""Search the web. Returns dict with data.web list of {url, title, description}."""',
        '{"query": query, "limit": limit}',
    ),
    "web_extract": (
        "web_extract",
        "urls: list",
        '"""Extract content from URLs. Returns dict with results list of {url, title, content, error}."""',
        '{"urls": urls}',
    ),
    "read_file": (
        "read_file",
        "path: str, offset: int = 1, limit: int = 500",
        '"""Read a file (1-indexed lines). Returns dict with "content" and "total_lines"."""',
        '{"path": path, "offset": offset, "limit": limit}',
    ),
    "write_file": (
        "write_file",
        "path: str, content: str",
        '"""Write content to a file (always overwrites). Returns dict with status."""',
        '{"path": path, "content": content}',
    ),
    "search_files": (
        "search_files",
        'pattern: str, target: str = "content", path: str = ".", file_glob: str = None, limit: int = 50, offset: int = 0, output_mode: str = "content", context: int = 0',
        '"""Search file contents (target="content") or find files by name (target="files"). Returns dict with "matches"."""',
        '{"pattern": pattern, "target": target, "path": path, "file_glob": file_glob, "limit": limit, "offset": offset, "output_mode": output_mode, "context": context}',
    ),
    "patch": (
        "patch",
        'path: str = None, old_string: str = None, new_string: str = None, replace_all: bool = False, mode: str = "replace", patch: str = None',
        '"""Targeted find-and-replace (mode="replace") or V4A multi-file patches (mode="patch"). Returns dict with status."""',
        '{"path": path, "old_string": old_string, "new_string": new_string, "replace_all": replace_all, "mode": mode, "patch": patch}',
    ),
    "terminal": (
        "terminal",
        "command: str, timeout: int = None, workdir: str = None",
        '"""Run a shell command (foreground only). Returns dict with "output" and "exit_code"."""',
        '{"command": command, "timeout": timeout, "workdir": workdir}',
    ),
}

_COMMON_HELPERS = '''\

# ---------------------------------------------------------------------------
# Convenience helpers (avoid common scripting pitfalls)
# ---------------------------------------------------------------------------

def json_parse(text):
    """Parse JSON tolerant of control characters (strict=False)."""
    return json.loads(text, strict=False)


def shell_quote(s):
    """Shell-escape a string for safe interpolation into commands."""
    return shlex.quote(s)


def retry(fn, max_attempts=3, delay=2):
    """Retry a function up to max_attempts times with exponential backoff."""
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay * (2 ** attempt))
    raise last_err

'''

# File-based RPC transport header (hermes _FILE_TRANSPORT_HEADER, :400-457).
# The RPC dir is baked into the source so the child needs no env injection
# (run_terminal does not let us add child env vars).
_FILE_TRANSPORT_HEADER = '''\
"""Auto-generated LunaMoth tools RPC stubs (file-based transport)."""
import json, os, shlex, threading, time

_RPC_DIR = {rpc_dir!r}
_seq = 0
_seq_lock = threading.Lock()
''' + _COMMON_HELPERS + '''\

def _call(tool_name, args):
    """Send a tool call request via file-based RPC and wait for response."""
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq
    seq_str = "%06d" % seq
    req_file = os.path.join(_RPC_DIR, "req_" + seq_str)
    res_file = os.path.join(_RPC_DIR, "res_" + seq_str)

    tmp = req_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({{"tool": tool_name, "args": args, "seq": seq}}, f)
    os.rename(tmp, req_file)

    deadline = time.monotonic() + 300
    poll_interval = 0.05
    while not os.path.exists(res_file):
        if time.monotonic() > deadline:
            raise RuntimeError("RPC timeout: no response for " + tool_name + " after 300s")
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.2, 0.25)

    with open(res_file, encoding="utf-8") as f:
        raw = f.read()
    try:
        os.unlink(res_file)
    except OSError:
        pass

    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

'''


def generate_tools_module(enabled_tools, rpc_dir: str) -> str:
    """Build the source for the auto-generated tools stub module (hermes
    generate_hermes_tools_module, :259-291)."""
    tools_to_generate = sorted(SANDBOX_ALLOWED_TOOLS & set(enabled_tools))
    stub_functions = []
    for tool_name in tools_to_generate:
        if tool_name not in _TOOL_STUBS:
            continue
        func_name, sig, doc, args_expr = _TOOL_STUBS[tool_name]
        stub_functions.append(
            f"def {func_name}({sig}):\n"
            f"    {doc}\n"
            f"    return _call({func_name!r}, {args_expr})\n"
        )
    header = _FILE_TRANSPORT_HEADER.format(rpc_dir=rpc_dir)
    return header + "\n".join(stub_functions)


# ---------------------------------------------------------------------------
# File-based RPC server (runs in a parent thread; hermes _rpc_server_loop, :468)
# ---------------------------------------------------------------------------

def _rpc_server_loop(
    rpc_dir: Path,
    dispatch,
    tool_call_log: list,
    tool_call_counter: list,
    max_tool_calls: int,
    allowed_tools: frozenset,
    stop_event: threading.Event,
):
    """Poll *rpc_dir* for ``req_*`` files, dispatch each via *dispatch*, write a
    ``res_*`` file. Mirrors hermes' RPC dispatch: allowlist enforcement, call
    limit, terminal blocked-param stripping (:516-574)."""
    while not stop_event.is_set():
        try:
            entries = sorted(p for p in rpc_dir.iterdir()
                             if p.name.startswith("req_") and not p.name.endswith(".tmp"))
        except OSError:
            entries = []
        if not entries:
            stop_event.wait(0.02)
            continue
        for req_path in entries:
            res_path = rpc_dir / req_path.name.replace("req_", "res_", 1)
            call_start = time.monotonic()
            try:
                request = json.loads(req_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                _write_response(res_path, tool_error(f"Invalid RPC request: {exc}"))
                _safe_unlink(req_path)
                continue

            tool_name = request.get("tool", "")
            tool_args = request.get("args", {})
            if not isinstance(tool_args, dict):
                tool_args = {}

            # Enforce the allow-list (hermes :517-526).
            if tool_name not in allowed_tools:
                available = ", ".join(sorted(allowed_tools))
                _write_response(res_path, json.dumps({
                    "error": (f"Tool '{tool_name}' is not available in execute_code. "
                              f"Available: {available}")
                }, ensure_ascii=False))
                _safe_unlink(req_path)
                continue

            # Enforce tool call limit (hermes :528-537).
            if tool_call_counter[0] >= max_tool_calls:
                _write_response(res_path, json.dumps({
                    "error": (f"Tool call limit reached ({max_tool_calls}). "
                              "No more tool calls allowed in this execution.")
                }, ensure_ascii=False))
                _safe_unlink(req_path)
                continue

            # Strip forbidden terminal parameters (hermes :539-542).
            if tool_name == "terminal":
                for param in _TERMINAL_BLOCKED_PARAMS:
                    tool_args.pop(param, None)

            try:
                result = dispatch(tool_name, tool_args)
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001
                logger.error("tool call failed in sandbox: %s", exc, exc_info=True)
                result = tool_error(str(exc))

            tool_call_counter[0] += 1
            tool_call_log.append({
                "tool": tool_name,
                "args_preview": str(tool_args)[:80],
                "duration": round(time.monotonic() - call_start, 2),
            })
            _write_response(res_path, result)
            _safe_unlink(req_path)


def _write_response(res_path: Path, payload: str) -> None:
    tmp = res_path.with_suffix(".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.rename(tmp, res_path)
    except OSError as e:
        logger.debug("failed to write RPC response: %s", e)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)


def _enabled_tools(ctx) -> list[str]:
    """The chara's currently-callable tools — the SAME set the gateway gates on
    (registry ∩ pack), so the sandboxed Python can call exactly what a model can."""
    try:
        if ctx.enabled_tool_names is not None:
            return sorted(ctx.enabled_tool_names())
    except Exception:  # noqa: BLE001
        pass
    return []


# ---------------------------------------------------------------------------
# The handler
# ---------------------------------------------------------------------------

def execute_code(args: dict, ctx) -> str:
    """Run the model's Python script in a child process inside the chara
    isolation, servicing whitelisted tool calls over file-based RPC."""
    code = args.get("code", "")
    if not isinstance(code, str) or not code.strip():
        return tool_error("execute_code requires non-empty 'code'.")

    if ctx.dispatch is None:
        return tool_error("execute_code is unavailable: no tool dispatcher in this context.")

    # Determine which tools the sandbox can call (hermes :1127-1132):
    # SANDBOX_ALLOWED_TOOLS ∩ enabled; empty intersection → full allowlist.
    session_tools = set(_enabled_tools(ctx))
    sandbox_tools = frozenset(SANDBOX_ALLOWED_TOOLS & session_tools) or SANDBOX_ALLOWED_TOOLS

    workspace = ctx.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    stage_dir = workspace / f".execute_code_{run_id}"
    rpc_dir = stage_dir / "rpc"
    try:
        rpc_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return tool_error(f"execute_code could not stage temp dir: {e}")

    timeout = DEFAULT_TIMEOUT
    max_tool_calls = DEFAULT_MAX_TOOL_CALLS
    tool_call_log: list = []
    tool_call_counter = [0]
    exec_start = time.monotonic()
    stop_event = threading.Event()
    rpc_thread = None

    try:
        # The child references the rpc dir by an in-isolation path. Under
        # admin/sandbox isolation the workspace path is shared verbatim; the stub
        # bakes in the absolute path of rpc_dir.
        tools_src = generate_tools_module(list(sandbox_tools), str(rpc_dir))
        (stage_dir / "hermes_tools.py").write_text(tools_src, encoding="utf-8")
        (stage_dir / "lunamoth_tools.py").write_text(tools_src, encoding="utf-8")
        (stage_dir / "script.py").write_text(code, encoding="utf-8")

        # Start the RPC server thread BEFORE the (blocking) child runs.
        rpc_thread = threading.Thread(
            target=_rpc_server_loop,
            args=(rpc_dir, ctx.dispatch, tool_call_log, tool_call_counter,
                  max_tool_calls, sandbox_tools, stop_event),
            daemon=True,
        )
        rpc_thread.start()

        # Run the child inside the chara isolation. PYTHONPATH points at the
        # stage dir so `from hermes_tools import ...` (and lunamoth_tools)
        # resolve. We force UTF-8 + no bytecode, exactly like hermes (:1225-1244).
        # Run from the workspace (the default cwd) and `cd` into the stage dir by
        # its workspace-relative name. Do NOT pass workdir=stage_dir: that makes
        # run_terminal set the real cwd to stage_dir under sandbox-darwin/admin
        # isolation, so this `cd {rel}` would double-apply (stage_dir/.execute_code_*
        # → "No such file or directory", script never runs, yet status=success).
        # With cwd=workspace for every isolation (bwrap chdir to workspace
        # itself), `cd {rel}` resolves to the stage dir uniformly.
        rel = stage_dir.name
        command = (
            f"cd {_q(rel)} && "
            f"PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 "
            f"PYTHONPATH={_q(str(stage_dir))} "
            f"python3 script.py"
        )
        raw_output = ctx.run_terminal(command, timeout=timeout)
        stop_event.set()
        if rpc_thread:
            rpc_thread.join(timeout=3)

        output = _strip_ansi(raw_output or "")
        output = _redact(output)

        # Head+tail truncation to MAX_STDOUT_BYTES (hermes :1398-1408).
        if len(output) > MAX_STDOUT_BYTES:
            head_n = int(MAX_STDOUT_BYTES * 0.4)
            tail_n = MAX_STDOUT_BYTES - head_n
            omitted = len(output) - head_n - tail_n
            output = (
                output[:head_n]
                + f"\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted "
                  f"out of {len(output):,} total] ...\n\n"
                + output[-tail_n:]
            )

        status = "success"
        if "[runner: timeout after" in raw_output or "timed out" in raw_output.lower():
            status = "timeout"

        result = {
            "status": status,
            "output": output,
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": round(time.monotonic() - exec_start, 2),
        }
        if status == "timeout":
            result["error"] = f"Script timed out after {timeout}s and was killed."
        return json.dumps(result, ensure_ascii=False)

    except Exception as exc:  # noqa: BLE001
        logger.error("execute_code failed: %s", exc, exc_info=True)
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": round(time.monotonic() - exec_start, 2),
        }, ensure_ascii=False)
    finally:
        stop_event.set()
        if rpc_thread:
            rpc_thread.join(timeout=2)
        shutil.rmtree(stage_dir, ignore_errors=True)


def _q(s: str) -> str:
    """Single-quote shell-escape (no shlex import at module top for clarity)."""
    return "'" + str(s).replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Schema (hermes build_execute_code_schema, :1724-1810)
# ---------------------------------------------------------------------------

_TOOL_DOC_LINES = [
    ("web_search",
     "  web_search(query: str, limit: int = 5) -> dict\n"
     "    Returns {\"data\": {\"web\": [{\"url\", \"title\", \"description\"}, ...]}}"),
    ("web_extract",
     "  web_extract(urls: list[str]) -> dict\n"
     "    Returns {\"results\": [{\"url\", \"title\", \"content\", \"error\"}, ...]} where content is markdown"),
    ("read_file",
     "  read_file(path: str, offset: int = 1, limit: int = 500) -> dict\n"
     "    Lines are 1-indexed. Returns {\"content\": \"...\", \"total_lines\": N}"),
    ("write_file",
     "  write_file(path: str, content: str) -> dict\n"
     "    Always overwrites the entire file."),
    ("search_files",
     "  search_files(pattern: str, target=\"content\", path=\".\", file_glob=None, limit=50) -> dict\n"
     "    target: \"content\" (search inside files) or \"files\" (find files by name). Returns {\"matches\": [...]}"),
    ("patch",
     "  patch(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict\n"
     "    Replaces old_string with new_string in the file."),
    ("terminal",
     "  terminal(command: str, timeout=None, workdir=None) -> dict\n"
     "    Foreground only (no background/pty). Returns {\"output\": \"...\", \"exit_code\": N}"),
]


def build_execute_code_schema(enabled_sandbox_tools=None) -> dict:
    """Build the execute_code schema, listing only the enabled sandbox tools
    (hermes :1724-1810)."""
    if enabled_sandbox_tools is None:
        enabled_sandbox_tools = SANDBOX_ALLOWED_TOOLS
    tool_lines = "\n".join(
        doc for name, doc in _TOOL_DOC_LINES if name in enabled_sandbox_tools
    )
    import_examples = [n for n in ("web_search", "terminal") if n in enabled_sandbox_tools]
    if not import_examples:
        import_examples = sorted(enabled_sandbox_tools)[:2]
    import_str = (", ".join(import_examples) + ", ...") if import_examples else "..."

    cwd_note = (
        "Scripts run in a private staging dir inside your workspace — use "
        "relative paths under the workspace or absolute workspace paths, or "
        "terminal()/read_file() for files elsewhere in the workspace."
    )
    description = (
        "Run a Python script that can call your tools programmatically. "
        "Use this when you need 3+ tool calls with processing logic between them, "
        "need to filter/reduce large tool outputs before they enter your context, "
        "need conditional branching (if X then Y else Z), or need to loop "
        "(fetch N pages, process N files, retry on failure).\n\n"
        "Use normal tool calls instead when: single tool call with no processing, "
        "you need to see the full result and apply complex reasoning, "
        "or the task requires interactive user input.\n\n"
        f"Available via `from hermes_tools import ...`:\n\n"
        f"{tool_lines}\n\n"
        "Limits: 5-minute timeout, 50KB stdout cap, max 50 tool calls per script. "
        "terminal() is foreground-only (no background or pty).\n\n"
        f"{cwd_note}\n\n"
        "Print your final result to stdout. Use Python stdlib (json, re, math, csv, "
        "datetime, collections, etc.) for processing between tool calls.\n\n"
        "Also available (no import needed — built into hermes_tools):\n"
        "  json_parse(text: str) — json.loads with strict=False; use for terminal() output with control chars\n"
        "  shell_quote(s: str) — shlex.quote(); use when interpolating dynamic strings into shell commands\n"
        "  retry(fn, max_attempts=3, delay=2) — retry with exponential backoff for transient failures"
    )
    return {
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python code to execute. Import tools with "
                        f"`from hermes_tools import {import_str}` "
                        "and print your final result to stdout."
                    ),
                },
            },
            "required": ["code"],
        },
    }


EXECUTE_CODE_SCHEMA = build_execute_code_schema()


registry.register(
    "execute_code", "code_execution",
    EXECUTE_CODE_SCHEMA,
    execute_code,
    check_fn=check_sandbox_requirements,
    emoji="🐍",
    max_result_size_chars=100_000,
)
