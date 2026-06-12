
import pytest

from lunamoth.tools.runner import os_sandbox_available, run_terminal, strip_ansi, truncate_middle


def test_dir_runs_any_command(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal("echo hello && echo world", ws, isolation="dir", timeout=10)
    assert "hello" in out and "world" in out and "exit=0" in out


def test_dir_writes_into_workspace(tmp_path):
    ws = tmp_path / "workspace"
    run_terminal("printf moth > art.txt", ws, isolation="dir", timeout=10)
    assert (ws / "art.txt").read_text() == "moth"


def test_timeout(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal("sleep 5", ws, isolation="dir", timeout=1)
    assert "timed out" in out


def test_timeout_with_pipe_holding_grandchild_returns_and_kills_group(tmp_path):
    # The audit-#14 scar (hermes #17327): a background child inherits the
    # stdout pipe; with subprocess.run(timeout=) only the leader dies and the
    # post-timeout communicate() blocks until the grandchild exits (minutes).
    # The killpg path must return promptly AND leave no survivor.
    import subprocess
    import time

    ws = tmp_path / "workspace"
    marker = "47.1359"  # an unusual sleep duration we can pgrep for
    t0 = time.monotonic()
    out = run_terminal(f"sleep {marker} & sleep 60", ws, isolation="dir", timeout=1)
    assert time.monotonic() - t0 < 6  # bounded — not wedged on the held pipe
    assert "timed out after 1s" in out
    # The whole GROUP died, not just the leader: the background sleep is gone.
    deadline = time.monotonic() + 3
    alive = True
    while time.monotonic() < deadline:
        alive = subprocess.run(["pgrep", "-f", f"sleep {marker}"], capture_output=True).returncode == 0
        if not alive:
            break
        time.sleep(0.05)
    assert not alive


def test_huge_timeout_is_clamped_with_a_note(tmp_path):
    # Audit #17: timeout=999999 must not be able to wedge an unattended cycle.
    ws = tmp_path / "workspace"
    out = run_terminal("echo ok", ws, isolation="dir", timeout=999999)
    assert "ok" in out and "exit=0" in out
    assert "timeout clamped to 600s (requested 999999s" in out


def test_tiny_timeout_is_clamped_up_with_a_note(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal("sleep 30", ws, isolation="dir", timeout=-5)
    assert "timed out after 1s" in out                    # clamped to the 1 s floor
    assert "timeout clamped to 1s (requested -5s" in out  # and the model is told


def test_in_range_timeout_gets_no_note(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal("echo ok", ws, isolation="dir", timeout=30)
    assert "clamped" not in out


def test_timeout_keeps_partial_output(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal("echo 早期输出; echo oops >&2; sleep 60", ws, isolation="dir", timeout=1)
    assert "timed out after 1s" in out
    assert "早期输出" in out  # what the command printed before the cut survives
    assert "oops" in out


def test_truncate_middle_keeps_head_and_tail_with_marker():
    # Audit #15: 40% head / 60% tail around an explicit marker, never a
    # silent last-N-chars cut.
    text = "H" * 5000 + "M" * 5000 + "T" * 5000
    out = truncate_middle(text, 1000)
    assert out.startswith("H" * 400)          # 40% head survives
    assert out.endswith("T" * 600)            # 60% tail survives
    assert "M" not in out                     # the middle is what was cut
    assert "14000 chars omitted out of 15000 total" in out
    # Total stays ~cap (cap + the marker line).
    assert len(out) < 1000 + 200


def test_truncate_middle_short_output_untouched():
    assert truncate_middle("short", 1000) == "short"


def test_long_output_truncated_head_and_tail(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal(
        "python3 -c \"print('HEADMARK' + 'x'*30000 + 'TAILMARK')\"",
        ws, isolation="dir", timeout=15,
    )
    assert "HEADMARK" in out                  # the head is no longer thrown away
    assert "TAILMARK" in out
    assert "chars omitted out of" in out      # explicit marker, no silent cut


@pytest.mark.parametrize("dirty,clean", [
    ("\x1b[31mred\x1b[0m text", "red text"),                       # CSI colors
    ("\x1b[?25l\x1b[2Khidden cursor", "hidden cursor"),            # CSI private mode
    ("\x1b]0;window title\x07body", "body"),                       # OSC, BEL terminator
    ("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\", "link"),          # OSC, ST terminator
    ("\x1bPq#0;2;0;0;0\x1b\\sixel gone", "sixel gone"),            # DCS string
    ("\x1b(Bcharset", "charset"),                                  # nF escape
    ("\x9b31mbright\x9b0m", "bright"),                             # 8-bit CSI
    ("\x9d0;title\x9cafter", "after"),                             # 8-bit OSC
    ("a\x85b\x90c", "abc"),                                        # bare 8-bit C1
])
def test_strip_ansi_full_ecma48(dirty, clean):
    assert strip_ansi(dirty) == clean


def test_strip_ansi_fast_path_returns_same_object():
    s = "plain text, no escapes — 中文 too"
    assert strip_ansi(s) is s   # fast path: untouched, not even copied
    assert strip_ansi("") == ""


def test_terminal_output_reaches_model_ansi_free(tmp_path):
    ws = tmp_path / "workspace"
    out = run_terminal("printf '\\033[1;32mGREEN\\033[0m\\n\\033]0;title\\007BODY\\n'", ws, isolation="dir", timeout=10)
    assert "GREEN" in out and "BODY" in out
    assert "\x1b" not in out and "title" not in out


def test_credentials_are_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    ws = tmp_path / "workspace"
    out = run_terminal('echo "key=$OPENAI_API_KEY"', ws, isolation="dir", timeout=10)
    assert "sk-secret" not in out


@pytest.mark.skipif(not os_sandbox_available(), reason="no OS sandbox (sandbox-exec/bwrap) on this host")
def test_sandbox_blocks_network_by_default(tmp_path):
    ws = tmp_path / "workspace"
    code = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('http://1.1.1.1', timeout=4); print('NETOK')\n"
        "except Exception: print('BLOCKED')\n"
    )
    out = run_terminal(f"python3 -c {_q(code)}", ws, isolation="sandbox", allow_network=False, timeout=20)
    assert "BLOCKED" in out and "NETOK" not in out


@pytest.mark.skipif(not os_sandbox_available(), reason="no OS sandbox on this host")
def test_sandbox_blocks_outside_write(tmp_path):
    ws = tmp_path / "workspace"
    target = tmp_path / "escape.txt"
    out = run_terminal(f"printf x > {target}", ws, isolation="sandbox", timeout=15)
    assert not target.exists()
    assert "exit=0" not in out  # the redirect should fail


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
