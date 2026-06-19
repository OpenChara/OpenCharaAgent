"""PTY path of the `terminal` tool / runner (run_terminal_pty).

Mirrors tests/test_runner.py style. Exercises the real-pseudo-terminal run:
a tty probe sees a terminal under pty=true (but not under the pipe path), the
output is ANSI-stripped + head/tail truncated, and a hung command still has its
whole process group killed on timeout.

PTY semantics are POSIX (openpty); these run on macOS and Linux alike. The
`admin` isolation is used so the tests don't require bwrap/sandbox-exec to be
installed on the host.
"""
import subprocess
import time

from lunamoth.tools.runner import run_terminal, run_terminal_pty


def test_pty_detects_a_tty(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal_pty("test -t 1 && echo HASTTY || echo NOTTY", ws, isolation="admin", timeout=10)
    assert "HASTTY" in out
    assert "NOTTY" not in out
    assert "exit=0" in out


def test_pipe_path_has_no_tty(tmp_path):
    # Contrast: the default subprocess+pipe path is NOT a terminal.
    ws = tmp_path / "workspace"
    out = run_terminal("test -t 1 && echo HASTTY || echo NOTTY", ws, isolation="admin", timeout=10)
    assert "NOTTY" in out
    assert "HASTTY" not in out


def test_pty_runs_basic_command(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal_pty("echo hello && echo world", ws, isolation="admin", timeout=10)
    assert "hello" in out and "world" in out and "exit=0" in out


def test_pty_writes_into_workspace(tmp_path):
    ws = tmp_path / "workspace"
    run_terminal_pty("printf moth > art.txt", ws, isolation="admin", timeout=10)
    assert (ws / "art.txt").read_text() == "moth"


def test_pty_output_is_ansi_stripped(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal_pty(
        "printf '\\033[1;32mGREEN\\033[0m\\n\\033]0;title\\007BODY\\n'",
        ws, isolation="admin", timeout=10,
    )
    assert "GREEN" in out and "BODY" in out
    assert "\x1b" not in out and "title" not in out


def test_pty_output_is_truncated_head_and_tail(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal_pty(
        "python3 -c \"print('HEADMARK' + 'x'*30000 + 'TAILMARK')\"",
        ws, isolation="admin", timeout=15,
    )
    assert "HEADMARK" in out
    assert "TAILMARK" in out
    assert "chars omitted out of" in out


def test_pty_timeout(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal_pty("sleep 5", ws, isolation="admin", timeout=1)
    assert "timed out after 1s" in out


def test_pty_huge_timeout_is_clamped_with_a_note(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal_pty("echo ok", ws, isolation="admin", timeout=999999)
    assert "ok" in out and "exit=0" in out
    assert "timeout clamped to 600s (requested 999999s" in out


def test_pty_timeout_keeps_partial_output(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal_pty("echo 早期输出; sleep 60", ws, isolation="admin", timeout=1)
    assert "timed out after 1s" in out
    assert "早期输出" in out


def test_pty_timeout_kills_the_whole_group(tmp_path):
    # Same scar as the pipe path (hermes #17327): a backgrounded grandchild must
    # die with the group, and the call must return promptly (not wedge on the
    # held pty).
    ws = tmp_path / "workspace"
    marker = "47.2468"  # an unusual sleep duration we can pgrep for
    t0 = time.monotonic()
    out = run_terminal_pty(f"sleep {marker} & sleep 60", ws, isolation="admin", timeout=1)
    assert time.monotonic() - t0 < 6
    assert "timed out after 1s" in out
    deadline = time.monotonic() + 3
    alive = True
    while time.monotonic() < deadline:
        alive = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True).returncode == 0
        if not alive:
            break
        time.sleep(0.05)
    assert not alive


def test_pty_credentials_are_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    ws = tmp_path / "workspace"
    out = run_terminal_pty('echo "key=$OPENAI_API_KEY"', ws, isolation="admin", timeout=10)
    assert "sk-secret" not in out


def test_pty_exit_code_reported(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal_pty("false", ws, isolation="admin", timeout=10)
    assert "exit=1" in out


def test_terminal_tool_pty_path_uses_a_tty(tmp_path):
    # End-to-end through the tool entry point: pty=true reaches run_terminal_pty.
    from lunamoth.tools.builtin.terminal import terminal

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    class _State:
        def load(self):
            return {"isolation": "admin", "network_access": False, "writable_paths": []}

    class _Ctx:
        workspace = ws
        state = _State()

        def run_terminal(self, command, *, timeout, workdir=None, browser=False):  # pragma: no cover
            raise AssertionError("pty=true must not fall back to the pipe runner")

    out = terminal(
        {"command": "test -t 1 && echo HASTTY || echo NOTTY", "pty": True, "timeout": 10},
        _Ctx(),
    )
    assert "HASTTY" in out and "NOTTY" not in out


def test_terminal_tool_no_stale_pty_deferred_note(tmp_path):
    # The old "pty mode is not yet supported" advisory must be gone.
    from lunamoth.tools.builtin.terminal import terminal

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    class _State:
        def load(self):
            return {"isolation": "admin", "network_access": False, "writable_paths": []}

    class _Ctx:
        workspace = ws
        state = _State()

        def run_terminal(self, command, *, timeout, workdir=None, browser=False):
            return "exit=0"

    out = terminal({"command": "echo hi", "pty": True, "timeout": 10}, _Ctx())
    assert "not yet supported" not in out
    assert "without a pseudo-terminal" not in out
