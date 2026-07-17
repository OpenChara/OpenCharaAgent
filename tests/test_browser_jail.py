"""The browser-specific OS jail (session/isolation.py, browser=True).

A real Chromium needs more latitude than the deny-default shell profile, so the
browser path uses an inverted jail: permissive by default, but with writes
confined to the workspace (+ the temp dirs the browser scratches in) and the
secret home (~/.chara) unreadable. Validated end-to-end on macOS 2026-06-19
(agent-browser + system Chrome under sandbox-exec). These unit tests exercise the
argv/profile BUILDERS directly, so they run on any platform without a real jail.
"""
from __future__ import annotations

from chara.session import isolation


# ---- macOS Seatbelt profile -------------------------------------------------

def test_macos_browser_profile_confines_writes_and_hides_secret(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    prof = isolation._macos_profile(ws, True, [], browser=True)
    # Inverted: allow-by-default so Chromium gets iokit/posix-shm/mach latitude.
    assert "(allow default)" in prof
    # ...but the secret home is unreadable and the workspace re-allowed over it.
    assert f'(deny file-read* (subpath "{isolation._chara_home()}"))' in prof
    assert f'(allow file-read* (subpath "{ws}"))' in prof
    # ...and writes are confined: deny-all then re-allow workspace + the temp
    # dirs Chrome's user-data-dir / ProcessSingleton socket / agent-browser
    # socket land in.
    assert "(deny file-write*)" in prof
    assert f'(allow file-write* (subpath "{ws}"))' in prof
    assert isolation._darwin_user_temp() in prof
    assert "/private/tmp" in prof


def test_macos_browser_profile_allows_metadata_traversal_not_data(tmp_path):
    """The workspace sits UNDER the denied home, so opening an absolute file://
    path needs metadata (stat) traversal of the home ancestors — re-allowed — while
    file CONTENTS (read-data) stay denied. Order matters: the broad
    `deny file-read*` must come BEFORE the `allow file-read-metadata` (last match
    wins in Seatbelt), and read-data is NEVER re-allowed on the home."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    home = isolation._chara_home()
    prof = isolation._macos_profile(ws, True, [], browser=True)
    assert f'(allow file-read-metadata (subpath "{home}"))' in prof
    # deny-data precedes allow-metadata (the broad deny is not overridden for data).
    assert prof.index(f'(deny file-read* (subpath "{home}"))') < prof.index(
        f'(allow file-read-metadata (subpath "{home}"))')
    # read-DATA on the home is never re-allowed (only the workspace/assets under it).
    assert f'(allow file-read* (subpath "{home}"))' not in prof


def test_macos_browser_profile_net_off_denies_network(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert "(deny network*)" in isolation._macos_profile(ws, False, [], browser=True)
    assert "(deny network*)" not in isolation._macos_profile(ws, True, [], browser=True)


def test_macos_shell_profile_unchanged_deny_default(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    prof = isolation._macos_profile(ws, True, [], browser=False)
    assert "(deny default)" in prof
    assert "(allow default)" not in prof


# ---- Linux bwrap argv -------------------------------------------------------

def test_linux_browser_jail_preserves_daemon_and_hides_secret(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    argv = isolation._linux_jail_argv(["/bin/bash", "-c", "x"], ws, True, [], browser=True)
    # The browser daemon must outlive a single call → NO --die-with-parent.
    assert "--die-with-parent" not in argv
    # Whole root readable (node/chromium/agent-browser live all over the host).
    assert "--ro-bind" in argv
    # Secret home hidden by an empty tmpfs, workspace re-bound rw over it.
    assert "--tmpfs" in argv
    assert str(isolation._chara_home()) in argv
    assert str(ws) in argv


def test_linux_landlock_browser_grants_proc(tmp_path):
    # Chrome's renderer FATALs without full /proc (opendir /proc/self/fd) under
    # Landlock — the browser variant must grant rw /proc + /sys + /dev/shm.
    ws = tmp_path / "workspace"
    ws.mkdir()
    argv = isolation._linux_landlock_argv(["/bin/bash", "-c", "x"], ws, True, [], browser=True)
    s = " ".join(argv)
    assert "--rw /proc" in s
    assert "--rw /sys" in s
    assert "--rw /dev/shm" in s
    assert "--rw /tmp" in s
    # The non-browser landlock jail does NOT grant /proc.
    shell = isolation._linux_landlock_argv(["/bin/bash", "-c", "x"], ws, True, [], browser=False)
    assert "--rw /proc" not in " ".join(shell)


def test_linux_shell_jail_keeps_die_with_parent(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    argv = isolation._linux_jail_argv(["/bin/bash", "-c", "x"], ws, True, [], browser=False)
    assert "--die-with-parent" in argv
    # The shell jail does NOT bind the whole root (only system dirs).
    assert not (argv.count("--ro-bind") and "/" in argv[argv.index("--ro-bind") + 1:argv.index("--ro-bind") + 2])


def test_linux_jail_chdir_honors_workdir(tmp_path):
    """bwrap's --chdir targets the caller-validated workdir (terminal's
    `workdir=` param); without one it stays the workspace. The binds — the
    confinement — are identical either way."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    sub = ws / "sub"
    sub.mkdir()
    for browser in (False, True):
        default = isolation._linux_jail_argv(["/bin/bash", "-c", "x"], ws, True, [], browser=browser)
        assert default[default.index("--chdir") + 1] == str(ws)
        argv = isolation._linux_jail_argv(["/bin/bash", "-c", "x"], ws, True, [],
                                          browser=browser, workdir=str(sub))
        i = argv.index("--chdir")
        assert argv[i + 1] == str(sub)
        # Only the starting directory moved — every other arg is unchanged.
        assert argv[:i + 1] == default[:i + 1]
        assert argv[i + 2:] == default[i + 2:]


