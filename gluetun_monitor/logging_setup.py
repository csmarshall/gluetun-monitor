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
from logging.handlers import RotatingFileHandler
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


_BASH_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"


class _BashFormatter(logging.Formatter):
    """Render ``[ts] [LEVEL] msg``, displaying WARNING as WARN (v1.x parity)."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a record, displaying WARNING as WARN for v1.x parity."""
        if record.levelname == "WARNING":
            record.levelname = "WARN"
        return super().format(record)


def install_bash_format_on_root(level: str = "WARNING") -> None:
    """Route stray third-party logs (docker-py / urllib3) through the bash format
    too, so the container's stderr is uniform — not a mix of our
    ``[ts] [LEVEL] msg`` and Python's default ``WARNING:urllib3...``. Our own
    Logger doesn't propagate, so this only affects library logging."""
    root = logging.getLogger()
    root.setLevel(_LEVEL_BY_NAME.get(level.upper(), logging.WARNING))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_BashFormatter(_BASH_FORMAT, _TIMESTAMP_FMT))
    root.addHandler(handler)


class Logger:
    """Thin wrapper over a stdlib logger, with the v1.x convenience methods."""

    def __init__(
        self,
        log_file: str | None = None,
        level: str = "INFO",
        *,
        stream: TextIO | None = None,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        # A private, non-propagating logger per instance keeps handlers isolated
        # (no duplicate lines, no interference with docker-py's own loggers).
        self._logger = logging.getLogger(f"gluetun_monitor.{next(_instance_counter)}")
        self._logger.setLevel(_LEVEL_BY_NAME.get(level.upper(), logging.INFO))
        self._logger.propagate = False
        self._logger.handlers.clear()

        formatter = _BashFormatter(_BASH_FORMAT, _TIMESTAMP_FMT)

        stream_handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        stream_handler.setFormatter(formatter)
        self._logger.addHandler(stream_handler)

        if log_file:
            try:
                Path(log_file).parent.mkdir(parents=True, exist_ok=True)
                file_handler: logging.Handler
                if max_bytes > 0:
                    # Size-capped rotation so the watchdog never fills its own disk
                    # (Tenet 7). ~max_bytes x backup_count total.
                    file_handler = RotatingFileHandler(
                        log_file, maxBytes=max_bytes, backupCount=backup_count,
                        encoding="utf-8",
                    )
                else:
                    file_handler = logging.FileHandler(log_file, encoding="utf-8")
                file_handler.setFormatter(formatter)
                self._logger.addHandler(file_handler)
            except OSError:
                # Unwritable path: degrade to stderr-only rather than crash.
                pass

    def log(self, level: str, message: str) -> None:
        """Emit ``message`` at the named level (string token, e.g. "CHECK")."""
        self._logger.log(_LEVEL_BY_NAME.get(level.upper(), logging.INFO), message)

    def debug(self, message: str) -> None:
        """Detail line (suppressed unless LOG_LEVEL=DEBUG)."""
        self._logger.debug(message)

    def info(self, message: str) -> None:
        """Normal operational message."""
        self._logger.info(message)

    def warn(self, message: str) -> None:
        """Non-fatal issue (renders as the v1.x ``WARN`` token)."""
        self._logger.warning(message)

    def error(self, message: str) -> None:
        """Failure / FAILED-state message."""
        self._logger.error(message)

    def check(self, message: str) -> None:
        """Per-loop ``CHECK`` marker (v1.x start/end-of-cycle token)."""
        self._logger.log(CHECK_LEVEL, message)

    def endpoint(self, message: str) -> None:
        """VPN ``ENDPOINT`` line (startup/failing/new)."""
        self._logger.log(ENDPOINT_LEVEL, message)
