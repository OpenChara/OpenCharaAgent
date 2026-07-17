"""Shutdown forensics + resource canary (#28).

A week-long charad that leaks (cached children, MCP connections, tool
schemas, transcript handles) is invisible until the OS OOM-kills it, and when
it dies the daemon log says nothing about *why*. Two small, never-throw
instruments — ported in shape from hermes' gateway/memory_monitor.py and
gateway/shutdown_forensics.py, kept stdlib-only and trimmed to our needs:

  * a 5-minute `[MEMORY] rss/gc/threads/uptime` line on a daemon thread, so a
    slow climb is grep-able after the fact, and
  * a fast (<10ms), non-blocking snapshot of who/what triggered shutdown,
    logged synchronously from the signal path.
"""
from __future__ import annotations

import contextlib
import gc
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

_log = logging.getLogger("chara.server.supervisor")

_BYTES_TO_MB = 1024 * 1024
_MEMORY_INTERVAL_SECONDS = 300.0


def _rss_mb() -> int | None:
    """Current process RSS in MB, or None if introspection is unavailable.

    Uses stdlib ``resource`` (Linux/macOS); ``ru_maxrss`` is bytes on macOS,
    KB on Linux. Never raises.
    """
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return int(maxrss / _BYTES_TO_MB)
        return int(maxrss / 1024)
    except Exception:
        return None


def log_memory_usage(prefix: str = "", *, start_time: float | None = None) -> None:
    """Emit a grep-friendly ``[MEMORY] ...`` line. Safe from any thread, never raises."""
    rss = _rss_mb()
    uptime = int(time.monotonic() - start_time) if start_time else 0
    try:
        gc_counts = gc.get_count()
    except Exception:
        gc_counts = (0, 0, 0)
    try:
        thread_count = threading.active_count()
    except Exception:
        thread_count = 0
    tag = f"{prefix} " if prefix else ""
    rss_str = "unavailable" if rss is None else f"{rss}MB"
    _log.info("[MEMORY] %srss=%s gc=%s threads=%d uptime=%ds", tag, rss_str, gc_counts, thread_count, uptime)


class ResourceCanary:
    """Periodic `[MEMORY]` logger on a daemon thread (leak detection).

    Daemon thread → never blocks process exit; every iteration is wrapped so a
    failed log can never throw into the agent/serve path. A baseline line is
    emitted on start and a final ``shutdown`` line on stop.
    """

    def __init__(self, interval: float = _MEMORY_INTERVAL_SECONDS) -> None:
        self.interval = float(interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time: float | None = None

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        if _rss_mb() is None:
            _log.warning("[MEMORY] resource canary unavailable (no resource.getrusage) — skipping")
            return False
        self._start_time = time.monotonic()
        self._stop.clear()
        log_memory_usage("baseline", start_time=self._start_time)
        self._thread = threading.Thread(target=self._loop, name="charad-memory-canary", daemon=True)
        self._thread.start()
        _log.info("[MEMORY] resource canary started (interval=%ds)", int(self.interval))
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                log_memory_usage(start_time=self._start_time)
            except Exception:
                _log.debug("memory canary iteration failed", exc_info=True)

    def stop(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is None:
            return
        with contextlib.suppress(Exception):
            log_memory_usage("shutdown", start_time=self._start_time)
        self._stop.set()
        self._thread = None
        with contextlib.suppress(Exception):
            thread.join(timeout=timeout)


def snapshot_shutdown_context(received_signal: Any = None) -> dict[str, Any]:
    """Fast (<10ms), never-raising snapshot of who/what asked us to shut down.

    Captures the signal name/number, our pid/ppid + parent process info
    (cmdline on Linux via /proc), whether systemd is our parent, RSS and the
    1-min load average. Pure stdlib; nothing here blocks on a subprocess.
    """
    pid = os.getpid()
    ppid = os.getppid()
    ctx: dict[str, Any] = {
        "ts": time.time(),
        "signal": _signal_name(received_signal),
        "signal_num": int(received_signal) if received_signal is not None else None,
        "pid": pid,
        "ppid": ppid,
    }
    parent = _proc_summary(ppid)
    if parent:
        ctx["parent"] = parent
    invocation_id = os.environ.get("INVOCATION_ID")
    ctx["under_systemd"] = bool(invocation_id) or ppid == 1
    if invocation_id:
        ctx["systemd_invocation_id"] = invocation_id
    rss = _rss_mb()
    if rss is not None:
        ctx["rss_mb"] = rss
    try:
        ctx["loadavg_1m"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    return ctx


_SIGNAL_NAME_BY_NUM: dict[int, str] = {}
for _name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"):
    _val = getattr(signal, _name, None)
    if _val is not None:
        _SIGNAL_NAME_BY_NUM[int(_val)] = _name


def _signal_name(sig: Any) -> str:
    if sig is None:
        return "UNKNOWN"
    try:
        sig_int = int(sig)
    except (TypeError, ValueError):
        return str(sig)
    return _SIGNAL_NAME_BY_NUM.get(sig_int, f"signal#{sig_int}")


def _proc_summary(pid: int) -> dict[str, Any]:
    """Compact /proc/<pid> snapshot (Linux only); empty dict elsewhere. Never raises."""
    if pid <= 0 or not sys.platform.startswith("linux"):
        return {}
    summary: dict[str, Any] = {"pid": pid}
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Name:"):
                    summary["name"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            data = fh.read()
        if data:
            summary["cmdline"] = data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()[:300]
    except OSError:
        pass
    return summary


def format_shutdown_context(ctx: dict[str, Any]) -> str:
    """Render a shutdown context dict as one scannable log line."""
    parent = ctx.get("parent") or {}
    parts = [
        f"signal={ctx.get('signal', '?')}",
        f"under_systemd={'yes' if ctx.get('under_systemd') else 'no'}",
        f"parent_pid={parent.get('pid', '?')}",
        f"parent_name={parent.get('name', '?')}",
    ]
    if "rss_mb" in ctx:
        parts.append(f"rss={ctx['rss_mb']}MB")
    if "loadavg_1m" in ctx:
        parts.append(f"loadavg_1m={ctx['loadavg_1m']}")
    if parent.get("cmdline"):
        parts.append(f"parent_cmdline={parent['cmdline']!r}")
    return " ".join(parts)
