"""The Supervisor coordinator (lunamothd).

Owns the long-lived per-chara children and gateway controllers, the WS/PTY
routing, the idle/life driving, the signal handlers, and the shutdown
forensics. The static HTTP front (http.py) and the child lifecycle
(children.py) are the heavy collaborators; this module wires them together and
runs the serve loop.

It deliberately never imports core/tools: chara work happens only inside
``lunamoth serve <name> --stdio`` children.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import http.server
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...obs.audit import AuditLog
from ...session import isolation as I
from ...session import sessions as S
from .. import netsec as N
from ..pty import PtyBridge
from ..ws import _WSSink, _close_ws, _path_from_ws, _recv_text
from .children import CharaChild, GatewayChild, _Driver
from .daemon import write_daemon_json
from .http import _reachable_ips, start_http
from .lifestate import LifeState
from .observability import ResourceCanary, format_shutdown_context, snapshot_shutdown_context
from .paths import WEB_DIR

_log = logging.getLogger("lunamoth.server.supervisor")

# Whole-frame resize escape consumed server-side by the PTY endpoint
# (hermes shape): \x1b[RESIZE:<cols>;<rows>] — full-match only.
_PTY_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_PTY_READ_TIMEOUT = 0.2


class Supervisor:
    def __init__(
        self,
        host: str,
        http_port: int,
        ws_port: int,
        token: str,
        *,
        allow_hosts: list[str] | None = None,
        secure_cookie: bool = False,
        pw_record: dict | None = None,
    ) -> None:
        self.host = host
        self.http_port = int(http_port)
        self.ws_port = int(ws_port)  # 0 ⇒ OS-assigned; resolved in serve()
        self.token = token
        # OPTIONAL password-login record (public bind only); None ⇒ login inert.
        self.pw_record = pw_record
        # Host/Origin allow set (anti DNS-rebinding / CSWSH). Loopback + bound
        # host always; `allow_hosts` names extra reachable hosts for a proxy.
        self.allow_hosts = N.allowed_hosts(host, allow_hosts)
        self.wildcard_bind = N.is_wildcard_host(host)
        self.secure_cookie = bool(secure_cookie)
        self.charas: dict[str, CharaChild] = {}
        self.gateways: dict[str, GatewayChild] = {}
        self._pty_bridges: set[PtyBridge] = set()
        # Strong refs for fire-and-forget hub-dispatch tasks — the loop holds only a
        # weak ref, so without this an in-flight RPC can be GC'd and silently dropped.
        self._bg_tasks: set[asyncio.Task] = set()
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._shutdown = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._canary = ResourceCanary()
        self._shutdown_ctx: dict[str, Any] | None = None
        self._installed_signals: list[int] = []

    def child(self, name: str) -> CharaChild:
        meta = S.load_session(name)
        if meta is None:
            raise RuntimeError(f"no chara named {name!r}")
        child = self.charas.get(name)
        if child is None:
            child = CharaChild(meta, self)
            self.charas[name] = child
        return child

    @staticmethod
    def is_autonomous(meta: S.SessionMeta) -> bool:
        """Autonomy is the chara's persisted `mode`: live = autonomous, chat =
        plain chat agent. This is THE single autonomy switch (board, in-chat,
        and TUI all flip it); there is no separate pause flag."""
        try:
            cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
            return str(cfg.get("mode") or "live") == "live"
        except (OSError, json.JSONDecodeError):
            return True  # default live

    @staticmethod
    def set_mode_on_disk(meta: S.SessionMeta, mode: str) -> None:
        """Persist mode (live|chat) into the session config — used for a chara
        whose child isn't running. A running child is told via its /mode
        command so the live agent + snapshot update too."""
        try:
            cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
        cfg["mode"] = "live" if mode == "live" else "chat"
        cfg.pop("api_key", None)  # SEC-2: never persist the secret into a session config
        meta.config_path.parent.mkdir(parents=True, exist_ok=True)
        meta.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def gateway(self, name: str) -> GatewayChild:
        meta = S.load_session(name)
        if meta is None:
            raise RuntimeError(f"no chara named {name!r}")
        gw = self.gateways.get(name)
        if gw is None:
            gw = GatewayChild(meta, self)
            self.gateways[name] = gw
        return gw

    async def start_chara(self, name: str) -> dict[str, Any]:
        # Board "on" = autonomous + resident: mode live, start the child.
        child = self.child(name)
        Supervisor.set_mode_on_disk(child.meta, "live")
        return await child.start()

    async def stop_chara(self, name: str) -> dict[str, Any]:
        # Board "off" = not autonomous + stopped (saves tokens): mode chat,
        # stop the child. Entering to chat later starts a plain chat agent.
        child = self.child(name)
        Supervisor.set_mode_on_disk(child.meta, "chat")
        return await child.stop()

    async def restart_chara(self, name: str) -> dict[str, Any]:
        """Apply a card edit to a RUNNING chara: stop+start the child so it re-reads
        its frozen card into the cached stable prefix (history is restored by
        make_session). Preserves the current mode (doesn't force live/chat). A stopped
        chara is left stopped — its next start reads the new card anyway."""
        child = self.child(name)
        running = child.proc is not None and child.proc.returncode is None
        if running:
            await child.stop()
            await child.start()
        return {"restarted": running, **child.status()}

    async def set_autonomy(self, name: str, on: bool) -> dict[str, Any]:
        """Flip autonomy (mode live|chat) WITHOUT killing the chat you're in —
        the in-chat switch. A running child is told via its /mode command so the
        live agent + snapshot update immediately; a stopped child gets the
        config write (and is started if turning autonomy on).

        Turning autonomy OFF also INTERRUPTS an in-flight self-work turn so the
        tool chain halts at the next safe boundary (after the current tool call),
        rather than running the whole turn to completion. An operator chat reply
        (a live client stream) is left alone — that isn't autonomous work."""
        child = self.child(name)
        mode = "live" if on else "chat"
        Supervisor.set_mode_on_disk(child.meta, mode)
        if child.proc is not None and child.proc.returncode is None:
            with contextlib.suppress(Exception):
                await child.private_call("command", {"line": f"/mode {mode}"}, timeout=10.0)
            child._snap_cache = None
            if on:
                snap = await child.snapshot(silent=True)
                child.idle.schedule_after(snap or {})  # resume from now, not instantly
            else:
                child._emit_life(LifeState("waiting"))
                # Halt a running self-work turn now (interrupt is a no-op if idle
                # between cycles). Skip when a client stream is live so toggling
                # off mid-conversation never cuts the chara's reply to the operator.
                if not child._client_stream_ids:
                    with contextlib.suppress(Exception):
                        await child.private_call("interrupt", {}, timeout=10.0)
        elif on:
            await child.start()
        return child.status()

    def chara_status(self, name: str) -> dict[str, Any] | None:
        child = self.charas.get(name)
        return child.status() if child else None

    def life_state(self, name: str) -> dict[str, Any] | None:
        child = self.charas.get(name)
        return dataclasses.asdict(child.life) if child and child.life else None

    async def start_gateway(self, name: str, *, persist: bool = True) -> dict[str, Any]:
        return await self.gateway(name).start(persist=persist)

    async def stop_gateway(self, name: str, *, persist: bool = True) -> dict[str, Any]:
        return await self.gateway(name).stop(persist=persist)

    def gateway_status(self, name: str) -> dict[str, Any] | None:
        gw = self.gateways.get(name)
        if gw is None:
            meta = S.load_session(name)
            if meta is None:
                return None
            gw = self.gateway(name)
        return gw.status()

    async def gateway_status_live(self, name: str) -> dict[str, Any] | None:
        meta = S.load_session(name)
        if meta is None:
            return None
        return await self.gateway(name).status_live()

    async def gateways_all_live(self) -> dict[str, Any]:
        """Live gateway status for EVERY chara — the global gateway view's one
        source of truth (the same status_live() the per-chara pane uses, so the
        overview and the in-chara panel never disagree)."""
        out: list[dict[str, Any]] = []
        for meta in S.list_sessions():
            gw = self.gateway(meta.name)
            with contextlib.suppress(Exception):
                status = await gw.status_live()
                out.append({"name": meta.name, "enabled": gw.enabled(), "gateway": status})
        return {"gateways": out}

    async def bootstrap_gateways(self) -> None:
        for meta in S.list_sessions():
            gw = self.gateway(meta.name)
            if gw.enabled():
                await gw.start(persist=False)

    async def serve(self, *, open_browser: bool = True) -> int:
        if not WEB_DIR.is_dir() or not any(WEB_DIR.iterdir()):
            print(
                f"error: the web UI is not built at {WEB_DIR}\n"
                "       run: cd apps/web && npm install && npm run build",
                file=sys.stderr,
            )
            return 1
        self.loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        self._canary.start()
        # Bind the WS first so a `--ws-port 0` (OS-assigned) port is known before
        # we bake it into the URL/daemon.json. The HTTP port is started here too
        # so a conflict surfaces with attribution rather than a raw traceback.
        try:
            ws_server = await self._start_ws()
        except OSError as exc:
            print(f"error: could not bind the WebSocket port: {exc}", file=sys.stderr)
            return 1
        try:
            self.ws_port = ws_server.sockets[0].getsockname()[1]
        except (IndexError, AttributeError):
            pass
        try:
            self._httpd = start_http(
                self.host, self.http_port, self.token, self,
                allow_hosts=self.allow_hosts, secure_cookie=self.secure_cookie,
                pw_record=self.pw_record,
            )
            self.http_port = self._httpd.server_address[1]
        except OSError:
            holder = N.describe_port_holder(self.http_port)
            print(
                f"error: HTTP port {self.http_port} held by {holder}\n"
                "       stop it, or pass --port <other> (or --port 0 for any free port).",
                file=sys.stderr,
            )
            ws_server.close()
            with contextlib.suppress(Exception):
                await ws_server.wait_closed()
            return 1
        self._warn_on_public_bind()
        url = f"http://{self.host}:{self.http_port}/#token={self.token}&ws={self.ws_port}"
        # The resident daemon child owns daemon.json: rewrite it with the
        # resolved ports (the WS port was OS-assigned) so `lunamoth start NAME`
        # and `--connect` read the live values. A foreground (ephemeral) run
        # must NOT clobber a running daemon's metadata.
        if os.getenv("LUNAMOTH_DAEMON_CHILD"):
            write_daemon_json(os.getpid(), self.http_port, self.ws_port, self.token)
        print(f"LunaMoth desktop: {url}", file=sys.stderr, flush=True)
        print("life.state: supervisor emits life.state frames on client connect and transitions", file=sys.stderr, flush=True)
        if open_browser:
            self._open_later(url)
        await self.bootstrap_gateways()
        try:
            await self._shutdown.wait()
        finally:
            ws_server.close()
            with contextlib.suppress(Exception):
                await ws_server.wait_closed()
            await self.shutdown()
        return 0

    def _warn_on_public_bind(self) -> None:
        """A non-loopback bind exposes the chara's shell + tools to the network.
        Warn prominently and print the reachable URLs (AstrBot server.py:642-677).
        The token is the gate — serve() refuses a wildcard bind without one
        (enforced in cmd_desktop), so a reachable instance is always authed."""
        if N.is_loopback_host(self.host):
            return
        bar = "=" * 60
        lines = [
            bar,
            "  SECURITY: LunaMoth is bound to a NON-LOOPBACK address.",
            f"  Anyone who can reach {self.host}:{self.http_port} and holds the",
            "  token can drive this chara's shell, files, and tools.",
            "  Put a TLS reverse proxy in front; never expose it raw on the",
            "  public internet. The URL below carries the access token.",
            bar,
        ]
        for line in lines:
            print(line, file=sys.stderr, flush=True)
        for ip in _reachable_ips(self.host):
            print(f"  reachable: http://{ip}:{self.http_port}/", file=sys.stderr, flush=True)
        if self.pw_record is not None:
            print(
                "  password login is ALSO enabled: bookmark the host and log in "
                "with the password (no token URL needed).",
                file=sys.stderr, flush=True,
            )

    def _open_later(self, url: str) -> None:
        def work() -> None:
            time.sleep(0.4)
            if sys.platform == "darwin":
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import webbrowser

                webbrowser.open(url)

        threading.Thread(target=work, name="desktop-open", daemon=True).start()

    async def _start_ws(self) -> Any:
        """Bind the WebSocket server (port 0 ⇒ OS-assigned) and return it so the
        caller can read the chosen port and own the lifecycle."""
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError("the desktop needs websockets. Install with: uv sync --extra server") from exc
        handler = functools.partial(self._ws_entry)
        return await websockets.serve(
            handler, self.host, self.ws_port, max_size=16 * 1024 * 1024
        )

    def _origin_ok(self, ws: Any) -> bool:
        """Reject a cross-origin WS even with a valid token (anti-CSWSH). A
        missing Origin (native clients / Electron / CLI tunnels) is allowed —
        the token gates those; a PRESENT foreign Origin is the browser attack."""
        origin = ""
        request = getattr(ws, "request", None)
        if request is not None:
            headers = getattr(request, "headers", None)
            if headers is not None:
                with contextlib.suppress(Exception):
                    origin = headers.get("Origin", "") or headers.get("origin", "") or ""
        return N.origin_allowed(origin, self.allow_hosts, wildcard_bind=self.wildcard_bind)

    def _ws_cookie(self, ws: Any) -> str:
        """The Cookie header from the WS handshake (browsers send same-origin
        cookies on the upgrade request) — so a password-login user, who reached the
        bare bookmark with no ?token=, authenticates the WS via the lm_auth cookie."""
        request = getattr(ws, "request", None)
        if request is not None:
            headers = getattr(request, "headers", None)
            if headers is not None:
                with contextlib.suppress(Exception):
                    return headers.get("Cookie", "") or headers.get("cookie", "") or ""
        return ""

    async def _ws_entry(self, ws: Any, path: str = "") -> None:
        path = _path_from_ws(ws, path)
        # Origin is checked FIRST (anti-CSWSH) — a cross-origin browser WS is
        # rejected 4403 before auth, so accepting the cookie below is safe.
        if not self._origin_ok(ws):
            await _close_ws(ws, 4403, "origin not allowed")
            return
        # Dual-read like the HTTP gate: ?token= (Electron/SSH/token-URL) OR the
        # lm_auth cookie (password-login users, whose bookmark has no token).
        # No token configured ⇒ auth disabled (open), same as the HTTP gate.
        if self.token and not N.request_authed(urlsplit(path).query, self._ws_cookie(ws), self.token):
            await _close_ws(ws, 4401, "authentication required")
            return
        route = urlsplit(path).path
        if route in ("", "/", "/hub"):
            await self._handle_hub(ws)
        elif route.startswith("/chara/"):
            rest = route[len("/chara/"):].strip("/")
            if rest.endswith("/pty"):
                await self._handle_pty(ws, rest[: -len("/pty")].strip("/"), path)
            else:
                await self._handle_chara(ws, rest)
        else:
            await _close_ws(ws, 4404, "unknown endpoint")

    async def _handle_hub(self, ws: Any) -> None:
        from .. import hub as H
        sink = _WSSink(ws, asyncio.get_running_loop())
        dispatcher = H.HubDispatcher(sink.write, supervisor=self)
        loop = asyncio.get_running_loop()
        try:
            await sink.write_async({"jsonrpc": "2.0", "method": "hello", "params": {"role": "hub"}})
            while True:
                try:
                    raw = await _recv_text(ws)
                except Exception:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    await sink.write_async({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                    continue
                task = loop.create_task(self._dispatch_hub_async(dispatcher, req, sink))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)
        finally:
            sink.close()

    async def _dispatch_hub_async(self, dispatcher: Any, req: Any, sink: _WSSink) -> None:
        resp = await asyncio.to_thread(dispatcher.dispatch, req)
        if resp is not None:
            await sink.write_async(resp)

    async def _handle_chara(self, ws: Any, name: str) -> None:
        if S.load_session(name) is None:
            await _close_ws(ws, 4404, "no such chara")
            return
        child = self.child(name)
        try:
            # Opening the chara's room is operator activity: clear suspension.
            await child.ensure_started(operator=True)
        except Exception as exc:  # noqa: BLE001
            await _close_ws(ws, 4423, str(exc)[:120])
            return
        driver = _Driver(ws)
        await child.connect_driver(driver)
        first = True
        try:
            while True:
                try:
                    raw = (await _recv_text(ws)).strip()
                except Exception:
                    break
                if not raw:
                    continue
                if first:
                    first = False
                    try:
                        req = json.loads(raw)
                    except json.JSONDecodeError:
                        req = None
                    if isinstance(req, dict) and req.get("method") == "rejoin":
                        params = req.get("params") if isinstance(req.get("params"), dict) else {}
                        ok = await child.handle_rejoin(int(params.get("last_seq") or 0), driver)
                        driver.joined = ok if ok else True
                        continue
                    driver.joined = True
                await child.forward_client_frame(raw)
        finally:
            await child.disconnect_driver(driver)
            await _close_ws(ws)

    async def _handle_pty(self, ws: Any, name: str, path: str) -> None:
        """An operator shell inside the chara's isolation jail, over one WS.

        The shell targets the chara's HOME (its sandbox workspace), not the
        agent: no ensure_started() — the PTY works while the chara child is
        stopped or resting, and a PTY is NOT a driver (no rejoin/seq).
        """
        # OPEN QUESTION (curriculum): should a chara be able to sense that an
        # operator shell entered its home? For now the chara is NOT notified
        # and the transcript is untouched — the audit trail is the only record.
        meta = S.load_session(name)
        if meta is None:
            await _close_ws(ws, 4404, "no such chara")
            return
        qs = parse_qs(urlsplit(path).query)

        def _dim(key: str, default: int) -> int:
            try:
                return int((qs.get(key) or [default])[0])
            except (TypeError, ValueError):
                return default

        workspace = meta.sandbox_dir / "workspace"
        allow_network, writable = I.runtime_permissions(meta.sandbox_dir)
        audit = AuditLog(meta.sandbox_dir / "logs" / "audit.jsonl")
        try:
            argv, cwd, env = I.interactive_shell_argv(
                meta.isolation,
                workspace,
                allow_network=allow_network,
                writable_paths=writable,
            )
            bridge = PtyBridge.spawn(argv, cwd=cwd, env=env, cols=_dim("cols", 80), rows=_dim("rows", 24))
        except (I.JailUnavailableError, OSError) as exc:
            # No degrade, no silent fallback: the operator sees WHY in the
            # terminal, then the socket closes with an error code.
            _log.warning("pty for %s failed to start: %s", name, exc)
            with contextlib.suppress(Exception):
                await ws.send(f"\r\nshell unavailable: {exc}\r\n")
            await _close_ws(ws, 1011, str(exc)[:120])
            return
        self._pty_bridges.add(bridge)
        audit.write("pty_open", chara=name, isolation=meta.isolation, pid=bridge.pid)
        _log.info("pty open for %s (isolation=%s, pid=%d)", name, meta.isolation, bridge.pid)
        loop = asyncio.get_running_loop()

        async def pump_pty_to_ws() -> None:
            while True:
                chunk = await loop.run_in_executor(None, bridge.read, _PTY_READ_TIMEOUT)
                if chunk is None:  # EOF: child exited
                    await _close_ws(ws, 1000, "shell exited")
                    return
                if not chunk:
                    continue
                try:
                    await ws.send(chunk)  # binary frame: raw bytes, never JSON
                except Exception:
                    return

        reader = asyncio.create_task(pump_pty_to_ws(), name=f"pty-{name}-reader")
        try:
            while True:
                try:
                    msg = await ws.recv()
                except Exception:
                    break
                raw = msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
                if not raw:
                    continue
                match = _PTY_RESIZE_RE.fullmatch(raw)
                if match:  # consumed server-side, never written to the shell
                    bridge.resize(int(match.group(1)), int(match.group(2)))
                    continue
                bridge.write(raw)
        finally:
            reader.cancel()
            # CancelledError is a BaseException: plain suppress(Exception)
            # would let it abort the rest of this cleanup.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            bridge.close()
            self._pty_bridges.discard(bridge)
            audit.write("pty_close", chara=name, pid=bridge.pid, exit=bridge.returncode)
            _log.info("pty closed for %s (pid=%d, exit=%s)", name, bridge.pid, bridge.returncode)
            await _close_ws(ws)

    def _install_signal_handlers(self) -> None:
        """Record shutdown forensics on the loop's own signal path (async-safe).

        ``loop.add_signal_handler`` runs the callback in the event loop, so we
        can snapshot *why* we're dying and set ``_shutdown`` without touching
        re-entrancy-unsafe signal internals. Best-effort: on platforms/threads
        where it isn't available (e.g. a non-main-thread test loop) we leave the
        plain handler installed by desktop.py in place.
        """
        loop = self.loop
        if loop is None:
            return
        for signame in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, functools.partial(self.request_shutdown, signal_num=int(sig)))
                self._installed_signals.append(int(sig))
            except (NotImplementedError, RuntimeError, ValueError, OSError):
                _log.debug("could not install async signal handler for %s", signame, exc_info=True)

    def _remove_signal_handlers(self) -> None:
        loop = self.loop
        if loop is None:
            return
        for sig in self._installed_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError, OSError):
                loop.remove_signal_handler(sig)
        self._installed_signals.clear()

    async def shutdown(self) -> None:
        # Forensics first: a durable "this is why lunamothd is exiting" line so a
        # week-long-daemon death isn't a silent gap in the log. Never blocks.
        ctx = self._shutdown_ctx or snapshot_shutdown_context()
        with contextlib.suppress(Exception):
            _log.info("[SHUTDOWN] %s", format_shutdown_context(ctx))
        self._remove_signal_handlers()
        if self._httpd is not None:
            self._httpd.shutdown()
        for bridge in list(self._pty_bridges):
            with contextlib.suppress(Exception):
                bridge.close()
        self._pty_bridges.clear()
        for gw in list(self.gateways.values()):
            with contextlib.suppress(Exception):
                await gw.stop(persist=False)
        for child in list(self.charas.values()):
            with contextlib.suppress(Exception):
                await child.stop()
        # Final RSS line after teardown, so "last RSS before exit" is in the log.
        self._canary.stop()

    def request_shutdown(self, signal_num: int | None = None) -> None:
        # Snapshot the trigger the first time we're asked to stop — cheap,
        # non-blocking, and the most useful single forensic fact.
        if self._shutdown_ctx is None:
            with contextlib.suppress(Exception):
                self._shutdown_ctx = snapshot_shutdown_context(signal_num)
        self._shutdown.set()

    # ── self-restart (run the freshly-installed code) ──────────────────────────
    @staticmethod
    def _relaunch_argv() -> list[str]:
        """The command to re-exec this same supervisor. ``sys.executable``'s venv now
        holds the new code after ``uv tool install --force``; ``-m lunamoth.front.cli``
        + the original args (``desktop --host … --port … --token …``) relaunch it
        identically on the same ports."""
        return [sys.executable, "-m", "lunamoth.front.cli", *sys.argv[1:]]

    async def restart_self(self) -> None:
        """Relaunch this supervisor IN PLACE (os.execv — same PID, so daemon.json stays
        valid; same ports, which free up because the listening sockets are close-on-exec)
        so it runs the just-installed code. Children are stopped first so none orphan; the
        web clients auto-reconnect to the new instance. Best-effort: a failed exec exits
        cleanly (the update is on disk) rather than lingering half-torn-down."""
        _log.info("[RESTART] relaunching supervisor into updated code")
        with contextlib.suppress(Exception):
            await self.shutdown()  # children/gateways/pty stopped + httpd closed
        argv = self._relaunch_argv()
        try:
            os.execv(sys.executable, argv)  # never returns on success
        except OSError as exc:
            _log.error("[RESTART] execv failed (%s) — the update is installed; "
                       "restart manually to apply it", exc)
            os._exit(1)

    def schedule_restart(self, delay: float = 1.0) -> bool:
        """Schedule restart_self() on the event loop after ``delay`` seconds — gives the
        triggering RPC response time to flush before the process re-execs. Threadsafe (the
        HTTP RPC path runs off the loop). Returns False if there's no loop to schedule on."""
        loop = getattr(self, "loop", None)
        if loop is None:
            return False
        loop.call_soon_threadsafe(
            lambda: loop.call_later(delay, lambda: asyncio.ensure_future(self.restart_self()))
        )
        return True
