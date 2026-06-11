"""Named sessions under the LunaMoth home directory.

Layout (Hermes-style home):

    ~/.lunamoth/
      app/                     # installed checkout (created by install.sh)
      bin/                     # managed tools (uv)
      sessions/<name>/
        session.json           # metadata: isolation, timestamps, pid
        config.json            # per-session Settings (welcome screen output)
        sandbox/               # files/, workspace/, logs/ for this session

A session is activated simply by exporting LUNAMOTH_CONFIG_DIR and
LUNAMOTH_SANDBOX before the runtime modules are imported; the CLI does this in
`lunamoth.cli`. This file must therefore stay import-light and never import
config/settings itself.

Remote access note: the baseline remote story is `ssh host lunamoth attach
<name>`. Anything fancier (gateway daemon, tunnels, web) should build on this
registry — treat session dirs + `session.json` as the stable interface.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_SESSION = "home"
ISOLATION_LEVELS = ("dir", "sandbox", "docker")  # dir < sandbox (OS-level) < docker


def lunamoth_home() -> Path:
    return Path(os.getenv("LUNAMOTH_HOME", Path.home() / ".lunamoth")).expanduser().resolve()


def sessions_dir() -> Path:
    return lunamoth_home() / "sessions"


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


@dataclass
class SessionMeta:
    name: str
    isolation: str = "sandbox"  # dir | sandbox | docker
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
        self.root.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in asdict(self).items()}
        self.meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def env(self) -> dict[str, str]:
        """Environment that points the runtime at this session."""
        return {
            "LUNAMOTH_CONFIG_DIR": str(self.root),
            "LUNAMOTH_SANDBOX": str(self.sandbox_dir),
            "LUNAMOTH_SESSION": self.name,
        }

    def running_pid(self) -> int | None:
        try:
            pid = int(self.pid_path.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

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

    def daemon_pid(self) -> int | None:
        """PID of the detached background loop for this agent, if running."""
        return self._alive(self.daemon_pid_path)

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
    return SessionMeta(**{k: v for k, v in data.items() if k in known})


def create_session(name: str, isolation: str = "sandbox", note: str = "") -> SessionMeta:
    if not valid_name(name):
        raise ValueError(f"invalid session name: {name!r} (use letters/digits/._-)")
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


def delete_session(name: str) -> None:
    import shutil

    meta = load_session(name)
    if meta is None:
        raise FileNotFoundError(f"no session named {name!r}")
    pid = meta.running_pid() or meta.daemon_pid()
    if pid:
        raise RuntimeError(f"session {name!r} is running (pid {pid}); stop it first")
    shutil.rmtree(meta.root)


def ensure_default_session() -> SessionMeta:
    meta = load_session(DEFAULT_SESSION)
    if meta is None:
        meta = create_session(DEFAULT_SESSION, isolation="sandbox", note="default home session")
    return meta
