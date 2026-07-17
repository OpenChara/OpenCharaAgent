"""Daemon (charad) metadata: the daemon.json contract + liveness/stop.

Pure stdlib + the session home; no asyncio, no HTTP. ``chara start`` /
``--connect`` / ``chara daemon`` read these to find and control the resident
supervisor.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

from ...session import sessions as S


def daemon_json_path() -> Path:
    return S.chara_home() / "daemon.json"


def daemon_log_path() -> Path:
    return S.chara_home() / "logs" / "daemon.log"


def write_daemon_json(pid: int, http_port: int, ws_port: int, token: str) -> Path:
    path = daemon_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"pid": int(pid), "http_port": int(http_port), "ws_port": int(ws_port), "token": token}, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def read_daemon_json() -> dict[str, Any]:
    try:
        data = json.loads(daemon_json_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def daemon_alive(data: dict[str, Any] | None = None) -> bool:
    data = data or read_daemon_json()
    try:
        pid = int(data.get("pid") or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        # On POSIX, kill(0) succeeds for zombies; check /proc when available so
        # status/stop do not report a dead daemon as alive on Linux.
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            try:
                parts = stat.read_text(encoding="utf-8", errors="replace").split()
                if len(parts) > 2 and parts[2] == "Z":
                    return False
            except OSError:
                pass
        return True
    except (OSError, ValueError, TypeError):
        return False


def daemon_status() -> dict[str, Any]:
    data = read_daemon_json()
    data["alive"] = daemon_alive(data)
    data["path"] = str(daemon_json_path())
    data["log"] = str(daemon_log_path())
    data["home"] = str(S.chara_home())
    return data


def stop_daemon_process(grace: float = 8.0) -> bool:
    data = read_daemon_json()
    if not daemon_alive(data):
        with contextlib.suppress(OSError):
            daemon_json_path().unlink()
        return False
    pid = int(data["pid"])
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        if not daemon_alive(data):
            with contextlib.suppress(OSError):
                daemon_json_path().unlink()
            return True
        time.sleep(0.1)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        daemon_json_path().unlink()
    return True
