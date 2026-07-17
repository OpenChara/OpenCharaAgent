"""The `chara` command — a roster of persistent agents, not throwaway sessions.

    chara                 open the webui desktop (web renderer + hub gateway)
    chara tui             open the terminal roster (resume-first launcher)
    chara new NAME        create an agent (--isolation sandbox|admin)
    chara ls              list agents and their status
    chara attach NAME     open an agent in the TUI (adopts its background loop)
    chara start [NAME]    run an agent in the background; --all / `start-all`
    chara stop [NAME]     stop an agent's background loop; --all
    chara rm NAME         delete an agent
    chara setup [NAME]    (re)run the setup wizard
    chara setup browser   install the optional agent-browser tool driver
    chara update          update to the latest release (wheel; dev checkout = git pull + uv sync)
    chara doctor          check environment & sandbox backends

Each agent is a persistent being: it lives in the background on its own and
you attach/detach. `start-all` brings them all back after a reboot. Remote
baseline: `ssh host -t chara attach NAME`; future gateways reuse
`sessions.SessionMeta.env()` as the activation interface.

IMPORTANT: runtime modules (config/settings/tui) resolve paths from env at
import time, so this module only imports them lazily AFTER session env vars
are exported.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import runpy
import signal
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import __version__
from ..session import sessions as S

APP_DIR = Path(__file__).resolve().parents[3]  # repo checkout (dev or ~/.chara/app)


# ---- resident supervisor discovery ----------------------------------------

def _daemon_info() -> dict:
    try:
        from ..server.supervisor import read_daemon_json

        return read_daemon_json()
    except Exception:
        return {}


def _daemon_rpc(method: str, params: dict | None = None, timeout: float = 10.0) -> dict | None:
    data = _daemon_info()
    token = str(data.get("token") or "")
    port = int(data.get("http_port") or 0)
    if not token or not port:
        return None
    # Avoid importing server/ from front at module import time; daemon helpers are
    # process-boundary clients using only stdlib HTTP.
    import urllib.parse
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/rpc?token={urllib.parse.quote(token)}",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    return out if isinstance(out, dict) else None


def _live_daemon_rpc(method: str, params: dict | None = None, timeout: float = 10.0) -> dict | None:
    resp = _daemon_rpc(method, params, timeout)
    if resp and "error" not in resp:
        return resp
    return None

def _activate(meta: S.SessionMeta) -> None:
    # meta.env() now carries CHARA_PY_BACKEND (derived from the session's
    # isolation), so the jail backend is never re-derived here.
    os.environ.update(meta.env())


def _needs_setup(meta: S.SessionMeta) -> bool:
    return not meta.is_configured()


# ---- background daemon (persistent agents) ---------------------------------

def _start_daemon(meta: S.SessionMeta, patience: float | None = None) -> bool:
    """Spawn a detached background process where this agent lives on its own.

    The agent keeps thinking / creating in its workspace with no terminal
    attached. Returns True if it started (or was already running/starting)."""
    if meta.daemon_pid():  # also drops a STALE pid file (reboot pid-reuse / dead pid)
        return True
    if not meta.is_configured():
        return False
    # Claim daemon.pid atomically (O_EXCL) BEFORE spawning: two concurrent starts
    # both used to pass the falsy pid check and double-spawn the agent. The winner
    # writes the real pid after Popen; a loser finds the claim already held and
    # treats the chara as already starting. daemon_pid() reads an empty file as an
    # in-flight claim (never stale until its TTL lapses).
    meta.root.mkdir(parents=True, exist_ok=True)
    try:
        claim = meta.daemon_pid_path.open("x", encoding="utf-8")
    except FileExistsError:
        return True  # a concurrent starter holds the claim — already starting
    env = {**os.environ, **meta.env()}  # meta.env() carries CHARA_PY_BACKEND
    try:
        log = meta.daemon_log.open("ab")
        # --session is an INERT identity marker (terminal.py ignores it; the session
        # itself rides env): without it every daemon argv is identical, so after a
        # reboot pid_is_chara couldn't tell chara A's daemon from chara B's.
        argv = [sys.executable, "-m", "chara.front.terminal", "--session", meta.name]
        if patience is not None:
            argv += ["--patience", str(patience)]
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, env=env, cwd=str(APP_DIR),
        )
    except BaseException:
        claim.close()
        meta.daemon_pid_path.unlink(missing_ok=True)  # release the claim on spawn failure
        raise
    with claim:
        claim.write(str(proc.pid))
    meta.last_active = time.time()
    meta.save()
    return True


def _stop_daemon(meta: S.SessionMeta) -> str:
    """Returns a status: "stopped" | "not running" | "starting — try again shortly".
    Callers print it (or compare == "stopped"); never test its truthiness.

    daemon_pid() verifies the process IDENTITY (pid_is_chara, with the session's
    --session marker) — a reboot-reused pid comes back None (file dropped), so we
    never killpg an unrelated process or a sibling chara's daemon. A FRESH empty
    claim means a concurrent _start_daemon is mid-Popen: unlinking it would let the
    daemon come up with no pid file (unfindable, unstoppable), so the claim is left
    for the starter and the stop reports "starting" instead of silently clearing it.
    Only a genuinely stale file (dead/foreign pid, or a claim past its TTL) is
    removed here."""
    pid = meta.daemon_pid()
    if not pid:
        if meta.daemon_claim_active():
            return "starting — try again shortly"
        meta.daemon_pid_path.unlink(missing_ok=True)
        return "not running"
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    meta.daemon_pid_path.unlink(missing_ok=True)
    return "stopped"


def _launch_tui(meta: S.SessionMeta, args: argparse.Namespace) -> int:
    _activate(meta)
    if getattr(args, "debug", False):
        os.environ["CHARA_DEBUG"] = "1"
    # Attaching adopts a backgrounded agent: pause its daemon so the two don't
    # both write the session, then resume it in the background when we detach.
    was_daemon = meta.daemon_pid() is not None
    if was_daemon:
        _stop_daemon(meta)
    # Setup happens in the plain terminal (Hermes-style), BEFORE the full-screen
    # TUI takes over — you pick model + character without the screen being
    # hijacked. The full-screen settings editor is only for mid-session /settings.
    if _needs_setup(meta):
        from .wizard import run_wizard

        run_wizard()
    argv = [sys.argv[0]]
    if args.patience is not None:
        argv += ["--patience", str(args.patience)]
    # The interaction mode lives in the chara's own config; pass it down only when
    # the operator overrides it for this attach (--mode, or the legacy --no-forever).
    mode = args.mode or ("chat" if getattr(args, "no_forever", False) else "")
    if mode:
        argv += ["--mode", mode]
    if args.clean_on_exit:
        argv.append("--clean-on-exit")
    module = "chara.front.terminal" if args.plain else "chara.front.tui"
    meta.mark_running()
    old_argv = sys.argv
    try:
        sys.argv = argv
        runpy.run_module(module, run_name="__main__")
        return 0
    finally:
        sys.argv = old_argv
        meta.clear_running()
        # Hand the agent back to the background if it was living there.
        if was_daemon and meta.is_configured():
            _start_daemon(meta, patience=args.patience)


# ---- subcommands -----------------------------------------------------------


def cmd_tui(args: argparse.Namespace) -> int:
    """Resume-first launcher: show the roster of charas, act on the choice, repeat.

    There is NO default session — every session is a chara, deliberately created.
    `chara tui` opens the roster (pick or summon a chara). Bare `chara`
    opens the webui desktop instead — see main()."""
    if not sys.stdin.isatty():
        # Headless with no chara named: nothing to open (no default 'home').
        print("no chara specified — try `chara ls`, `chara attach NAME`, "
              "or `chara new NAME`.", file=sys.stderr)
        return 1
    from .roster import run_launcher

    first = True
    while True:
        result = run_launcher(animate=first)  # splash animates once, not on every return
        first = False
        if not result:
            return 0
        action, name = result
        if action == "attach":
            meta = S.load_session(name)
            if meta:
                _launch_tui(meta, args)
        elif action == "new":
            meta = _prompt_new_session()
            if meta:
                _launch_tui(meta, args)
        elif action == "start_all":
            _start_all()
        elif action == "stop" and name:
            meta = S.load_session(name)
            if meta:
                print(_stop_daemon(meta))


def _prompt_new_session() -> "S.SessionMeta | None":
    """Creating an agent is deliberate (each one is a persistent being)."""
    try:
        name = input("new chara name: ").strip()
        if not name:
            return None
        iso = (input(f"isolation {S.ISOLATION_LEVELS} [sandbox]: ").strip() or "sandbox")
        return S.create_session(name, isolation=iso)
    except (EOFError, KeyboardInterrupt):
        return None
    except (ValueError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return None


def _start_all() -> None:
    started = []
    for meta in S.list_sessions():
        if meta.is_configured() and not meta.daemon_pid() and not meta.running_pid():
            if _start_daemon(meta):
                started.append(meta.name)
    print(f"summoned {len(started)} chara into the background: {', '.join(started) or '(none)'}")


def cmd_new(args: argparse.Namespace) -> int:
    try:
        meta = S.create_session(args.name, isolation=args.isolation, note=args.note)
    except (ValueError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"created session {meta.name!r} ({meta.isolation}) at {meta.root}")
    if args.attach:
        return _launch_tui(meta, args)
    print(f"start it with: chara attach {meta.name}")
    return 0


def cmd_ls(_args: argparse.Namespace) -> int:
    rows = S.list_sessions()
    if not rows:
        print("no chara yet — run `chara` or `chara new NAME`")
        return 0
    print(f"{'NAME':<16} {'CHARACTER':<22} {'STATUS':<10} {'ISOLATION':<9} LAST ACTIVE")
    for m in rows:
        ts = m.last_active or m.created_at
        when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"
        print(f"{m.name:<16} {m.character_label():<22} {m.status():<10} {m.isolation:<9} {when}")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r} (see `chara ls`)", file=sys.stderr)
        return 1
    if meta.running_pid():
        print(f"error: chara {args.name!r} already has a TUI attached (pid {meta.running_pid()})", file=sys.stderr)
        return 1
    return _launch_tui(meta, args)


def cmd_start(args: argparse.Namespace) -> int:
    if getattr(args, "all", False) or args.name is None:
        _start_all()
        return 0
    delegated = _live_daemon_rpc("chara.start", {"name": args.name}, timeout=30.0)
    if delegated is not None:
        print(f"{args.name}: running under charad")
        return 0
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r}", file=sys.stderr)
        return 1
    if not meta.is_configured():
        print(f"error: chara {args.name!r} isn't set up yet — `chara attach {args.name}` first", file=sys.stderr)
        return 1
    _start_daemon(meta, patience=args.patience)
    print(f"{args.name}: running in the background (pid {meta.daemon_pid()}) · logs: {meta.daemon_log}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    if getattr(args, "all", False) or args.name is None:
        n = sum(1 for m in S.list_sessions() if _stop_daemon(m) == "stopped")
        print(f"stopped {n} background chara")
        return 0
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r}", file=sys.stderr)
        return 1
    print(f"{args.name}: {_stop_daemon(meta)}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    if not args.yes:
        try:
            ok = input(f"delete session {args.name!r} and its sandbox? [y/N] ").strip().lower() == "y"
        except EOFError:
            ok = False
        if not ok:
            print("aborted")
            return 1
    try:
        S.delete_session(args.name)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"deleted {args.name!r}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Headless one-shot (Claude Code's `-p`): send one message, print the reply.

    --stream-json emits one protocol event per line (JSONL) — the same wire
    format the future server/desktop clients consume; scripts can pipe it."""
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r} (see `chara ls`)", file=sys.stderr)
        return 1
    if not meta.is_configured():
        print(f"error: chara {args.name!r} isn't set up yet — `chara attach {args.name}` first", file=sys.stderr)
        return 1
    _activate(meta)
    from ..protocol import TextDelta, to_json
    from ..protocol.api import CharaHandle

    handle = CharaHandle()
    handle.attach()
    for ev in handle.stream_user(args.prompt):
        if args.stream_json:
            sys.stdout.write(to_json(ev) + "\n")
        elif isinstance(ev, TextDelta):
            sys.stdout.write(ev.text)
        sys.stdout.flush()
    if not args.stream_json:
        print()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r} (see `chara ls`)", file=sys.stderr)
        return 1
    if not meta.is_configured():
        print(f"error: chara {args.name!r} isn't set up yet — `chara setup {args.name}` first", file=sys.stderr)
        return 1
    if meta.running_pid():
        print(f"error: chara {args.name!r} already has an attached frontend (pid {meta.running_pid()})", file=sys.stderr)
        return 1
    if meta.daemon_pid():
        print(f"error: chara {args.name!r} is running in the background; stop it first", file=sys.stderr)
        return 1
    _activate(meta)
    if getattr(args, "debug", False):
        os.environ["CHARA_DEBUG"] = "1"
    old_term = None
    term_requested = False

    def _handle_sigterm(signum, frame):  # noqa: ARG001 - signal handler signature
        nonlocal term_requested
        term_requested = True
        meta.clear_running()
        raise SystemExit(143)

    try:
        old_term = signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError):
        old_term = None
    meta.mark_running()
    try:
        if args.stdio:
            from ..server.stdio import serve

            return serve()
        token = args.token or secrets.token_urlsafe(32)
        if not args.token:
            print(f"server token: {token}", file=sys.stderr, flush=True)
        print(
            f"serving {args.name!r} on ws://{args.host}:{args.port} "
            "(bind/expose publicly only if you intend to)",
            file=sys.stderr,
            flush=True,
        )
        try:
            from ..server.ws import serve_forever
        except ImportError as e:
            print(f"error: WebSocket transport requires websockets. Install with: uv sync --extra server ({e})", file=sys.stderr)
            return 1
        try:
            import asyncio

            asyncio.run(serve_forever(args.host, args.port, token))
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 0
        return 0
    finally:
        meta.clear_running()
        if old_term is not None and not term_requested:
            try:
                signal.signal(signal.SIGTERM, old_term)
            except (ValueError, OSError):
                pass


