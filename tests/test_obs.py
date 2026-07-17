"""The obs/ diagnostics: log files, redaction, session tag, error split, ring."""
import logging

from chara.obs import broker, get_logger, setup_logging


def test_files_redaction_and_session_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("CHARA_SESSION", "testchara")
    setup_logging(debug=False, directory=tmp_path, force=True)
    log = get_logger("unittest")
    log.info("connect with key sk-or-abcdefghijklmnop and Bearer tok123secret")
    log.warning("retry 1/5: connection failed")
    log.debug("invisible at INFO level")

    main = (tmp_path / "chara.log").read_text(encoding="utf-8")
    errors = (tmp_path / "errors.log").read_text(encoding="utf-8")
    assert "[testchara]" in main and "chara.unittest" in main
    # Credentials never reach disk (hermes redaction rule).
    assert "sk-or-abcdefghijklmnop" not in main and "tok123secret" not in main
    assert "•••" in main
    assert "invisible" not in main
    # errors.log carries WARNING+ only.
    assert "retry 1/5" in errors and "connect with key" not in errors


def test_debug_level_toggle(tmp_path):
    setup_logging(debug=True, directory=tmp_path, force=True)
    get_logger("unittest").debug("debug detail visible")
    assert "debug detail visible" in (tmp_path / "chara.log").read_text(encoding="utf-8")


def test_ring_buffer_keeps_tail(tmp_path):
    setup_logging(debug=False, directory=tmp_path, force=True)
    log = get_logger("unittest")
    for i in range(600):
        log.info("ring line %d", i)
    tail = broker.tail(10)
    assert len(tail) == 10 and "ring line 599" in tail[-1]
    assert len(broker.ring) == broker.ring.maxlen  # bounded


def test_setup_is_idempotent(tmp_path):
    first = setup_logging(directory=tmp_path, force=True)
    second = setup_logging()  # no force: keeps existing handlers
    root = logging.getLogger("chara")
    assert first == tmp_path
    assert second  # returns a path without rebuilding
    assert len([h for h in root.handlers]) == 3  # main + errors + ring
