"""Thin entry for `lunamoth desktop` / the resident supervisor."""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from typing import Any

from ..session import sessions as S
from . import supervisor as SUP


def free_port(host: str = "127.0.0.1") -> int:
    return SUP.free_port(host)


def serve_desktop(host: str, http_port: int, ws_port: int, token: str,
                  open_browser: bool = True) -> int:
    """Run the supervisor in the foreground; Ctrl-C/SIGTERM tears down children."""
    sup = SUP.Supervisor(host, http_port, ws_port, token)

    def _term(signum: int, frame: Any) -> None:  # noqa: ARG001 - signal handler signature
        sup.request_shutdown()

    old_term = None
    with contextlib.suppress(ValueError, OSError):
        import signal

        old_term = signal.signal(signal.SIGTERM, _term)
    try:
        return asyncio.run(sup.serve(open_browser=open_browser))
    except KeyboardInterrupt:
        return 0
    finally:
        if old_term is not None:
            with contextlib.suppress(ValueError, OSError):
                import signal

                signal.signal(signal.SIGTERM, old_term)


def daemonize_desktop(host: str, http_port: int, ws_port: int, token: str, *, debug: bool = False) -> dict[str, Any]:
    """Start a detached supervisor process and write ~/.lunamoth/daemon.json."""
    existing = SUP.read_daemon_json()
    if SUP.daemon_alive(existing):
        return existing
    log_path = SUP.daemon_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    env = {**os.environ, "LUNAMOTH_DAEMON_CHILD": "1"}
    if debug:
        env["LUNAMOTH_DEBUG"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "lunamoth.front.cli",
        "desktop",
        "--port",
        str(http_port),
        "--ws-port",
        str(ws_port),
        f"--token={token}",
        "--no-open",
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        cwd=str(SUP.APP_DIR),
        env=env,
    )
    log.close()
    path = SUP.write_daemon_json(proc.pid, http_port, ws_port, token)
    # Return only after the supervisor answers its local HTTP RPC; otherwise an
    # immediate `lunamoth start NAME` could race and fall back to the legacy daemon.
    import json as _json
    import time as _time
    import urllib.parse
    import urllib.request

    deadline = _time.time() + 5.0
    while _time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{http_port}/rpc?token={urllib.parse.quote(str(token))}",
                data=_json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sessions.list", "params": {}}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=0.5):
                break
        except Exception:
            _time.sleep(0.05)
    return {"pid": proc.pid, "http_port": http_port, "ws_port": ws_port, "token": token, "path": str(path)}


def daemon_status() -> dict[str, Any]:
    data = SUP.read_daemon_json()
    data["alive"] = SUP.daemon_alive(data)
    data["path"] = str(SUP.daemon_json_path())
    data["log"] = str(SUP.daemon_log_path())
    data["home"] = str(S.lunamoth_home())
    return data