def cmd_gateway(args: argparse.Namespace) -> int:
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r} (see `chara ls`)", file=sys.stderr)
        return 1
    if not meta.is_configured():
        print(f"error: chara {args.name!r} isn't set up yet — `chara setup {args.name}` first", file=sys.stderr)
        return 1
    if meta.running_pid():
        print(f"error: chara {args.name!r} already has an attached frontend/gateway (pid {meta.running_pid()})", file=sys.stderr)
        return 1
    if meta.daemon_pid():
        print(f"error: chara {args.name!r} is running in the background; stop it first", file=sys.stderr)
        return 1
    _activate(meta)
    if getattr(args, "debug", False):
        os.environ["CHARA_DEBUG"] = "1"
    meta.mark_running()
    from ..server.supervisor import GATEWAY_FATAL_EXIT
    try:
        from ..messaging.gateway import MessagingGateway, load_config

        cfg = load_config()
        # The gateway respects the chara's own cadence: explicit --patience wins,
        # else the chara's configured patience, else the safe 600s default.
        patience = args.patience
        if patience is None:
            from ..session.settings import load_settings
            patience = getattr(load_settings(), "patience", None)
        print(f"messaging gateway for {args.name!r}: starting {', '.join(cfg.get('adapters', {}).keys())}", file=sys.stderr)
        MessagingGateway.from_config(cfg, patience=patience).run()
        return 0
    except FileNotFoundError:
        # No messaging.json: a configuration problem, not a transient crash —
        # the supervisor must not retry it (EX_CONFIG → fatal state).
        print(f"error: missing messaging config: {meta.root / 'messaging.json'}", file=sys.stderr)
        return GATEWAY_FATAL_EXIT
    except ValueError as e:
        # Malformed config / unknown adapter / missing required field: fatal
        # until the operator fixes it, never an auto-restart loop.
        print(f"error: messaging config is invalid: {e}", file=sys.stderr)
        return GATEWAY_FATAL_EXIT
    except KeyboardInterrupt:
        return 0
    finally:
        meta.clear_running()