def test_build_jail_command_threads_workdir_to_bwrap(tmp_path, monkeypatch):
    """On the Linux/bwrap tier build_jail_command returns run_cwd=None (bwrap
    chdirs itself) — the validated workdir must ride --chdir instead of being
    silently dropped."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    sub = ws / "proj"
    sub.mkdir()
    monkeypatch.setattr(isolation, "os_sandbox_available", lambda: True)
    monkeypatch.setattr(isolation.sys, "platform", "linux")
    cmd, run_cwd, note = isolation.build_jail_command(
        "pwd", ws, "sandbox", allow_network=True, workdir=str(sub))
    assert cmd[0] == "bwrap"
    assert run_cwd is None
    assert cmd[cmd.index("--chdir") + 1] == str(sub)


# ---- behavioral: the deny-order actually holds under a real sandbox-exec -----

def test_macos_browser_profile_denies_secret_contents_behaviorally(tmp_path, monkeypatch):
    """Run the REAL browser profile under sandbox-exec and prove the deny-order
    is correct: the secret home's file CONTENTS are unreadable (cat fails) even
    though metadata traversal is allowed. A string assertion can't catch a
    last-match-wins regression; this can. macOS-only (needs sandbox-exec)."""
    import shutil
    import subprocess
    import sys

    import pytest

    if sys.platform != "darwin" or shutil.which("sandbox-exec") is None:
        pytest.skip("needs macOS sandbox-exec")

    # conftest points CHARA_HOME at a tmp dir; plant a fake secret there.
    home = isolation._chara_home()
    home.mkdir(parents=True, exist_ok=True)
    secret = home / "auth.json"
    secret.write_text("SECRET-SHOULD-NOT-LEAK", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir()

    cat = isolation._macos_jail(f"cat {secret}", ws, True, [], browser=True)
    r = subprocess.run(cat, capture_output=True, text=True, timeout=20)
    assert r.returncode != 0, "browser jail leaked the secret's CONTENTS"
    assert "SECRET-SHOULD-NOT-LEAK" not in r.stdout

    # A workspace file IS readable (the re-allow over the denied home works).
    (ws / "ok.txt").write_text("WORKSPACE-OK", encoding="utf-8")
    okc = isolation._macos_jail(f"cat {ws / 'ok.txt'}", ws, True, [], browser=True)
    r2 = subprocess.run(okc, capture_output=True, text=True, timeout=20)
    assert r2.returncode == 0 and "WORKSPACE-OK" in r2.stdout


# ---- the loose browser jail is NOT reachable from the model's shell tools ----

def test_terminal_tools_never_select_the_browser_jail():
    """The browser jail is looser (allow-default on macOS, host /proc on Linux).
    It must be reachable ONLY through the agent-browser driver, never the model's
    terminal/execute_code tools — otherwise a chara could get the loose jail for
    arbitrary shell. Assert those tool bodies call run_terminal withOUT browser=True
    (i.e. the default browser=False)."""
    import ast
    from pathlib import Path

    src = Path(isolation.__file__).resolve().parents[1] / "tools" / "builtin"
    for fname in ("terminal.py", "execute_code.py", "process.py", "search.py"):
        path = src / fname
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("run_terminal", "run_terminal_pty")):
                for kw in node.keywords:
                    assert kw.arg != "browser" or (
                        isinstance(kw.value, ast.Constant) and kw.value.value is False), (
                        f"{fname}: a shell tool passes browser= to run_terminal — "
                        "the loose browser jail must not be reachable from the model's shell")


# ---- security regressions: Seatbelt path escaping + env secret filtering -----

def test_macos_profile_escapes_quotes_in_writable_paths(tmp_path):
    """An operator /allow-dir path containing a double-quote (legal in a macOS
    filename) must NOT break out of the SBPL string literal to inject directives.
    Regression for the profile-injection vector."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    evil = tmp_path / 'a") (allow default) (deny nothing'  # would inject if unescaped
    evil.mkdir()
    prof = isolation._macos_profile(ws, True, [evil], browser=False)
    # the injected quote is backslash-escaped, so it can't terminate the SBPL
    # literal — the path stays ONE string atom, no directive is injected.
    assert '\\")' in prof  # the breakout quote rendered as \" inside the literal
    # the unescaped path never appears as a bare, closed literal (would be a breakout)
    assert f'(subpath "{evil}")' not in prof


def test_sbpl_rejects_newline_paths():
    import pytest
    with pytest.raises(isolation.JailUnavailableError):
        isolation._sbpl("/tmp/a\nb")


def test_base_env_strips_secret_named_vars(tmp_path, monkeypatch):
    """Provider keys + any secret-looking env name (our own OpenRouter/Ark/DashScope
    keys, AWS_*, *_TOKEN) are stripped from every jailed child; benign vars survive."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    monkeypatch.setenv("ARK_API_KEY", "ark-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("HF_TOKEN", "hf-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("LANG", "en_US.UTF-8")  # benign — must survive
    env = isolation._base_env(tmp_path)
    for leaked in ("OPENROUTER_API_KEY", "ARK_API_KEY", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN", "GITHUB_TOKEN"):
        assert leaked not in env, f"{leaked} leaked into the jailed child env"
    assert env.get("LANG") == "en_US.UTF-8"  # locale not stripped
