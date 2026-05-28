"""Logger format parity + LOG_LEVEL gating + file output."""

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
    stream = io.StringIO()
    log = Logger(log_file=None, level="DEBUG", stream=stream)
    log.info("hello")
    assert _lines(stream) == [("INFO", "hello")]


def test_warning_renders_as_warn() -> None:
    stream = io.StringIO()
    Logger(log_file=None, level="DEBUG", stream=stream).warn("careful")
    assert _lines(stream) == [("WARN", "careful")]


def test_check_and_endpoint_tokens() -> None:
    stream = io.StringIO()
    log = Logger(log_file=None, level="INFO", stream=stream)
    log.check("Start")
    log.endpoint("Status: NEW")
    levels = [lvl for lvl, _ in _lines(stream)]
    assert levels == ["CHECK", "ENDPOINT"]


def test_debug_suppressed_at_info_level() -> None:
    stream = io.StringIO()
    log = Logger(log_file=None, level="INFO", stream=stream)
    log.debug("noisy")
    log.info("kept")
    assert _lines(stream) == [("INFO", "kept")]


def test_debug_shown_at_debug_level() -> None:
    stream = io.StringIO()
    log = Logger(log_file=None, level="DEBUG", stream=stream)
    log.debug("seen")
    assert _lines(stream) == [("DEBUG", "seen")]


def test_check_endpoint_survive_info_threshold() -> None:
    # The whole point of putting CHECK/ENDPOINT above INFO: they must not be
    # filtered out at the default level.
    stream = io.StringIO()
    log = Logger(log_file=None, level="INFO", stream=stream)
    log.check("Start")
    assert len(_lines(stream)) == 1


def test_writes_to_file(tmp_path: Path) -> None:
    log_file = tmp_path / "sub" / "monitor.log"  # parent created on demand
    log = Logger(log_file=str(log_file), level="INFO", stream=io.StringIO())
    log.info("persisted")
    assert log_file.exists()
    assert "[INFO] persisted" in log_file.read_text()


def test_unwritable_file_degrades_gracefully() -> None:
    # A path under a non-existent, uncreatable location must not raise.
    stream = io.StringIO()
    log = Logger(log_file="/proc/cannot/create/here.log", level="INFO", stream=stream)
    log.info("still works")
    assert "[INFO] still works" in stream.getvalue()