def cmd_desktop(args: argparse.Namespace) -> int:
    """The desktop app: resident supervisor + web renderer."""
    from ..server import netsec as N
    from ..server.desktop import daemonize_desktop, free_port, serve_desktop
    from ..server.supervisor import daemon_alive, read_daemon_json

    if getattr(args, "debug", False):
        os.environ["CHARA_DEBUG"] = "1"
    # Distribution lock: set the env BEFORE spawning so the supervisor + every chara child
    # (which inherit it) pin to the sandbox jail and refuse admin. Env or flag, either works.
    if getattr(args, "force_sandbox", False):
        os.environ["CHARA_FORCE_SANDBOX"] = "1"
    from ..session.isolation import force_sandbox
    if force_sandbox():
        # Persistently downgrade any existing admin chara to sandbox at startup, so it
        # STAYS sandbox even after the lock is later removed (the toggle re-enables then).
        from ..session.sessions import downgrade_admin_sessions
        downgraded = downgrade_admin_sessions()
        if downgraded:
            print(f"force-sandbox: downgraded {len(downgraded)} chara(s) to the sandbox: {', '.join(downgraded)}")
    host = getattr(args, "host", None) or "127.0.0.1"
    # An explicit token may come from --token OR the CHARA_TOKEN env (the Docker
    # entrypoint sets the latter, generating one if unset). Either counts as the
    # operator vouching for a known token; absent both, a per-run random is used.
    explicit_token = args.token or os.environ.get("CHARA_TOKEN") or ""
    token = explicit_token or secrets.token_urlsafe(24)
    allow = [h.strip() for h in (getattr(args, "allow_host", None) or "").split(",") if h.strip()]

    # A non-loopback bind exposes a shell + tools to the network; the token is
    # the gate. A wildcard bind without an explicit token is refused outright —
    # the random per-run token would be unknown to a remote client. (Login is a
    # later iteration; the shared token is the gate today — plan §2, Track D.)
    if N.is_wildcard_host(host) and not explicit_token:
        print(
            "error: refusing to bind 0.0.0.0 without a token. Pass --token <secret> "
            "(or set CHARA_TOKEN) so remote clients can authenticate — the token "
            "is the access gate.",
            file=sys.stderr,
        )
        return 2

    # OPTIONAL password login (plan §4b) — an ALTERNATIVE to the token URL for a
    # public bind: the operator bookmarks https://host/ and types a password. It
    # is ADDITIVE (the token gate is unchanged) and INERT for a loopback bind —
    # the local Electron/SSH app never sees a login screen. For a non-loopback
    # bind we resolve a password (CHARA_PASSWORD env, else an existing
    # auth.json, else generate one and print it ONCE) and store only its hash.
    pw_record = None
    if not N.is_loopback_host(host):
        from ..server import authpw as AUTHPW

        try:
            _enabled, generated = AUTHPW.ensure_password(
                env_password=os.environ.get("CHARA_PASSWORD")
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        pw_record = AUTHPW.load_record()
        if generated:
            print(
                "password login enabled — bookmark the host and log in with:\n"
                f"  password: {generated}\n"
                "  (shown once; stored hashed in ~/.chara/auth.json — "
                "set CHARA_PASSWORD to choose your own)",
                file=sys.stderr,
                flush=True,
            )
        elif pw_record is not None:
            print(
                "password login enabled (using CHARA_PASSWORD / stored auth.json).",
                file=sys.stderr,
                flush=True,
            )

    # A non-loopback bind targets a reverse proxy, which needs STABLE, pinnable
    # ports — a random bind-0 pair is unproxiable. So for a non-loopback bind
    # default the HTTP port to 6180 even when --port is unset, so a bare
    # `--host 0.0.0.0 --no-open` is proxiable (not random). Loopback keeps auto.
    req_port = args.port
    if req_port == 0 and not N.is_loopback_host(host):
        req_port = 6180

    # WS port → bind 0 (OS-assigned, collision-free) for a loopback bind; the
    # supervisor bakes the chosen port into the printed URL + daemon.json.
    # --ws-port honored if given. For a non-loopback bind, default it next to the
    # HTTP port (http+1) so the README's Caddyfile can path-route /hub,/chara/* → it.
    ws_port = args.ws_port or 0
    if ws_port == 0 and req_port and not N.is_loopback_host(host):
        ws_port = req_port + 1

    # HTTP port handling (D2): if the requested port is taken, attach to OUR live
    # daemon (don't double-spawn); fail with attribution if it's a foreign holder.
    http_port = req_port
    if http_port not in (0, None):
        data = read_daemon_json()
        if int(data.get("http_port") or 0) == int(http_port) and daemon_alive(data):
            print(
                f"charad already running · http:{data.get('http_port')} ws:{data.get('ws_port')}"
                f" · {data.get('path') or ''}".rstrip(),
            )
            return 0
        if N.port_in_use(host, http_port):
            holder = N.describe_port_holder(http_port)
            print(
                f"error: HTTP port {http_port} held by {holder}\n"
                "       stop it, or pass --port <other> (or --port 0 for any free port).",
                file=sys.stderr,
            )
            return 1
    elif http_port is None:
        http_port = free_port(host)

    if getattr(args, "daemon", False) and not os.getenv("CHARA_DAEMON_CHILD"):
        info = daemonize_desktop(host, http_port, ws_port, token,
                                 debug=bool(getattr(args, "debug", False)), allow_hosts=allow)
        print(f"charad pid {info['pid']} · http:{info['http_port']} ws:{info['ws_port']} · {info.get('path', '')}")
        return 0
    return serve_desktop(host, http_port, ws_port, token, allow_hosts=allow,
                         pw_record=pw_record,
                         open_browser=(not args.no_open and not os.getenv("CHARA_DAEMON_CHILD")))


def cmd_connect(args: argparse.Namespace) -> int:
    """Reach a remote chara over an SSH tunnel: `chara connect ssh://host`.

    The remote charad stays bound to 127.0.0.1; we forward its HTTP + WS
    ports through `ssh -L` and open the browser at the tunneled localhost URL.
    SSH provides encryption + auth; the server is never exposed (plan §3/§9)."""
    from ..server.sshconnect import ConnectError, connect

    try:
        return connect(args.target, open_browser=not args.no_open)
    except ConnectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from ..server.supervisor import daemon_status, stop_daemon_process

    if args.action == "stop":
        print("stopped charad" if stop_daemon_process() else "charad not running")
        return 0
    st = daemon_status()
    if not st.get("alive"):
        print(f"charad: stopped ({st.get('path')})")
        return 1
    print(f"charad: running pid {st.get('pid')} · http:{st.get('http_port')} ws:{st.get('ws_port')}")
    resp = _live_daemon_rpc("sessions.list", {}, timeout=10.0)
    if resp and isinstance(resp.get("result"), list):
        for row in resp["result"]:
            gw = row.get("gateway") or {}
            life = row.get("life") or {}
            print(
                f"  {row.get('name')}: chara={(row.get('chara') or {}).get('state') or row.get('status')}"
                f" life={life.get('state') or '-'}"
                f" gateway={gw.get('state') or '-'} {gw.get('detail') or ''}"
            )
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    # `chara setup browser` installs the optional agent-browser driver +
    # Chromium that the browser_* tools are gated on — not a chara named
    # "browser". (No chara may be named "browser"; the name is reserved here.)
    if args.name == "browser":
        return cmd_setup_browser(args)
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r} (see `chara ls` or `chara new {args.name}`)", file=sys.stderr)
        return 1
    _activate(meta)
    from .wizard import run_wizard

    run_wizard(non_interactive_ok=False)
    return 0


