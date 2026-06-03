"""Logger format parity + LOG_LEVEL gating + file output.

Why: the log format is a compatibility surface — users grep for `[CHECK]`,
`[ENDPOINT]`, `[WARN]` and the `[ts] [LEVEL] msg` shape (preserved from v1.x).
These tests pin that the stdlib-logging backend reproduces that exactly, that
the custom CHECK/ENDPOINT levels survive the default INFO threshold, and that an
unwritable log path degrades to stderr rather than crashing the monitor.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from gluetun_monitor.logging_setup import Logger

_LINE_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[([A-Z]+)\] (.*)$")


def _lines(stream: io.StringIO) -> list[tuple[str, str]]:
    out = []
    for raw in stream.getvalue().splitlines():
        m = _LINE_RE.match(raw)
        assert m, f"line does not match v1.x format: {raw!r}"
        out.append((m.group(1), m.group(2)))
    return out


def test_format_matches_v1() -> None:
    """An INFO line matches the exact v1.x `[ts] [LEVEL] msg` format."""
    stream = io.StringIO()
    log = Logger(log_file=None, level="DEBUG", stream=stream)
    log.info("hello")
    assert _lines(stream) == [("INFO", "hello")]


def test_warning_renders_as_warn() -> None:
    """stdlib's WARNING renders as the v1.x token `WARN`, not `WARNING`."""
    stream = io.StringIO()
    Logger(log_file=None, level="DEBUG", stream=stream).warn("careful")
    assert _lines(stream) == [("WARN", "careful")]


def test_check_and_endpoint_tokens() -> None:
    """The v1.x CHECK/ENDPOINT markers exist as custom levels with those exact
    token names (users grep for them)."""
    stream = io.StringIO()
    log = Logger(log_file=None, level="INFO", stream=stream)
    log.check("Start")
    log.endpoint("Status: NEW")
    levels = [lvl for lvl, _ in _lines(stream)]
    assert levels == ["CHECK", "ENDPOINT"]


def test_debug_suppressed_at_info_level() -> None:
    """DEBUG is gated by LOG_LEVEL — silent at the default INFO (new in v2)."""
    stream = io.StringIO()
    log = Logger(log_file=None, level="INFO", stream=stream)
    log.debug("noisy")
    log.info("kept")
    assert _lines(stream) == [("INFO", "kept")]


def test_debug_shown_at_debug_level() -> None:
    """...but DEBUG appears when LOG_LEVEL=DEBUG (the per-site/per-dependent detail)."""
    stream = io.StringIO()
    log = Logger(log_file=None, level="DEBUG", stream=stream)
    log.debug("seen")
    assert _lines(stream) == [("DEBUG", "seen")]


def test_check_endpoint_survive_info_threshold() -> None:
    """CHECK/ENDPOINT are placed just above INFO precisely so they are NOT
    filtered out at the default level — losing them would hide loop markers."""
    stream = io.StringIO()
    log = Logger(log_file=None, level="INFO", stream=stream)
    log.check("Start")
    assert len(_lines(stream)) == 1


def test_writes_to_file(tmp_path: Path) -> None:
    """Logs are written to the file (creating the parent dir on demand)."""
    log_file = tmp_path / "sub" / "monitor.log"  # parent created on demand
    log = Logger(log_file=str(log_file), level="INFO", stream=io.StringIO())
    log.info("persisted")
    assert log_file.exists()
    assert "[INFO] persisted" in log_file.read_text()


def test_unwritable_file_degrades_gracefully() -> None:
    """An unwritable log path must not crash the monitor — it degrades to
    stderr-only (the watchdog must never become the outage)."""
    stream = io.StringIO()
    log = Logger(log_file="/proc/cannot/create/here.log", level="INFO", stream=stream)
    log.info("still works")
    assert "[INFO] still works" in stream.getvalue()


def test_log_file_rotates_at_max_bytes(tmp_path: Path) -> None:
    """The file handler rotates so the watchdog can't fill its own disk: with a
    tiny max_bytes, writing past it creates a .1 backup and the active file stays
    bounded."""
    log_file = tmp_path / "monitor.log"
    log = Logger(log_file=str(log_file), level="INFO", stream=io.StringIO(),
                 max_bytes=1024, backup_count=2)
    for i in range(200):
        log.info(f"line {i} " + "x" * 60)  # ~80 bytes each -> well past 1KB
    assert (tmp_path / "monitor.log.1").exists()  # rotated
    assert log_file.stat().st_size <= 4096  # active file is bounded, not unbounded
    # backup_count respected: never more than .1 and .2
    assert not (tmp_path / "monitor.log.3").exists()


def test_log_rotation_disabled_with_zero_max_bytes(tmp_path: Path) -> None:
    """max_bytes=0 disables rotation (plain FileHandler) — opt-out for users who
    manage rotation externally (logrotate, etc.)."""
    log_file = tmp_path / "monitor.log"
    log = Logger(log_file=str(log_file), level="INFO", stream=io.StringIO(),
                 max_bytes=0, backup_count=5)
    for i in range(50):
        log.info(f"line {i}")
    assert log_file.exists()
    assert not (tmp_path / "monitor.log.1").exists()  # no rotation


def test_install_bash_format_on_root_formats_third_party() -> None:
    """Library logs (via the root logger) render in the bash format, not Python's
    default 'WARNING:name:msg' — so the container's output stays uniform."""
    import logging as _logging

    from gluetun_monitor.logging_setup import install_bash_format_on_root

    install_bash_format_on_root("WARNING")
    root = _logging.getLogger()
    assert len(root.handlers) == 1
    fmt = root.handlers[0].formatter
    assert fmt is not None
    rec = _logging.LogRecord("urllib3", _logging.WARNING, "x", 1, "pool full", None, None)
    line = fmt.format(rec)
    assert _LINE_RE.match(line), line   # [ts] [WARN] pool full
    assert "[WARN]" in line
