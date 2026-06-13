"""Minimal MCP client — external tool servers join the gateway (stdio only).

Config is the Claude Code convention, looked up per chara then per project:

    <CONFIG_DIR>/mcp.json  or  <repo>/mcp.json
    {"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"], "env": {}}}}

Design notes (the heavy lifting hermes does with the official MCP SDK —
stdio + HTTP/SSE + OAuth — is out of scope; we speak the stdio transport
directly with newline-delimited JSON-RPC and zero new dependencies):

- One persistent subprocess per server, spawned lazily on first use and
  restarted on the next call if it died. Locked per server: gateway calls
  arrive from worker threads.
- Subprocess env is FILTERED (hermes's safe-env rule): only a small benign
  set plus whatever the server's config block declares — never our API keys.
- MCP tools surface as `mcp__<server>__<tool>` (Claude Code naming) and go
  through the same audit trail as built-ins. A tool pack opts in with
  "mcp_servers": ["fetch"] or ["*"].
- IMPORTANT: MCP servers are operator-configured infrastructure and run
  OUTSIDE the chara's sandbox jail. Configuring one is a trust decision.
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, NoReturn

from ..config import ROOT, SANDBOX_ROOT
from ..obs import get_logger
from .schema_sanitizer import sanitize_input_schema

_log = get_logger("mcp")

_PROTOCOL_VERSION = "2025-03-26"
_SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL")
# Real timeouts (hermes mcp_tool.py: per-call 120 s, connect 60 s). A hung MCP
# server must never wedge the turn: on timeout the server is killed, reaped,
# and marked dead so later calls fail fast instead of hanging again.
_CONNECT_TIMEOUT = 60.0   # initialize handshake + tools/list
_CALL_TIMEOUT = 120.0     # one tools/call
_REAP_WAIT = 5.0          # grace for terminate() before kill()
_RESULT_CAP = 8000


def load_config(config_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """mcpServers from the chara's config dir, else the project root."""
    candidates = []
    if config_dir:
        candidates.append(Path(config_dir) / "mcp.json")
    candidates.append(ROOT / "mcp.json")
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                return {str(k): v for k, v in servers.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _safe_env(declared: "dict[str, str] | None") -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV}
    for k, v in (declared or {}).items():
        env[str(k)] = str(v)
    return env


class McpError(RuntimeError):
    pass


