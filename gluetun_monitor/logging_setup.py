"""Logging via stdlib ``logging``, formatted to match the v1.x bash output.

Format: ``[YYYY-MM-DD HH:MM:SS] [LEVEL] message`` to stderr and (best-effort) the
log file. The v1.x ``CHECK`` and ``ENDPOINT`` markers are registered as custom
levels just above INFO so they survive the default LOG_LEVEL=INFO threshold;
``WARNING`` renders as ``WARN`` to match v1.x. DEBUG is gated by LOG_LEVEL (new
in v2 — bash always emitted it).
"""

from __future__ import annotations

import itertools
import logging
import sys
from pathlib import Path
from typing import TextIO

# Custom levels between INFO (20) and WARNING (30) so they're never suppressed
# at the default INFO threshold but read as informational.
CHECK_LEVEL = 21
ENDPOINT_LEVEL = 22
logging.addLevelName(CHECK_LEVEL, "CHECK")
logging.addLevelName(ENDPOINT_LEVEL, "ENDPOINT")

_LEVEL_BY_NAME: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "CHECK": CHECK_LEVEL,
    "ENDPOINT": ENDPOINT_LEVEL,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"
_instance_counter = itertools.count()


class _BashFormatter(logging.Formatter):
    """Render ``[ts] [LEVEL] msg``, displaying WARNING as WARN (v1.x parity)."""

    def format(self, record: logging.LogRecord) -> str:
        if record.levelname == "WARNING":
            record.levelname = "WARN"
        return super().format(record)


class Logger:
    """Thin wrapper over a stdlib logger, with the v1.x convenience methods."""

    def __init__(
        self,
        log_file: str | None = None,
        level: str = "INFO",
        *,
        stream: TextIO | None = None,
    ) -> None:
        # A private, non-propagating logger per instance keeps handlers isolated
        # (no duplicate lines, no interference with docker-py's own loggers).
        self._logger = logging.getLogger(f"gluetun_monitor.{next(_instance_counter)}")
        self._logger.setLevel(_LEVEL_BY_NAME.get(level.upper(), logging.INFO))
        self._logger.propagate = False
        self._logger.handlers.clear()

        formatter = _BashFormatter("[%(asctime)s] [%(levelname)s] %(message)s", _TIMESTAMP_FMT)

        stream_handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        stream_handler.setFormatter(formatter)
        self._logger.addHandler(stream_handler)

        if log_file:
            try:
                Path(log_file).parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(log_file, encoding="utf-8")
                file_handler.setFormatter(formatter)
                self._logger.addHandler(file_handler)
            except OSError:
                # Unwritable path: degrade to stderr-only rather than crash.
                pass

    def log(self, level: str, message: str) -> None:
        self._logger.log(_LEVEL_BY_NAME.get(level.upper(), logging.INFO), message)

    def debug(self, message: str) -> None:
        self._logger.debug(message)

    def info(self, message: str) -> None:
        self._logger.info(message)

    def warn(self, message: str) -> None:
        self._logger.warning(message)

    def error(self, message: str) -> None:
        self._logger.error(message)

    def check(self, message: str) -> None:
        self._logger.log(CHECK_LEVEL, message)

    def endpoint(self, message: str) -> None:
        self._logger.log(ENDPOINT_LEVEL, message)
