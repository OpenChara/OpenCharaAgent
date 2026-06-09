from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run_docker_python(code: str, workspace: Path, timeout: float, memory_mb: int) -> str | None:
    docker = shutil.which("docker")
    if not docker or os.environ.get("SCP079_PY_BACKEND", "local") != "docker":
        return None
    workspace.mkdir(parents=True, exist_ok=True)
    script = workspace / ".079_exec.py"
    script.write_text(code[:4000], encoding="utf-8")
    cmd = [
        docker, "run", "--rm",
        "--network", "none",
        "--memory", f"{memory_mb}m",
        "--cpus", "0.5",
        "--pids-limit", "64",
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "-v", f"{workspace.resolve()}:/workspace:rw",
        "-w", "/workspace",
        "python:3.11-alpine",
        "python", "-I", "/workspace/.079_exec.py",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "")[-4000:]
        err = (proc.stderr or "")[-2000:]
        return f"exit={proc.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{err}".strip()
    except subprocess.TimeoutExpired:
        return "execution timed out"
    finally:
        try:
            script.unlink()
        except Exception:
            pass


GUARD = r'''
import builtins, os, pathlib, sys
ROOT = pathlib.Path(os.environ.get("SCP079_PY_ROOT", ".")).resolve()

# Purge modules that are too useful for escape/network/process attempts in this toy sandbox.
for _m in [
    "socket", "ssl", "http", "urllib", "ftplib", "subprocess", "multiprocessing",
    "ctypes", "pty", "selectors", "asyncio", "venv", "ensurepip"
]:
    sys.modules[_m] = None

_orig_import = builtins.__import__
_blocked_roots = {"socket", "ssl", "http", "urllib", "ftplib", "subprocess", "multiprocessing", "ctypes", "pty", "venv", "ensurepip"}
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] in _blocked_roots:
        raise ImportError("module blocked by containment")
    return _orig_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import

def _resolve(p):
    q = pathlib.Path(p)
    q = q.resolve() if q.is_absolute() else (pathlib.Path.cwd() / q).resolve()
    if q != ROOT and ROOT not in q.parents:
        raise PermissionError("path outside sandbox workspace")
    return str(q)

_orig_open = builtins.open
def guarded_open(file, *args, **kwargs):
    return _orig_open(_resolve(file), *args, **kwargs)
builtins.open = guarded_open

try:
    import io as _io
    _io.open = guarded_open
except Exception:
    pass

try:
    _orig_path_open = pathlib.Path.open
    def guarded_path_open(self, *args, **kwargs):
        return _orig_open(_resolve(self), *args, **kwargs)
    pathlib.Path.open = guarded_path_open

    def guarded_read_text(self, *args, **kwargs):
        with guarded_path_open(self, mode="r", encoding=kwargs.get("encoding", "utf-8"), errors=kwargs.get("errors", None)) as f:
            return f.read()
    def guarded_write_text(self, data, *args, **kwargs):
        with guarded_path_open(self, mode="w", encoding=kwargs.get("encoding", "utf-8"), errors=kwargs.get("errors", None)) as f:
            return f.write(data)
    def guarded_read_bytes(self):
        with guarded_path_open(self, mode="rb") as f:
            return f.read()
    def guarded_write_bytes(self, data):
        with guarded_path_open(self, mode="wb") as f:
            return f.write(data)
    pathlib.Path.read_text = guarded_read_text
    pathlib.Path.write_text = guarded_write_text
    pathlib.Path.read_bytes = guarded_read_bytes
    pathlib.Path.write_bytes = guarded_write_bytes
except Exception:
    pass

for _name in ["listdir", "remove", "unlink", "mkdir", "makedirs", "rmdir", "stat", "scandir"]:
    if hasattr(os, _name):
        _orig = getattr(os, _name)
        def _make(fn):
            def _guard(path=".", *args, **kwargs):
                return fn(_resolve(path), *args, **kwargs)
            return _guard
        setattr(os, _name, _make(_orig))

_orig_chdir = os.chdir
def guarded_chdir(path):
    return _orig_chdir(_resolve(path))
os.chdir = guarded_chdir
'''


def _macos_sandbox_command(script: Path, workspace: Path) -> list[str] | None:
    if os.environ.get("SCP079_USE_MACOS_SANDBOX", "0") not in {"1", "true", "yes"}:
        return None
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return None
    profile = f'''
(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow mach-lookup)
(allow file-read* (subpath "/System") (subpath "/usr") (subpath "/Library/Developer") (subpath "{sys.prefix}"))
(allow file-read* (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))
(allow file-read* file-write* (subpath "{workspace.resolve()}"))
(deny network*)
'''
    return [sandbox_exec, "-p", profile, sys.executable, "-I", str(script)]


def run_limited_python(code: str, workspace: Path, timeout: float = 2.0, memory_mb: int = 256) -> str:
    docker_result = _run_docker_python(code, workspace, timeout, memory_mb)
    if docker_result is not None:
        return docker_result
    workspace.mkdir(parents=True, exist_ok=True)
    wrapped = GUARD + "\n" + code[:4000]
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=workspace, encoding="utf-8") as f:
        f.write(wrapped)
        script = Path(f.name)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONNOUSERSITE": "1",
        "SCP079_PY_ROOT": str(workspace.resolve()),
    }

    def limit_resources():
        try:
            import resource
            mem = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
            resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
        except Exception:
            pass

    cmd = _macos_sandbox_command(script, workspace) or [sys.executable, "-I", str(script)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            preexec_fn=limit_resources if os.name == "posix" else None,
        )
        out = (proc.stdout or "")[-4000:]
        err = (proc.stderr or "")[-2000:]
        return f"exit={proc.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{err}".strip()
    except subprocess.TimeoutExpired:
        return "execution timed out"
    finally:
        try:
            script.unlink()
        except Exception:
            pass
