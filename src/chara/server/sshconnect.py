"""`chara connect ssh://[user@]host[:port]` — reach a remote chara over an SSH tunnel.

The cleanest secure remote path (plan §3 Track D, §7): the remote `charad`
stays bound to 127.0.0.1, and we forward two local ports (HTTP + WS) to the
remote loopback through `ssh -L`. SSH handles encryption + auth; the server is
never exposed. The browser then opens at a tunneled localhost URL carrying the
remote daemon's token.

Design for testability: every non-trivial decision is a PURE function (URL
parse, daemon.json parse, `ssh -L` argv build, local URL build). The two pieces
that talk to the outside world — running `ssh <target> <cmd>` and spawning the
long-lived `ssh -L` tunnel — sit behind thin seams (`_ssh_exec`, `_spawn_tunnel`,
`_free_local_port`, `_open_browser`) that tests monkeypatch. No new deps:
stdlib + subprocess only.
"""
from __future__ import annotations

import atexit
import contextlib
import json
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any


class ConnectError(Exception):
    """A user-facing connect failure (ssh missing, auth, no daemon, port busy)."""


@dataclass(frozen=True)
class SshTarget:
    """A parsed ssh:// target. `target()` is what we hand to the `ssh` binary."""

    host: str
    user: str | None = None
    port: int = 22

    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


@dataclass(frozen=True)
class RemoteDaemon:
    """The pieces of the remote daemon.json we forward + authenticate with."""

    token: str
    http_port: int
    ws_port: int


# ── pure functions (unit-tested directly) ────────────────────────────────────


def parse_ssh_url(url: str) -> SshTarget:
    """Parse `ssh://[user@]host[:port]` (also tolerates a bare `[user@]host[:port]`).

    Raises ConnectError on anything we can't turn into a host.
    """
    raw = (url or "").strip()
    if not raw:
        raise ConnectError("empty ssh target")
    # urlsplit needs a scheme to populate .hostname/.username/.port; add one if
    # the user passed a bare host[:port].
    if "://" not in raw:
        raw = "ssh://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "ssh":
        raise ConnectError(f"not an ssh:// URL: {url!r}")
    host = parts.hostname
    if not host:
        raise ConnectError(f"no host in ssh URL: {url!r}")
    try:
        port = parts.port or 22
    except ValueError as exc:  # malformed port component
        raise ConnectError(f"bad port in ssh URL: {url!r}") from exc
    user = parts.username or None
    return SshTarget(host=host, user=user, port=int(port))


