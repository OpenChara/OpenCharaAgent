"""Shell-output plumbing for search.py (apple-to-apple port of the hermes
ripgrep/grep/find machinery in reference/hermes-agent/tools/file_operations.py).

OpenCharaAgent's ``ctx.run_terminal`` returns a FORMATTED string (``exit=N`` /
``STDOUT:`` / ``STDERR:`` sections + notes), not a clean ``(exit_code, stdout)``
pair the hermes ``_exec`` seam produced. To recover the underlying command's
real exit code (so the exit==2-only error guard works) and a clean stdout
payload, every command is wrapped to append a sentinel line carrying ``$?`` and
the merged stream is re-split here.

This module has NO top-level ``registry.register`` call, so the tool-discovery
AST scan never imports it as a tool module (leading-underscore + no register).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_RC_SENTINEL = "__LM_RC__"
# rg/grep diagnostics carry a tool prefix; rg's regex-parse-error block emits an
# indented caret line + a trailing "error: ..." line. Classify payload by shape.
_SEARCH_OUTPUT_RE = re.compile(r'^([A-Za-z]:)?[^\s:][^\n]*?[:\-]\d|^[^\s:][^\s]*$')


@dataclass
class ExecResult:
    stdout: str
    exit_code: int
    # False when the RC sentinel never came back — the command did not run to
    # completion (runner timeout, jail refusal, runner error). exit_code is
    # meaningless then; `note` carries the runner's own explanation. Callers
    # MUST surface this instead of reading empty stdout as "0 matches".
    completed: bool = True
    note: str = ""


def escape_shell_arg(arg: str) -> str:
    """Single-quote *arg*, escaping embedded single quotes as ``'"'"'``.
    (hermes file_operations.py:834-837)."""
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def _parse_runner_output(raw: str) -> tuple[str, str]:
    """Split OpenCharaAgent runner output into (stdout_section, stderr_section).

    The runner formats success as ``exit=N\\nSTDOUT:\\n...\\nSTDERR:\\n...`` with
    optional ``[chara: ...]`` notes appended. We merge STDOUT+STDERR into one
    stream (mirroring hermes ``stderr=subprocess.STDOUT``), then peel the runner
    notes. The authoritative exit code rides the sentinel, parsed by the caller.
    """
    out_section = ""
    err_section = ""
    # Drop runner notes (they begin with "[chara:" / "[timed out" / "[runner")
    lines = raw.split("\n")
    mode = None
    out_lines: list[str] = []
    err_lines: list[str] = []
    for line in lines:
        if line.startswith("STDOUT:"):
            mode = "out"
            continue
        if line.startswith("STDERR:"):
            mode = "err"
            continue
        if line.startswith("exit=") or line.startswith("[chara:") \
                or line.startswith("[timed out") or line.startswith("[runner error"):
            mode = None
            continue
        if mode == "out":
            out_lines.append(line)
        elif mode == "err":
            err_lines.append(line)
    out_section = "\n".join(out_lines)
    err_section = "\n".join(err_lines)
    return out_section, err_section


def run_capturing_rc(ctx, command: str, *, timeout: int = 60) -> ExecResult:
    """Run *command* via ctx.run_terminal, recovering the command's REAL exit
    code and a stderr-merged stdout stream (hermes ``_exec`` semantics).

    A trailing ``; printf '\\n__LM_RC__%s' "$?"`` carries the exit code through
    the runner's formatting; with ``set -o pipefail`` already on the rg/grep
    pipelines, the propagated code is rg/grep's, not head's.
    """
    wrapped = f"{{ {command}\n}}; printf '\\n{_RC_SENTINEL}%s' \"$?\""
    raw = ctx.run_terminal(wrapped, timeout=timeout)
    out_section, err_section = _parse_runner_output(raw)
    merged = out_section
    exit_code = 0
    completed = False
    # The sentinel was emitted on stdout; find the LAST occurrence.
    idx = merged.rfind(_RC_SENTINEL)
    if idx != -1:
        tail = merged[idx + len(_RC_SENTINEL):]
        m = re.match(r"\s*(-?\d+)", tail)
        if m:
            exit_code = int(m.group(1))
            completed = True
        merged = merged[:idx].rstrip("\n")
    # Merge stderr (rg/grep diagnostics) after stdout, like stderr=STDOUT.
    if err_section.strip():
        merged = (merged + "\n" + err_section).strip("\n") if merged else err_section
    if completed:
        return ExecResult(stdout=merged, exit_code=exit_code)
    # No sentinel = the command never finished: a runner timeout, a jail
    # refusal, or a runner error. Reading this as exit=0/empty-stdout turned
    # every such failure into a clean "0 matches" — a false negative that sent
    # the model down the wrong path. Carry the runner's own note out instead.
    note = next((ln.strip() for ln in raw.split("\n")
                 if ln.startswith(("[timed out", "[chara:", "[runner error"))),
                "the command did not run to completion")
    return ExecResult(stdout=merged, exit_code=-1, completed=False, note=note)


def split_tool_diagnostics(output: str) -> tuple[str, str]:
    """Separate rg/grep diagnostic lines from real match output by SHAPE.
    Returns ``(diagnostics, payload)`` (hermes file_operations.py:288-329)."""
    diagnostics: list[str] = []
    payload: list[str] = []
    for line in output.split("\n"):
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("rg: ") or stripped.startswith("grep: "):
            diagnostics.append(line)
            continue
        if line == "--" or _SEARCH_OUTPUT_RE.match(line):
            payload.append(line)
        else:
            diagnostics.append(line)
    return "\n".join(diagnostics), "\n".join(payload)


def parse_search_context_line(line: str):
    """Parse grep/rg context output ``path-line-content``; prefer the rightmost
    numeric separator (hermes file_operations.py:345-367)."""
    if not line or line == "--":
        return None
    match = None
    for candidate in re.finditer(r"-(\d+)-", line):
        match = candidate
    if match is None:
        return None
    path = line[: match.start()]
    if not path:
        return None
    return path, int(match.group(1)), line[match.end():]
