"""Named sessions under the OpenCharaAgent home directory.

Layout (Hermes-style home):

    ~/.chara/
      app/                     # installed checkout (created by install.sh)
      bin/                     # managed tools (uv)
      sessions/<name>/
        session.json           # metadata: isolation, timestamps, pid
        config.json            # per-session Settings (welcome screen output)
        sandbox/               # files/, workspace/, logs/ for this session

A session is activated simply by exporting CHARA_CONFIG_DIR and
CHARA_SANDBOX before the runtime modules are imported; the CLI does this in
`chara.cli`. This file must therefore stay import-light and never import
config/settings itself.

Remote access note: the baseline remote story is `ssh host chara attach
<name>`. Anything fancier (gateway daemon, tunnels, web) should build on this
registry — treat session dirs + `session.json` as the stable interface.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ISOLATION_LEVELS = ("sandbox", "admin")  # sandbox (OS jail) | admin (no jail, trusted operator)

# The ONE owner of the isolation→python-backend mapping (which jail a chara's
# tools run under). Previously hand-copied in supervisor.py, hub.py and cli.py —
# a safety-relevant fact that drifts if one copy is missed. Resolve it HERE and
# expose it through SessionMeta.env() so callers never re-derive it.
ISOLATION_TO_BACKEND = {"sandbox": "sandbox", "admin": "admin"}


def isolation_to_backend(isolation: str) -> str:
    # The distribution lock pins every chara to the sandbox jail at launch (the child's
    # CHARA_PY_BACKEND), regardless of its stored isolation. backend() clamps again at
    # runtime, but launching jailed from the start is the clean guarantee.
    from .isolation import force_sandbox
    if force_sandbox():
        return "sandbox"
    return ISOLATION_TO_BACKEND.get(isolation, "sandbox")

# Legacy isolation values mapped on read so old session configs keep working.
_LEGACY_ISOLATION = {"dir": "admin", "local": "admin", "docker": "admin"}


def normalize_isolation(value: str) -> str:
    """Map legacy isolation values to the current two-mode set (sandbox|admin)."""
    return _LEGACY_ISOLATION.get((value or "").strip().lower(), value)


def chara_home() -> Path:
    return Path(os.getenv("CHARA_HOME", Path.home() / ".chara")).expanduser().resolve()


def sessions_dir() -> Path:
    return chara_home() / "sessions"


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


# An EMPTY daemon.pid is an in-flight start claim (front/cli._start_daemon opens it
# O_EXCL before spawning, writes the pid after). A claim that outlives this TTL is a
# crashed starter — drop it so the chara can be started again.
_CLAIM_TTL = 60.0

# A pid RECORD younger than this is trusted without the identity check: a just-
# spawned child can still be mid fork→exec, when its command line briefly reads as
# the parent's. The reboot-reuse staleness the check exists for is by definition
# hours old, so the grace window costs nothing.
_IDENTITY_GRACE = 5.0


def pid_is_chara(pid: int, session: str | None = None) -> bool:
    """Does this pid actually belong to a chara process — and, when ``session``
    is given, to THAT chara's daemon?

    Liveness alone (``kill(pid, 0)``) is not identity: after a reboot the OS can hand
    a recorded pid to an UNRELATED process. Read the command line (/proc on Linux,
    ``ps`` on macOS) and require the lowercase package name in it — the daemon argv
    always carries it (``-m chara.front.terminal``, or the ``chara``
    entrypoint). Case-SENSITIVE on purpose: a stranger whose path merely contains
    e.g. ``.../OpenCharaAgent/...`` must not pass.

    The package name alone can't tell chara A's daemon from chara B's — every daemon
    argv is identically ``-m chara.front.terminal`` (the session rides only env),
    so reboot pid-reuse ACROSS siblings made start-all skip A and A.stop() kill B.
    cli._start_daemon therefore stamps an inert ``--session <name>`` marker into the
    argv (front/terminal.py accepts and ignores it). With ``session`` given, a
    cmdline carrying a DIFFERENT ``--session`` is a sibling's process — foreign. A
    chara cmdline with NO marker at all still passes: a pre-upgrade daemon
    (spawned before the marker existed) must not be treated as a stranger.
    False on any doubt."""
    if pid <= 0:
        return False
    try:
        proc = Path(f"/proc/{pid}/cmdline")
        if proc.exists():  # Linux
            cmdline = proc.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
        else:  # macOS (no /proc)
            p = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                               capture_output=True, text=True, timeout=5)
            if p.returncode != 0:
                return False
            cmdline = p.stdout
    except (OSError, subprocess.SubprocessError):
        return False
    if "chara" not in cmdline:
        return False
    if session is None:
        return True
    # Session names are token-safe (valid_name: no whitespace), so plain word
    # splitting recovers the marker from either the /proc or the `ps` rendering.
    tokens = cmdline.split()
    marker: str | None = None
    for i, tok in enumerate(tokens):
        if tok == "--session":
            if i + 1 < len(tokens):
                marker = tokens[i + 1]
            break
        if tok.startswith("--session="):
            marker = tok.split("=", 1)[1]
            break
    if marker is None:
        return True  # pre-marker daemon (old install) — chara, just unlabeled
    return marker == session


@dataclass
class SessionMeta:
    name: str
    isolation: str = "sandbox"  # sandbox | admin
    created_at: float = field(default_factory=time.time)
    last_active: float = 0.0
    note: str = ""

    @property
    def root(self) -> Path:
        return sessions_dir() / self.name

    @property
    def sandbox_dir(self) -> Path:
        return self.root / "sandbox"

    @property
    def meta_path(self) -> Path:
        return self.root / "session.json"

    @property
    def pid_path(self) -> Path:
        return self.root / "tui.pid"

    @property
    def daemon_pid_path(self) -> Path:
        return self.root / "daemon.pid"

    @property
    def daemon_log(self) -> Path:
        return self.root / "daemon.log"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    def is_configured(self) -> bool:
        """Has this agent been set up (provider/character chosen)?"""
        return self.config_path.exists()

    def character_label(self) -> str:
        """Display name of the agent's character, read cheaply from config.json."""
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "default"
        path = (data.get("character_path") or "").strip()
        return Path(path).stem if path else "default"

    def save(self) -> None:
        from ..config import atomic_write_text
        self.root.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items()}
        atomic_write_text(self.meta_path, json.dumps(data, ensure_ascii=False, indent=2))

    def env(self) -> dict[str, str]:
        """Environment that points the runtime at this session — the COMPLETE
        activation interface, including the python-backend (jail) derived from
        this session's isolation. Callers use this verbatim; they must not
        re-derive CHARA_PY_BACKEND themselves (that drift is a safety bug)."""
        return {
            "CHARA_CONFIG_DIR": str(self.root),
            "CHARA_SANDBOX": str(self.sandbox_dir),
            "CHARA_SESSION": self.name,
            "CHARA_PY_BACKEND": isolation_to_backend(self.isolation),
        }

    def set_isolation(self, isolation: str) -> str:
        """Switch this chara's OS isolation in BOTH stores so they can NEVER drift —
        the ONE writer every caller routes through. session.json (``isolation``) is the
        jail AUTHORITY: env() derives CHARA_PY_BACKEND from self.isolation, read back
        by the next child's load_session. config.json ``isolation`` is the UI/snapshot
        mirror (the same field name as the authority — there is no derived py_backend
        copy any more). Writing only one store left the post-wake sandbox toggle a no-op
        on the authority (the chara relaunched with the OLD jail). Takes effect on the
        NEXT process start — the backend is pinned at launch, never hot-swapped.

        The config mirror is best-effort: a missing/corrupt config.json is left untouched
        (never rewritten from scratch — that would wipe the chara's model/etc), since it
        is only a snapshot and session.json is the source of truth. Returns the normalized
        isolation; raises ValueError on an unknown value."""
        from ..config import atomic_write_text
        iso = normalize_isolation(isolation)
        if iso not in ISOLATION_LEVELS:
            raise ValueError(f"isolation must be one of {sorted(ISOLATION_LEVELS)}")
        self.isolation = iso
        self.save()  # session.json — the authority (meta.env())
        try:
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return iso  # no/corrupt mirror — leave it; never wipe an existing config
        if isinstance(cfg, dict):
            cfg["isolation"] = iso
            cfg.pop("py_backend", None)  # drop a legacy mirror copy — isolation is the one field
            atomic_write_text(self.config_path, json.dumps(cfg, ensure_ascii=False, indent=2), private=True)
        return iso

    def running_pid(self) -> int | None:
        try:
            pid = int(self.pid_path.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def running_marker_stale(self) -> bool:
        """True when tui.pid exists but does not point at a live process."""
        return self.pid_path.exists() and self.running_pid() is None

    def mark_running(self, pid: int | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.pid_path.write_text(str(pid or os.getpid()), encoding="utf-8")
        self.last_active = time.time()
        self.save()

    def clear_running(self) -> None:
        try:
            self.pid_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _alive(pid_path: Path) -> int | None:
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def daemon_claim_active(self) -> bool:
        """True while daemon.pid is an EMPTY in-flight start claim younger than
        ``_CLAIM_TTL`` (cli._start_daemon opens it O_EXCL BEFORE spawning and writes
        the real pid only after Popen). The ONE freshness rule — ``daemon_pid()``
        (leave the claim for the starter) and cli._stop_daemon (don't unlink a
        mid-spawn claim) both read it, so they can never disagree."""
        try:
            if self.daemon_pid_path.read_text().strip():
                return False  # a real pid record, not a claim
            return time.time() - self.daemon_pid_path.stat().st_mtime <= _CLAIM_TTL
        except OSError:
            return False  # no file (or unreadable) — no claim to protect

    def daemon_pid(self) -> int | None:
        """PID of the detached background loop for this agent, if running.

        Liveness alone is not enough: across a reboot the recorded pid can be REUSED
        by an unrelated process — ``start-all`` would silently skip the chara and
        ``stop`` would killpg a stranger. Verify the process identity
        (``pid_is_chara``, with THIS session's ``--session`` marker so a sibling
        chara's daemon is foreign too); any STALE pid file (dead pid, garbage
        content, or a foreign process) is removed here, so start may proceed and
        stop never signals it. An EMPTY file is an in-flight start claim (see
        cli._start_daemon) and is left alone until ``_CLAIM_TTL`` expires."""
        try:
            text = self.daemon_pid_path.read_text().strip()
        except OSError:
            return None
        if not text:
            if not self.daemon_claim_active():
                # a crashed starter's orphaned claim self-heals after the TTL
                self.daemon_pid_path.unlink(missing_ok=True)
            return None
        try:
            pid = int(text)
            os.kill(pid, 0)
        except (OSError, ValueError):
            self.daemon_pid_path.unlink(missing_ok=True)  # dead pid / garbage — stale
            return None
        if not pid_is_chara(pid, session=self.name):
            # Identity failure = reboot pid-reuse — EXCEPT a record written moments
            # ago, whose child may still be mid fork→exec (see _IDENTITY_GRACE).
            try:
                fresh = time.time() - self.daemon_pid_path.stat().st_mtime < _IDENTITY_GRACE
            except OSError:
                fresh = False
            if not fresh:
                self.daemon_pid_path.unlink(missing_ok=True)
                return None
        return pid

    def status(self) -> str:
        """attached (live TUI) | running (background daemon) | idle | new (unconfigured)."""
        if self._alive(self.pid_path):
            return "attached"
        if self.daemon_pid():
            return "running"
        return "idle" if self.is_configured() else "new"


def load_session(name: str) -> SessionMeta | None:
    path = sessions_dir() / name / "session.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    known = {f for f in SessionMeta.__dataclass_fields__}
    meta = SessionMeta(**{k: v for k, v in data.items() if k in known})
    meta.isolation = normalize_isolation(meta.isolation)  # legacy dir/local/docker → admin
    return meta


def create_session(name: str, isolation: str = "sandbox", note: str = "") -> SessionMeta:
    if not valid_name(name):
        raise ValueError(f"invalid session name: {name!r} (use letters/digits/._-)")
    isolation = normalize_isolation(isolation)  # accept legacy dir/local/docker → admin
    if isolation not in ISOLATION_LEVELS:
        raise ValueError(f"isolation must be one of {ISOLATION_LEVELS}")
    if load_session(name) is not None:
        raise FileExistsError(f"session {name!r} already exists")
    meta = SessionMeta(name=name, isolation=isolation, note=note)
    meta.sandbox_dir.mkdir(parents=True, exist_ok=True)
    meta.save()
    return meta


def list_sessions() -> list[SessionMeta]:
    base = sessions_dir()
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.iterdir()):
        if p.is_dir():
            meta = load_session(p.name)
            if meta is not None:
                out.append(meta)
    out.sort(key=lambda m: m.last_active or m.created_at, reverse=True)
    return out


def downgrade_admin_sessions() -> list[str]:
    """Distribution-lock startup step: persistently rewrite every ``admin`` chara to
    ``sandbox`` — in BOTH stores (session.json ``isolation`` = the jail authority via
    env(), and config.json ``isolation`` = the UI/snapshot mirror) — so
    the downgrade STICKS: after the lock is later removed the chara stays sandbox (the
    toggle just re-enables). Idempotent; returns the names that were downgraded.

    Called at startup when force_sandbox() is on. (backend() also clamps at runtime, so
    the jail is safe regardless; this is what makes the downgrade survive removing the
    lock instead of springing back to admin.)"""
    downgraded: list[str] = []
    for meta in list_sessions():
        try:
            cfg = json.loads(meta.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = None
        cfg_admin = isinstance(cfg, dict) and normalize_isolation(str(cfg.get("isolation") or "")) == "admin"
        if meta.isolation != "admin" and not cfg_admin:
            continue
        meta.set_isolation("sandbox")  # both stores, the ONE isolation writer
        downgraded.append(meta.name)
    return downgraded


def delete_session(name: str) -> None:
    import shutil

    meta = load_session(name)
    if meta is None:
        raise FileNotFoundError(f"no session named {name!r}")
    pid = meta.running_pid() or meta.daemon_pid()
    if pid:
        raise RuntimeError(f"session {name!r} is running (pid {pid}); stop it first")
    shutil.rmtree(meta.root)


def soft_delete_session(name: str) -> dict[str, str]:
    """SOFT delete (the UI 'delete chara' button): MOVE the session dir into
    ~/.chara/.trash/sessions/<id>/ instead of removing it. The chara leaves the board
    and its locked card leaves the deck (list_sessions no longer sees it) → it's back to
    'not awakened'; the deck TEMPLATE card it was woken from is untouched and re-wakeable.
    All data stays on disk, recoverable, and the name is freed so re-waking reuses it
    cleanly. Mirrors the card soft-delete (~/.trash/cards). Must be stopped first."""
    import shutil

    meta = load_session(name)
    if meta is None:
        raise FileNotFoundError(f"no session named {name!r}")
    pid = meta.running_pid() or meta.daemon_pid()
    if pid:
        raise RuntimeError(f"session {name!r} is running (pid {pid}); stop it first")
    trash = chara_home() / ".trash" / "sessions"
    trash.mkdir(parents=True, exist_ok=True)
    tid = os.urandom(6).hex()
    dest = trash / tid
    shutil.move(str(meta.root), str(dest))
    (dest / "origin.json").write_text(
        json.dumps({"name": name, "ts": int(time.time())}), encoding="utf-8",
    )
    return {"ok": True, "trash_id": tid}