def cmd_setup_browser(args: argparse.Namespace) -> int:
    """Install the Node `agent-browser` CLI + its Chromium so the browser_*
    tools work. Actually installs (idempotent); `--check` only reports.

    The 12 browser tools stay hidden (check_fn-gated) until BOTH the CLI and a
    Chromium build are present. They run under the default `sandbox` isolation as
    well as `admin` — `session.isolation.build_jail_command(browser=True)` uses a
    Chromium-capable jail (writes confined to workspace+temp, secret home hidden,
    --no-sandbox auto-injected). Validated macOS + Linux/bwrap 2026-06-19."""
    from ..protocol.api import browser_driver_status

    check = bool(getattr(args, "check", False))
    cli, chromium = browser_driver_status()

    print("chara setup browser — the browser_* tool driver\n")
    print(f"  {'✓' if cli else '✗'} agent-browser CLI" + (f" — {cli}" if cli else " — not found"))
    print(f"  {'✓' if chromium else '✗'} Chromium build" + ("" if chromium else " — not found"))

    if cli and chromium:
        print("\nBrowser driver is ready — the browser_* tools work under both")
        print("`sandbox` and `admin` isolation.")
        return 0
    if check:
        print("\nNot installed. Run `chara setup browser` (no --check) to install.")
        return 1

    npm = shutil.which("npm")
    if not npm:
        print("\n✗ Node.js (node + npm) is required and was not found.")
        print("  Install Node 18+ first, then re-run `chara setup browser`:")
        print("    Linux:  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && apt-get install -y nodejs")
        print("    macOS:  brew install node")
        return 1

    print(f"\n  npm: {npm}\n")

    def _run(cmd: list) -> int:
        """Run an install step, treating a missing/unrunnable binary as failure
        rather than crashing the command."""
        try:
            return subprocess.run(cmd).returncode
        except (OSError, ValueError) as e:
            print(f"  ({' '.join(cmd)} could not run: {e})")
            return 1

    if not cli:
        print("→ npm install -g agent-browser ...")
        if _run([npm, "install", "-g", "agent-browser"]) != 0:
            print("\n✗ `npm install -g agent-browser` failed.", file=sys.stderr)
            return 1
    # agent-browser install --with-deps downloads its own Chrome (+ system libs
    # on Linux). Re-resolve the CLI after the npm step so we can call it.
    ab = shutil.which("agent-browser") or "agent-browser"
    print("→ agent-browser install --with-deps  (downloads Chromium) ...")
    if _run([ab, "install", "--with-deps"]) != 0:
        # --with-deps needs root on Linux for the apt step; retry plain.
        print("  (--with-deps failed; retrying `agent-browser install` without system deps)")
        if _run([ab, "install"]) != 0:
            print("\n✗ Chromium download failed.", file=sys.stderr)
            return 1

    # Shim the crashpad handler so Chrome survives under the OS jail (Landlock).
    from ..protocol.api import apply_browser_runtime_fixups
    apply_browser_runtime_fixups()
    cli, chromium = browser_driver_status()  # re-probes (resets the driver cache)
    print(f"\n  {'✓' if cli else '✗'} agent-browser CLI")
    print(f"  {'✓' if chromium else '✗'} Chromium build")
    if cli and chromium:
        print("\n✓ Browser driver ready — browser_* tools work under sandbox + admin.")
        return 0
    print("\n✗ Driver still not detected; see output above.", file=sys.stderr)
    return 1


