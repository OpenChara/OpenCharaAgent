"""The `lunamoth` command — a roster of persistent agents, not throwaway sessions.

    lunamoth                 open the roster (resume-first launcher)
    lunamoth new NAME        create an agent (--isolation dir|sandbox|docker)
    lunamoth ls              list agents and their status
    lunamoth attach NAME     open an agent in the TUI (adopts its background loop)
    lunamoth start [NAME]    run an agent in the background; --all / `start-all`
    lunamoth stop [NAME]     stop an agent's background loop; --all
    lunamoth rm NAME         delete an agent
    lunamoth setup [NAME]    (re)run the setup wizard
    lunamoth setup browser   install the optional agent-browser tool driver
    lunamoth update          update to the latest release (wheel; dev checkout = git pull + uv sync)
    lunamoth doctor          check environment & sandbox backends

Each agent is a persistent being: it lives in the background on its own and
you attach/detach. `start-all` brings them all back after a reboot. Remote
baseline: `ssh host -t lunamoth attach NAME`; future gateways reuse
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

APP_DIR = Path(__file__).resolve().parents[3]  # repo checkout (dev or ~/.lunamoth/app)


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

# session isolation level -> python tool execution backend
_ISOLATION_TO_BACKEND = {"dir": "local", "sandbox": "sandbox", "docker": "docker"}


def _activate(meta: S.SessionMeta) -> None:
    os.environ.update(meta.env())
    os.environ.setdefault("LUNAMOTH_PY_BACKEND", _ISOLATION_TO_BACKEND[meta.isolation])


def _needs_setup(meta: S.SessionMeta) -> bool:
    return not meta.is_configured()


# ---- background daemon (persistent agents) ---------------------------------

def _start_daemon(meta: S.SessionMeta, patience: float | None = None) -> bool:
    """Spawn a detached background process where this agent lives on its own.

    The agent keeps thinking / creating in its workspace with no terminal
    attached. Returns True if it started (or was already running)."""
    if meta.daemon_pid():
        return True
    if not meta.is_configured():
        return False
    env = {**os.environ, **meta.env()}
    env.setdefault("LUNAMOTH_PY_BACKEND", _ISOLATION_TO_BACKEND[meta.isolation])
    log = meta.daemon_log.open("ab")
    argv = [sys.executable, "-m", "lunamoth.front.terminal"]
    if patience is not None:
        argv += ["--patience", str(patience)]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, env=env, cwd=str(APP_DIR),
    )
    meta.daemon_pid_path.write_text(str(proc.pid), encoding="utf-8")
    meta.last_active = time.time()
    meta.save()
    return True


def _stop_daemon(meta: S.SessionMeta) -> bool:
    pid = meta.daemon_pid()
    if not pid:
        meta.daemon_pid_path.unlink(missing_ok=True)
        return False
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    meta.daemon_pid_path.unlink(missing_ok=True)
    return True


def _launch_tui(meta: S.SessionMeta, args: argparse.Namespace) -> int:
    _activate(meta)
    if getattr(args, "debug", False):
        os.environ["LUNAMOTH_DEBUG"] = "1"
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
    module = "lunamoth.front.terminal" if args.plain else "lunamoth.front.tui"
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


def cmd_default(args: argparse.Namespace) -> int:
    """Resume-first launcher: show the roster of charas, act on the choice, repeat.

    There is NO default session — every session is a chara, deliberately created.
    `lunamoth` with no args opens the roster (pick or summon a chara)."""
    if not sys.stdin.isatty():
        # Headless with no chara named: nothing to open (no default 'home').
        print("no chara specified — try `lunamoth ls`, `lunamoth attach NAME`, "
              "or `lunamoth new NAME`.", file=sys.stderr)
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
                print("stopped" if _stop_daemon(meta) else "not running")


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
    print(f"start it with: lunamoth attach {meta.name}")
    return 0


def cmd_ls(_args: argparse.Namespace) -> int:
    rows = S.list_sessions()
    if not rows:
        print("no chara yet — run `lunamoth` or `lunamoth new NAME`")
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
        print(f"error: no chara named {args.name!r} (see `lunamoth ls`)", file=sys.stderr)
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
        print(f"{args.name}: running under lunamothd")
        return 0
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r}", file=sys.stderr)
        return 1
    if not meta.is_configured():
        print(f"error: chara {args.name!r} isn't set up yet — `lunamoth attach {args.name}` first", file=sys.stderr)
        return 1
    _start_daemon(meta, patience=args.patience)
    print(f"{args.name}: running in the background (pid {meta.daemon_pid()}) · logs: {meta.daemon_log}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    if getattr(args, "all", False) or args.name is None:
        n = sum(1 for m in S.list_sessions() if _stop_daemon(m))
        print(f"stopped {n} background chara")
        return 0
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r}", file=sys.stderr)
        return 1
    print(f"{args.name}: stopped" if _stop_daemon(meta) else f"{args.name}: not running")
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
        print(f"error: no chara named {args.name!r} (see `lunamoth ls`)", file=sys.stderr)
        return 1
    if not meta.is_configured():
        print(f"error: chara {args.name!r} isn't set up yet — `lunamoth attach {args.name}` first", file=sys.stderr)
        return 1
    _activate(meta)
    from ..protocol import TextDelta, to_json
    from ..protocol.api import CharaHandle

    handle = CharaHandle()
    handle.attach(present=True)
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
        print(f"error: no chara named {args.name!r} (see `lunamoth ls`)", file=sys.stderr)
        return 1
    if not meta.is_configured():
        print(f"error: chara {args.name!r} isn't set up yet — `lunamoth setup {args.name}` first", file=sys.stderr)
        return 1
    if meta.running_pid():
        print(f"error: chara {args.name!r} already has an attached frontend (pid {meta.running_pid()})", file=sys.stderr)
        return 1
    if meta.daemon_pid():
        print(f"error: chara {args.name!r} is running in the background; stop it first", file=sys.stderr)
        return 1
    _activate(meta)
    if getattr(args, "debug", False):
        os.environ["LUNAMOTH_DEBUG"] = "1"
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
        print(f"error: no chara named {args.name!r} (see `lunamoth ls`)", file=sys.stderr)
        return 1
    if not meta.is_configured():
        print(f"error: chara {args.name!r} isn't set up yet — `lunamoth setup {args.name}` first", file=sys.stderr)
        return 1
    if meta.running_pid():
        print(f"error: chara {args.name!r} already has an attached frontend/gateway (pid {meta.running_pid()})", file=sys.stderr)
        return 1
    if meta.daemon_pid():
        print(f"error: chara {args.name!r} is running in the background; stop it first", file=sys.stderr)
        return 1
    _activate(meta)
    if getattr(args, "debug", False):
        os.environ["LUNAMOTH_DEBUG"] = "1"
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
        os.environ["LUNAMOTH_DEBUG"] = "1"
    host = getattr(args, "host", None) or "127.0.0.1"
    token = args.token or secrets.token_urlsafe(24)
    allow = [h.strip() for h in (getattr(args, "allow_host", None) or "").split(",") if h.strip()]

    # A non-loopback bind exposes a shell + tools to the network; the token is
    # the gate. A wildcard bind without an explicit token is refused outright —
    # the random per-run token would be unknown to a remote client. (Login is a
    # later iteration; the shared token is the gate today — plan §2, Track D.)
    if N.is_wildcard_host(host) and not args.token:
        print(
            "error: refusing to bind 0.0.0.0 without a token. Pass --token <secret> "
            "so remote clients can authenticate (the token is the access gate).",
            file=sys.stderr,
        )
        return 2

    # WS port → bind 0 (OS-assigned, collision-free); the supervisor bakes the
    # chosen port into the printed URL + daemon.json. --ws-port honored if given.
    ws_port = args.ws_port or 0

    # HTTP port handling (D2): if the requested port is taken, attach to OUR live
    # daemon (don't double-spawn); fail with attribution if it's a foreign holder.
    http_port = args.port
    if http_port not in (0, None):
        data = read_daemon_json()
        if int(data.get("http_port") or 0) == int(http_port) and daemon_alive(data):
            print(
                f"lunamothd already running · http:{data.get('http_port')} ws:{data.get('ws_port')}"
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

    if getattr(args, "daemon", False) and not os.getenv("LUNAMOTH_DAEMON_CHILD"):
        info = daemonize_desktop(host, http_port, ws_port, token,
                                 debug=bool(getattr(args, "debug", False)), allow_hosts=allow)
        print(f"lunamothd pid {info['pid']} · http:{info['http_port']} ws:{info['ws_port']} · {info.get('path', '')}")
        return 0
    return serve_desktop(host, http_port, ws_port, token, allow_hosts=allow,
                         open_browser=(not args.no_open and not os.getenv("LUNAMOTH_DAEMON_CHILD")))


def cmd_daemon(args: argparse.Namespace) -> int:
    from ..server.supervisor import daemon_status, stop_daemon_process

    if args.action == "stop":
        print("stopped lunamothd" if stop_daemon_process() else "lunamothd not running")
        return 0
    st = daemon_status()
    if not st.get("alive"):
        print(f"lunamothd: stopped ({st.get('path')})")
        return 1
    print(f"lunamothd: running pid {st.get('pid')} · http:{st.get('http_port')} ws:{st.get('ws_port')}")
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
    # `lunamoth setup browser` installs the optional agent-browser driver +
    # Chromium that the browser_* tools are gated on — not a chara named
    # "browser". (No chara may be named "browser"; the name is reserved here.)
    if args.name == "browser":
        return cmd_setup_browser(args)
    meta = S.load_session(args.name)
    if meta is None:
        print(f"error: no chara named {args.name!r} (see `lunamoth ls` or `lunamoth new {args.name}`)", file=sys.stderr)
        return 1
    _activate(meta)
    from .wizard import run_wizard

    run_wizard(non_interactive_ok=False)
    return 0


def cmd_setup_browser(args: argparse.Namespace) -> int:
    """Install the Node `agent-browser` CLI + its Chromium so the browser_*
    tools can be enabled. Honest about prerequisites; never pretends to succeed.

    The 12 browser tools stay hidden (check_fn-gated) until BOTH the CLI and a
    Chromium build are present. A real Chromium will NOT launch under the default
    `sandbox` isolation (sandbox-exec/bwrap block namespaces, /dev/shm, sockets)
    — the browser toolpack needs `dir` or `docker` isolation, plus --no-sandbox
    (the driver injects that automatically as root / under AppArmor userns)."""
    from ..protocol.api import browser_driver_status

    check = bool(getattr(args, "check", False))
    cli, chromium = browser_driver_status()

    print("lunamoth setup browser — the optional browser_* tool driver\n")
    print(f"  {'✓' if cli else '✗'} agent-browser CLI" + (f" — {cli}" if cli else " — not found"))
    print(f"  {'✓' if chromium else '✗'} Chromium build" + ("" if chromium else " — not found"))

    if cli and chromium:
        print("\nBrowser driver is ready. Enable the browser toolpack on a chara")
        print("running under `dir` or `docker` isolation (a real Chromium will not")
        print("launch under the default `sandbox` isolation).")
        return 0

    node = shutil.which("node")
    npm = shutil.which("npm")
    if not (node and npm):
        print("\n✗ Node.js is required (node + npm) and was not found.")
        print("  Install Node 18+ first: https://nodejs.org  (or `brew install node`).")
        print("  Then re-run: lunamoth setup browser")
        return 1

    print(f"\n  node: {node}")
    print(f"  npm:  {npm}")
    print("\nTo install the driver:")
    print("  1. npm install -g agent-browser     # the automation CLI")
    print("  2. agent-browser install            # downloads its Chromium")
    print("\nIsolation caveat: enable the browser toolpack only on a chara running")
    print("under `dir` or `docker` isolation, with --no-sandbox (Chromium will not")
    print("start under the default sandbox-exec/bwrap jail).")

    if check:
        return 1
    print("\n(Re-run with the steps above, or pass nothing to see this guidance again.)")
    print("This command does not install automatically — run the two npm steps yourself,")
    print("then `lunamoth setup browser` confirms the driver is ready.")
    return 1


def _uv_path() -> str:
    return shutil.which("uv") or str(S.lunamoth_home() / "bin" / "uv")


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
        print("up to date" if behind == 0 else f"{behind} commit(s) behind — run `lunamoth update`")
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
    # Wheel install (the user channel): `uv tool upgrade` pulls the newest
    # version uv can resolve for this package. The wheel carries the built
    # frontend (front/webui/), so there's no node/build step here.
    uv = _uv_path()
    if not (Path(uv).exists() or shutil.which("uv")):
        print("error: uv not found; reinstall via install.sh", file=sys.stderr)
        return 1
    if args.check:
        print("wheel install — run `lunamoth update` to fetch the latest release")
        return 0
    print("updating lunamoth (wheel) ...")
    proc = subprocess.run([uv, "tool", "upgrade", "lunamoth"])
    if proc.returncode != 0:
        print(
            "error: `uv tool upgrade lunamoth` failed; reinstall the latest "
            "release via install.sh (private repo: set GITHUB_TOKEN)",
            file=sys.stderr,
        )
        return proc.returncode
    print("updated.")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    def line(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))

    print(f"lunamoth {__version__} @ {APP_DIR}")
    line("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    line("uv", bool(shutil.which("uv")), shutil.which("uv") or "missing (install.sh provides one)")
    _is_git = (APP_DIR / ".git").exists()
    line("install channel", True, "dev (git checkout)" if _is_git else "wheel (GitHub Release)")
    if sys.platform == "darwin":
        line("sandbox-exec (simple sandbox)", bool(shutil.which("sandbox-exec")))
    else:
        line("bubblewrap (simple sandbox)", bool(shutil.which("bwrap")), "install: apt/dnf install bubblewrap")
    line("docker (optional)", bool(shutil.which("docker")))
    # Optional browser_* tool driver (hidden until installed; `lunamoth setup browser`).
    try:
        from ..protocol.api import browser_driver_status
        _cli, _chromium = browser_driver_status()
        line("browser tools (optional)", bool(_cli) and _chromium,
             "ready" if (_cli and _chromium) else "run `lunamoth setup browser` to enable")
    except Exception:
        line("browser tools (optional)", False, "run `lunamoth setup browser`")
    print(f"  home: {S.lunamoth_home()}  sessions: {len(S.list_sessions())}")
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
    print(f"lunamoth {__version__}")
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
        S.lunamoth_home().mkdir(parents=True, exist_ok=True)
        (S.lunamoth_home() / _STAMP).write_text(json.dumps({"t": time.time(), "behind": behind}))
    except OSError:
        pass


def _maybe_update_hint() -> None:
    """At most once a day, mention available updates. Never blocks, never raises."""
    stamp = S.lunamoth_home() / _STAMP
    try:
        data = json.loads(stamp.read_text())
        if time.time() - data.get("t", 0) < 86400:
            if data.get("behind", 0) > 0:
                print(f"(update available: {data['behind']} commit(s) behind — `lunamoth update`)")
            return
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    behind = _commits_behind()
    if behind is None:
        return
    _write_update_stamp(behind)
    if behind > 0:
        print(f"(update available: {behind} commit(s) behind — `lunamoth update`)")


# ---- parser ----------------------------------------------------------------


def _add_tui_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--patience", "--cooldown", dest="patience", type=float, default=None,
                   help="override pause between the chara's spontaneous cycles, in seconds")
    p.add_argument("--mode", choices=["live", "chat"], default="",
                   help="override the chara's interaction mode for this attach (live: keeps creating while you watch; chat: only replies)")
    p.add_argument("--no-forever", action="store_true", help=argparse.SUPPRESS)  # pre-rename alias for --mode chat
    p.add_argument("--plain", action="store_true", help="legacy plain terminal instead of the TUI")
    p.add_argument("--clean-on-exit", action="store_true", help="wipe the session sandbox on shutdown (default: persist)")
    p.add_argument("--debug", action="store_true", help="DEBUG-level diagnostics in the chara's sandbox/logs/")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lunamoth", description="LunaMoth — agentic character tavern")
    p.add_argument("--version", action="version", version=f"lunamoth {__version__}")
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
    sp.add_argument("--daemon", action="store_true", help="start lunamothd in the background")
    sp.add_argument("--debug", action="store_true", help="DEBUG-level diagnostics")
    sp.set_defaults(func=cmd_desktop)

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
    args = build_parser().parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        _maybe_update_hint()
        return cmd_default(args)
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