class _Client:
    """One stdio MCP server: lazy spawn, line-delimited JSON-RPC, per-client lock."""

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self._id = 0
        self._tools: list[dict[str, Any]] | None = None
        # Set once a timeout killed the server: a hung server that wedged one
        # call must not be restarted to hang the next one — later calls fail
        # fast with a visible error until the operator reconfigures/restarts.
        self.dead: str | None = None
        self._rx: "queue.Queue[str | None]" = queue.Queue()

    # -- transport --------------------------------------------------------------------

    def _ensure_started(self) -> None:
        if self.dead:
            raise McpError(f"mcp server {self.name!r} is disabled ({self.dead})")
        if self.proc is not None and self.proc.poll() is None:
            return
        command = self.config.get("command")
        if not command:
            raise McpError(f"mcp server {self.name!r}: no command configured")
        argv = [str(command)] + [str(a) for a in self.config.get("args", [])]
        restarted = self.proc is not None
        # Server stderr goes to a shared log, not DEVNULL: a crashing server
        # must leave diagnostics (hermes scar — the SDK default leaked stderr
        # into the live TUI; a file with per-spawn headers is the right slice).
        stderr_target = subprocess.DEVNULL
        log_handle = None
        try:
            log_path = SANDBOX_ROOT / "logs" / "mcp-stderr.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("ab")
            log_handle.write(f"\n--- {self.name} ({argv[0]}) {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
            log_handle.flush()
            stderr_target = log_handle
        except OSError:
            _log.debug("mcp stderr log unavailable; using DEVNULL", exc_info=True)
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_target,
                env=_safe_env(self.config.get("env")), text=True, bufsize=1,
            )
        except OSError as e:
            _log.error("server %r failed to start (%s): %s", self.name, argv[0], e)
            raise McpError(f"mcp server {self.name!r} failed to start: {e}") from e
        finally:
            if log_handle is not None:
                log_handle.close()  # the child holds its own duplicate
        _log.info("server %r %s (pid %d)", self.name, "restarted" if restarted else "started", self.proc.pid)
        self._tools = None
        # Dedicated reader thread per spawn: stdout lines flow into a queue so
        # _rpc can wait with a DEADLINE instead of blocking forever on a hung
        # server (the unbounded `for line in stdout` was the wedge).
        self._rx = queue.Queue()
        threading.Thread(
            target=self._read_loop, args=(self.proc, self._rx),
            name=f"lunamoth-mcp-{self.name}-reader", daemon=True,
        ).start()
        self._rpc("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "lunamoth", "version": "0.1"},
        }, timeout=_CONNECT_TIMEOUT)
        self._notify("notifications/initialized")

    @staticmethod
    def _read_loop(proc: subprocess.Popen, rx: "queue.Queue[str | None]") -> None:
        try:
            assert proc.stdout
            for line in proc.stdout:
                rx.put(line)
        except (OSError, ValueError):
            pass
        rx.put(None)  # EOF sentinel: the server closed its stdout

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:  # BrokenPipeError, closed file
            raise McpError(f"mcp server {self.name!r} is not accepting input ({e})") from e

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _rpc(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert self.proc
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        # Read until OUR response; ignore server notifications/other traffic.
        # Bounded by `timeout`: on expiry the server is killed and marked dead
        # — a visible error, never a wedged turn.
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._timeout_kill(method, timeout)
            try:
                raw = self._rx.get(timeout=remaining)
            except queue.Empty:
                self._timeout_kill(method, timeout)
            if raw is None:
                _log.error("server %r closed the stream during %s (crashed?)", self.name, method)
                raise McpError(f"mcp server {self.name!r} closed the stream (crashed?)")
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != rid:
                continue  # notification or unrelated message
            if "error" in msg:
                err = msg["error"]
                _log.warning("server %r returned an error for %s: %s", self.name, method, err)
                raise McpError(f"mcp {self.name}: {err.get('message', err)}")
            return msg.get("result", {})

    def _timeout_kill(self, method: str, timeout: float) -> NoReturn:
        self.dead = f"timed out after {timeout:.0f}s during {method}; killed"
        _log.error("server %r %s — it stays disabled until reconfigure/restart", self.name, self.dead)
        self._reap()
        raise McpError(f"mcp server {self.name!r} {self.dead}. It is disabled until the session is reconfigured.")

    def _reap(self) -> None:
        """Terminate AND wait — terminate-and-drop leaves zombies (audit #21)."""
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            return
        try:
            proc.wait(timeout=_REAP_WAIT)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=_REAP_WAIT)
            except subprocess.TimeoutExpired:
                _log.error("server %r did not die after SIGKILL (pid %d)", self.name, proc.pid)

    def close(self) -> None:
        self._reap()

    # -- MCP operations ---------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        with self.lock:
            self._ensure_started()
            if self._tools is None:
                result = self._rpc("tools/list", {}, timeout=_CONNECT_TIMEOUT)
                self._tools = [t for t in result.get("tools", []) if t.get("name")]
            return self._tools

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> str:
        with self.lock:
            self._ensure_started()
            result = self._rpc("tools/call", {"name": tool, "arguments": arguments}, timeout=_CALL_TIMEOUT)
        parts = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(f"[{block.get('type', 'non-text')} content omitted]")
        text = "\n".join(parts) or "(empty result)"
        if result.get("isError"):
            raise McpError(text[:1000])
        if len(text) > _RESULT_CAP:
            text = text[:_RESULT_CAP] + f"\n[output truncated — {len(text)} chars total]"
        return text


class McpManager:
    """All configured servers; tool names are mcp__<server>__<tool>."""

    def __init__(self, config_dir: Path | None = None):
        self.servers = load_config(config_dir)
        self._clients: dict[str, _Client] = {}
        atexit.register(self.close_all)

    def _client(self, server: str) -> _Client:
        if server not in self.servers:
            raise McpError(f"no mcp server named {server!r} configured")
        if server not in self._clients:
            self._clients[server] = _Client(server, self.servers[server])
        return self._clients[server]

    def allowed_servers(self, pack_entries: "list[str] | None") -> list[str]:
        """Servers a tool pack opts into: explicit names or '*' for all configured."""
        if not pack_entries:
            return []
        if "*" in pack_entries:
            return sorted(self.servers)
        return [s for s in pack_entries if s in self.servers]

    def schemas(self, servers: list[str]) -> list[dict[str, Any]]:
        """OpenAI-style function specs for the given servers' tools.

        A server that fails to start is skipped with no fabricated entries —
        its tools simply don't exist this turn (the failure is in the audit)."""
        out: list[dict[str, Any]] = []
        for server in servers:
            try:
                tools = self._client(server).list_tools()
            except McpError:
                continue
            for t in tools:
                out.append({
                    "type": "function",
                    "function": {
                        "name": f"mcp__{server}__{t['name']}",
                        "description": (t.get("description") or "")[:1000],
                        # Untrusted server schema → sanitize into a strict-backend
                        # -safe DEEP COPY (never mutate the cached _tools entry).
                        "parameters": sanitize_input_schema(t.get("inputSchema")),
                    },
                })
        return out

    def call(self, qualified: str, arguments: dict[str, Any]) -> str:
        try:
            _, server, tool = qualified.split("__", 2)
        except ValueError as e:
            raise McpError(f"bad mcp tool name {qualified!r}") from e
        return self._client(server).call_tool(tool, arguments)

    def close_all(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()