def _uv_path() -> str:
    from ..config import find_uv
    return find_uv() or str(S.chara_home() / "bin" / "uv")


def cmd_update(args: argparse.Namespace) -> int:
    # Two channels (mirrors install.sh): a git checkout is the DEV/edge channel
    # (git pull + uv sync); anything else is a wheel install from a GitHub
    # Release, upgraded in place via `uv tool upgrade`.
    if (APP_DIR / ".git").exists():
        return _update_dev_checkout(args)
    return _update_wheel(args)


def _update_dev_checkout(args: argparse.Namespace) -> int:
    git = shutil.which("git")
    if not git:
        print("error: git not found", file=sys.stderr)
        return 1
    if args.check:
        behind = _commits_behind()
        if behind is None:
            print("could not reach origin")
            return 1
        print("up to date" if behind == 0 else f"{behind} commit(s) behind — run `chara update`")
        return 0
    print(f"updating dev checkout {APP_DIR} ...")
    steps = [[git, "-C", str(APP_DIR), "pull", "--ff-only", "origin", "main"]]
    uv = _uv_path()
    if Path(uv).exists() or shutil.which("uv"):
        steps.append([uv, "sync", "--project", str(APP_DIR)])
    for cmd in steps:
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print(f"error: {' '.join(map(str, cmd))} failed", file=sys.stderr)
            return proc.returncode
    print("note: dev checkout — rebuild the served UI after frontend edits:")
    print(f"  cd {APP_DIR / 'apps' / 'web'} && npm ci && npm run build")
    _write_update_stamp(behind=0)
    print("updated.")
    return 0


