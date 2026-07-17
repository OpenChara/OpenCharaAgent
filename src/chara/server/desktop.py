"""Thin entry for `chara desktop` / the resident supervisor."""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from typing import Any

from . import supervisor as SUP


def free_port(host: str = "127.0.0.1") -> int:
    return SUP.free_port(host)


def serve_desktop(host: str, http_port: int, ws_port: int, token: str,
                  open_browser: bool = True, allow_hosts: list[str] | None = None,
                  pw_record: dict[str, Any] | None = None) -> int:
    """Run the supervisor in the foreground; Ctrl-C/SIGTERM tears down children."""
    # The hub process is NOT a chara, so its `chara.*` logs (supervisor child
    # lifecycle, visuals-job tracebacks, card drafting, image-gen, RPC errors) had
    # nowhere to land and were lost — the gap behind "a background generation crashed
    # and there's no log". Route them to ~/.chara/logs/{chara,errors}.log.
    with contextlib.suppress(Exception):
        from ..obs.log import setup_logging
        from ..session.sessions import home_dir
        setup_logging(directory=home_dir() / "logs")
    sup = SUP.Supervisor(host, http_port, ws_port, token, allow_hosts=allow_hosts,
                         pw_record=pw_record)

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


def daemonize_desktop(host: str, http_port: int, ws_port: int, token: str, *,
                      debug: bool = False, allow_hosts: list[str] | None = None) -> dict[str, Any]:
    """Start a detached supervisor process and write ~/.chara/daemon.json."""
    existing = SUP.read_daemon_json()
    if SUP.daemon_alive(existing):
        return existing
    log_path = SUP.daemon_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    env = {**os.environ, "CHARA_DAEMON_CHILD": "1"}
    if debug:
        env["CHARA_DEBUG"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "chara.front.cli",
        "desktop",
        "--host",
        str(host),
        "--port",
        str(http_port),
        "--ws-port",
        str(ws_port),
        f"--token={token}",
        "--no-open",
    ]
    if allow_hosts:
        cmd += ["--allow-host", ",".join(allow_hosts)]
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
    # immediate `chara start NAME` could race and fall back to the legacy daemon.
    import json as _json
    import time as _time
    import urllib.parse
    import urllib.request

    probe_host = "127.0.0.1" if SUP.N.is_wildcard_host(host) or SUP.N.is_loopback_host(host) else host
    deadline = _time.time() + 5.0
    while _time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"http://{probe_host}:{http_port}/rpc?token={urllib.parse.quote(str(token))}",
                data=_json.dumps({"jsonrpc": "2.0", "id": 1, "method": "sessions.list", "params": {}}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=0.5):
                break
        except Exception:
            _time.sleep(0.05)
    # The child rewrites daemon.json with the OS-assigned ws_port once bound.
    final = SUP.read_daemon_json()
    return {
        "pid": proc.pid,
        "http_port": int(final.get("http_port") or http_port),
        "ws_port": int(final.get("ws_port") or ws_port),
        "token": token,
        "path": str(path),
    }