def parse_daemon_json(text: str) -> RemoteDaemon:
    """Parse the remote `~/.chara/daemon.json` text into a RemoteDaemon.

    Raises ConnectError when the file is missing/empty/malformed or lacks the
    fields we need (token + both ports).
    """
    blob = (text or "").strip()
    if not blob:
        raise ConnectError("remote daemon.json is empty or missing")
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ConnectError(f"remote daemon.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConnectError("remote daemon.json is not a JSON object")
    token = data.get("token")
    try:
        http_port = int(data.get("http_port") or 0)
        ws_port = int(data.get("ws_port") or 0)
    except (TypeError, ValueError) as exc:
        raise ConnectError("remote daemon.json has non-numeric ports") from exc
    if not token or not http_port or not ws_port:
        raise ConnectError(
            "remote daemon.json missing token/http_port/ws_port — is charad running on the remote?"
        )
    return RemoteDaemon(token=str(token), http_port=http_port, ws_port=ws_port)


def build_ssh_argv(
    target: SshTarget,
    local_http: int,
    remote_http: int,
    local_ws: int,
    remote_ws: int,
) -> list[str]:
    """Build the `ssh -N -L … -L … <target>` argv for the persistent tunnel.

    Two forwards (HTTP + WS), no remote command (`-N`), keepalive + fail-fast on
    a forward error so we never silently hang. Mirrors hermes ssh-tunnel.ts:136.
    """
    argv = [
        "ssh",
        "-N",
        "-L",
        f"{local_http}:127.0.0.1:{remote_http}",
        "-L",
        f"{local_ws}:127.0.0.1:{remote_ws}",
    ]
    if target.port and target.port != 22:
        argv += ["-p", str(target.port)]
    argv += [
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        target.target(),
    ]
    return argv


def build_remote_exec_argv(target: SshTarget, command: str) -> list[str]:
    """Build a one-shot `ssh <target> <command>` argv (read daemon.json / start it)."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if target.port and target.port != 22:
        argv += ["-p", str(target.port)]
    argv += [target.target(), command]
    return argv


def build_local_url(daemon: RemoteDaemon, local_http: int, local_ws: int) -> str:
    """The tunneled localhost URL the browser opens (token + local ws port in the hash)."""
    token = urllib.parse.quote(daemon.token, safe="")
    return f"http://127.0.0.1:{local_http}/#token={token}&ws={local_ws}"


# ── thin subprocess seams (monkeypatched in tests) ───────────────────────────


def _ssh_exec(target: SshTarget, command: str, timeout: float = 20.0) -> tuple[int, str, str]:
    """Run `ssh <target> <command>` once; return (returncode, stdout, stderr)."""
    argv = build_remote_exec_argv(target, command)
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ConnectError(
            "ssh binary not found on PATH — install an SSH client (OpenSSH)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ConnectError(f"ssh to {target.target()} timed out after {timeout:.0f}s") from exc
    return proc.returncode, proc.stdout, proc.stderr


def _spawn_tunnel(argv: list[str]) -> subprocess.Popen[bytes]:
    """Spawn the long-lived `ssh -L` tunnel process."""
    try:
        return subprocess.Popen(argv)  # noqa: S603 — argv list, no shell
    except FileNotFoundError as exc:
        raise ConnectError(
            "ssh binary not found on PATH — install an SSH client (OpenSSH)."
        ) from exc


def _free_local_port() -> int:
    """Pick a free local port by binding 0 and reading it back (hermes findFreePort)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _open_browser(url: str) -> None:
    with contextlib.suppress(Exception):
        webbrowser.open(url)


# ── orchestration helpers ────────────────────────────────────────────────────


def read_remote_daemon(target: SshTarget) -> RemoteDaemon | None:
    """Read + parse the remote daemon.json. Returns None if the file is absent
    (so the caller can decide to start charad); raises ConnectError on an SSH
    failure (auth/host) or a malformed-but-present file."""
    rc, out, err = _ssh_exec(target, "cat ~/.chara/daemon.json")
    if rc != 0:
        msg = (err or out or "").strip().lower()
        # A missing file is the "no daemon yet" signal, not a hard error.
        if "no such file" in msg or "cannot find" in msg or "not exist" in msg:
            return None
        detail = (err or out or "").strip() or f"ssh exited {rc}"
        raise ConnectError(f"ssh to {target.target()} failed: {detail}")
    return parse_daemon_json(out)


def start_remote_daemon(target: SshTarget) -> None:
    """Start charad on the remote (`chara desktop --daemon`). Raises on failure."""
    rc, out, err = _ssh_exec(
        target, "chara desktop --daemon --no-open", timeout=60.0
    )
    if rc != 0:
        detail = (err or out or "").strip() or f"ssh exited {rc}"
        raise ConnectError(
            f"could not start charad on {target.target()}: {detail}\n"
            "       Start it manually there (`chara desktop --daemon`) and retry."
        )


def ensure_remote_daemon(target: SshTarget, *, log=print) -> RemoteDaemon:
    """Make sure charad is up on the remote and return its daemon info.

    Reads daemon.json; if absent, starts the daemon and re-reads (briefly
    retrying while it writes the file)."""
    daemon = read_remote_daemon(target)
    if daemon is not None:
        return daemon
    log(f"no charad on {target.target()} — starting it…")
    start_remote_daemon(target)
    for _ in range(10):
        daemon = read_remote_daemon(target)
        if daemon is not None:
            return daemon
        time.sleep(1.0)
    raise ConnectError(
        f"started charad on {target.target()} but its daemon.json never appeared."
    )


# ── the entry point used by `cmd_connect` ────────────────────────────────────


def connect(url: str, *, open_browser: bool = True, log=print) -> int:
    """Run the full connect flow: resolve remote daemon → free local ports →
    open the `ssh -L` tunnel → print + open the local URL → keep alive until
    Ctrl-C, tearing the tunnel down cleanly. Returns a process exit code."""
    target = parse_ssh_url(url)
    log(f"connecting to {target.target()} (ssh)…")
    daemon = ensure_remote_daemon(target, log=log)

    local_http = _free_local_port()
    local_ws = _free_local_port()
    argv = build_ssh_argv(
        target, local_http, daemon.http_port, local_ws, daemon.ws_port
    )

    proc = _spawn_tunnel(argv)

    def _teardown(*_a: Any) -> None:
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)

    atexit.register(_teardown)
    with contextlib.suppress(ValueError):  # not on the main thread → skip handler
        signal.signal(signal.SIGINT, lambda *_a: (_teardown(), sys.exit(0)))

    # Give the tunnel a moment; if ssh died immediately (auth / forward error),
    # surface it instead of opening a dead browser tab.
    time.sleep(1.0)
    if proc.poll() is not None:
        raise ConnectError(
            f"ssh tunnel to {target.target()} exited immediately (rc={proc.returncode}) — "
            "check the host, your key/agent, and that the remote ports are free."
        )

    local_url = build_local_url(daemon, local_http, local_ws)
    log(f"tunnel up: http://127.0.0.1:{local_http}  (ws {local_ws}) → {target.target()} 127.0.0.1:{daemon.http_port}/{daemon.ws_port}")
    log(f"open: {local_url}")
    if open_browser:
        _open_browser(local_url)
    log("Ctrl-C to disconnect.")

    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _teardown()
    return 0