def _update_wheel(args: argparse.Namespace) -> int:
    # Wheel install (the user channel): reinstall from the LATEST release wheel URL
    # via the shared self-update core. `uv tool upgrade` is a no-op on a URL-pinned
    # tool (install.sh pins `chara @ <wheel-url>`), so it could never upgrade —
    # the core fetches the newest release wheel and `uv tool install --force`s it.
    # The wheel carries the built frontend (front/webui/), so there's no node step.
    from .. import updater
    if args.check:
        url = updater.latest_wheel_url()
        print("could not reach GitHub to check for updates" if not url
              else f"latest release wheel: {url}\nrun `chara update` to install it")
        return 0 if url else 1
    print("updating chara (wheel) — fetching the latest release ...")
    result = updater.apply()
    print(result.get("output") or "")
    if not result.get("ok"):
        # apply()'s output already includes the manual command; nudge to it explicitly.
        print(f"\nupdate failed. To update by hand:\n  {updater.manual_command()}", file=sys.stderr)
        return 1
    print("updated. restart chara to run the new version.")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    def line(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))

    print(f"chara {__version__} @ {APP_DIR}")
    line("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    line("uv", bool(shutil.which("uv")), shutil.which("uv") or "missing (install.sh provides one)")
    _is_git = (APP_DIR / ".git").exists()
    line("install channel", True, "dev (git checkout)" if _is_git else "wheel (GitHub Release)")
    if sys.platform == "darwin":
        line("sandbox-exec (simple sandbox)", bool(shutil.which("sandbox-exec")))
    else:
        line("bubblewrap (simple sandbox)", bool(shutil.which("bwrap")), "install: apt/dnf install bubblewrap")
    # Optional browser_* tool driver (hidden until installed; `chara setup browser`).
    try:
        from ..protocol.api import browser_driver_status
        _cli, _chromium = browser_driver_status()
        line("browser tools (optional)", bool(_cli) and _chromium,
             "ready" if (_cli and _chromium) else "run `chara setup browser` to enable")
    except Exception:
        line("browser tools (optional)", False, "run `chara setup browser`")
    # ffmpeg backs the chara's video/audio work (best-effort installed by install.sh).
    _ffmpeg = shutil.which("ffmpeg")
    _ffmpeg_hint = "brew install ffmpeg" if sys.platform == "darwin" else "apt/dnf install ffmpeg"
    line("ffmpeg (optional)", bool(_ffmpeg), _ffmpeg or f"missing — {_ffmpeg_hint}")
    print(f"  home: {S.chara_home()}  sessions: {len(S.list_sessions())}")
    # Diagnostics live per chara, next to its audit trail.
    for m in S.list_sessions():
        log_dir = m.sandbox_dir / "logs"
        err_file = log_dir / "errors.log"
        last_error = ""
        try:
            lines = [ln for ln in err_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                last_error = f"  last error: {lines[-1][:120]}"
        except OSError:
            pass
        print(f"  logs[{m.name}]: {log_dir}{last_error}")
    return 0


def cmd_version(_args: argparse.Namespace) -> int:
    print(f"chara {__version__}")
    return 0


# ---- update hint (cheap, cached, fail-silent) ------------------------------

_STAMP = "update_check.json"


def _commits_behind() -> int | None:
    git = shutil.which("git")
    if not git or not (APP_DIR / ".git").exists():
        return None
    try:
        subprocess.run(
            [git, "-C", str(APP_DIR), "fetch", "--quiet", "origin", "main"],
            timeout=5, check=True, capture_output=True,
        )
        out = subprocess.run(
            [git, "-C", str(APP_DIR), "rev-list", "--count", "HEAD..origin/main"],
            timeout=5, check=True, capture_output=True, text=True,
        )
        return int(out.stdout.strip())
    except Exception:
        return None


def _write_update_stamp(behind: int) -> None:
    try:
        S.chara_home().mkdir(parents=True, exist_ok=True)
        (S.chara_home() / _STAMP).write_text(json.dumps({"t": time.time(), "behind": behind}))
    except OSError:
        pass


def _maybe_update_hint() -> None:
    """At most once a day, mention available updates. Never blocks, never raises."""
    stamp = S.chara_home() / _STAMP
    try:
        data = json.loads(stamp.read_text())
        if time.time() - data.get("t", 0) < 86400:
            if data.get("behind", 0) > 0:
                print(f"(update available: {data['behind']} commit(s) behind — `chara update`)")
            return
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    behind = _commits_behind()
    if behind is None:
        return
    _write_update_stamp(behind)
    if behind > 0:
        print(f"(update available: {behind} commit(s) behind — `chara update`)")


# ---- parser ----------------------------------------------------------------


def _add_tui_flags(p: argparse.ArgumentParser, *, suppress: bool = False) -> None:
    # The same TUI flags live on the top-level parser AND on the `tui` subparser,
    # so they work in either position (`chara --plain tui` and `chara tui
    # --plain`). On the subparser the defaults are SUPPRESS: an absent flag then
    # leaves the attribute untouched instead of clobbering a value the top-level
    # parser already set (the classic argparse parent/subparser default trap).
    none_def = argparse.SUPPRESS if suppress else None
    str_def = argparse.SUPPRESS if suppress else ""
    false_def = argparse.SUPPRESS if suppress else False
    p.add_argument("--patience", "--cooldown", dest="patience", type=float, default=none_def,
                   help="override pause between the chara's spontaneous cycles, in seconds")
    p.add_argument("--mode", choices=["live", "chat"], default=str_def,
                   help="override the chara's interaction mode for this attach (live: keeps creating while you watch; chat: only replies)")
    p.add_argument("--no-forever", action="store_true", default=false_def, help=argparse.SUPPRESS)  # pre-rename alias for --mode chat
    p.add_argument("--plain", action="store_true", default=false_def, help="legacy plain terminal instead of the TUI")
    p.add_argument("--clean-on-exit", action="store_true", default=false_def, help="wipe the session sandbox on shutdown (default: persist)")
    p.add_argument("--debug", action="store_true", default=false_def, help="DEBUG-level diagnostics in the chara's sandbox/logs/")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chara", description="OpenCharaAgent — agentic character tavern")
    p.add_argument("--version", action="version", version=f"chara {__version__}")
    _add_tui_flags(p)
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("new", help="create a session")
    sp.add_argument("name")
    sp.add_argument("--isolation", choices=S.ISOLATION_LEVELS, default="sandbox")
    sp.add_argument("--note", default="")
    sp.add_argument("--attach", action="store_true", help="open it immediately")
    _add_tui_flags(sp)
    sp.set_defaults(func=cmd_new)

    sp = sub.add_parser("ls", aliases=["list"], help="list sessions")
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("attach", help="open a session in the TUI")
    sp.add_argument("name")
    _add_tui_flags(sp)
    sp.set_defaults(func=cmd_attach)

    sp = sub.add_parser("start", help="let an agent live in the background; --all for every agent")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--all", action="store_true", help="start every configured agent")
    sp.add_argument("--patience", "--cooldown", dest="patience", type=float, default=None)
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("start-all", help="start every configured agent in the background (e.g. after a reboot)")
    sp.add_argument("--patience", "--cooldown", dest="patience", type=float, default=None)
    sp.set_defaults(func=lambda a: (_start_all() or 0))

    sp = sub.add_parser("stop", help="stop an agent's background loop; --all to stop every agent")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("rm", help="delete a session")
    sp.add_argument("name")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser("run", help="headless one-shot: -p sends a message and prints the reply")
    sp.add_argument("name")
    sp.add_argument("-p", "--prompt", required=True, help="the message to send")
    sp.add_argument("--stream-json", action="store_true", help="one protocol event per line (JSONL wire format)")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("serve", help="serve one session over JSON-RPC (stdio or WebSocket)")
    sp.add_argument("name")
    sp.add_argument("--stdio", action="store_true", help="speak newline-delimited JSON-RPC on stdin/stdout")
    sp.add_argument("--host", default="127.0.0.1", help="WebSocket bind host (default: 127.0.0.1)")
    sp.add_argument("--port", type=int, default=8137, help="WebSocket bind port (default: 8137)")
    sp.add_argument("--token", default="", help="WebSocket bearer token (auto-generated if omitted)")
    sp.add_argument("--debug", action="store_true", help="DEBUG-level diagnostics in the chara's sandbox/logs/")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("gateway", help="run one session's configured messaging adapters")
    sp.add_argument("name")
    sp.add_argument("--patience", "--cooldown", dest="patience", type=float, default=None,
                    help="override pause between the chara's spontaneous cycles, in seconds")
    sp.add_argument("--debug", action="store_true", help="DEBUG-level diagnostics in the chara's sandbox/logs/")
    sp.set_defaults(func=cmd_gateway)

    sp = sub.add_parser("desktop", help="open the desktop app (web renderer + hub gateway)")
    sp.add_argument("--host", default="127.0.0.1",
                    help="address to bind (default: 127.0.0.1; a non-loopback host exposes the chara to the network and needs --token; 0.0.0.0 is refused without one)")
    sp.add_argument("--allow-host", default="",
                    help="comma-separated extra Host/Origin names to allow (for a reverse proxy in front of a public bind)")
    sp.add_argument("--port", type=int, default=0, help="HTTP port for the renderer (default: auto)")
    sp.add_argument("--ws-port", type=int, default=0, help="WebSocket port for the gateway (default: auto)")
    sp.add_argument("--token", default="", help="gateway token (auto-generated if omitted)")
    sp.add_argument("--no-open", action="store_true", help="don't open the browser")
    sp.add_argument("--daemon", action="store_true", help="start charad in the background")
    sp.add_argument("--force-sandbox", action="store_true",
                    help="pin every chara to the sandbox jail; disable admin isolation (for distributing the service). "
                         "Equivalent to CHARA_FORCE_SANDBOX=1.")
    sp.add_argument("--debug", action="store_true", help="DEBUG-level diagnostics")
    sp.set_defaults(func=cmd_desktop)

    sp = sub.add_parser("tui", help="open the terminal roster (the resume-first launcher); bare `chara` opens the webui")
    _add_tui_flags(sp, suppress=True)  # accept --plain/--mode/etc. after `tui` too
    sp.set_defaults(func=cmd_tui)

    sp = sub.add_parser("connect", help="reach a remote chara over an SSH tunnel: connect ssh://[user@]host[:port]")
    sp.add_argument("target", help="ssh target, e.g. ssh://user@host:22 (encryption + auth via SSH; the remote stays bound to 127.0.0.1)")
    sp.add_argument("--no-open", action="store_true", help="don't open the browser")
    sp.set_defaults(func=cmd_connect)

    sp = sub.add_parser("daemon", help="manage the resident desktop supervisor")
    sp.add_argument("action", choices=["stop", "status"])
    sp.set_defaults(func=cmd_daemon)

    sp = sub.add_parser("setup", help="(re)run the setup wizard; `setup browser` installs the browser_* tool driver")
    sp.add_argument("name", help="chara name, or `browser` to set up the optional browser tool driver")
    sp.add_argument("--check", action="store_true", help="(browser) report status only, don't print install guidance")
    sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser("update", help="update the installed checkout")
    sp.add_argument("--check", action="store_true", help="only check, do not install")
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("doctor", help="check environment & sandbox backends")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("version", help="print version")
    sp.set_defaults(func=cmd_version)

    return p


def main(argv: list[str] | None = None) -> int:
    from ..config import migrate_legacy_home
    migrate_legacy_home()  # ~/.lunamoth → ~/.chara, once, before any path is used
    args = build_parser().parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        # Bare `chara` opens the webui desktop (the primary face); `chara tui`
        # opens the terminal roster. Seed the desktop defaults the `desktop`
        # subparser would have supplied, so cmd_desktop can read them uniformly.
        _maybe_update_hint()
        for k, v in (("host", "127.0.0.1"), ("allow_host", ""), ("port", 0),
                     ("ws_port", 0), ("token", ""), ("no_open", False), ("daemon", False)):
            if not hasattr(args, k):
                setattr(args, k, v)
        return cmd_desktop(args)
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
