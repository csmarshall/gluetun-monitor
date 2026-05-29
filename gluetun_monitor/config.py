"""Runtime configuration, loaded from environment variables.

The env-var contract (names + defaults) is part of the v1.x compatibility surface
pinned by the characterization suite. New v2.0.0 knobs (dependent viability,
recreate) are additive and default to safe, on-by-default behavior per Tenet 7.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARN", "WARNING", "ERROR"}


def _env_int(name: str, default: int, errors: list[str]) -> int:
    """Read an int env var, returning ``default`` when **unset**.

    An *unset* var is just the (sane) default. A var that is **set** to an
    unparseable value is malformed config: record a fatal error so the CLI
    refuses to start, rather than guessing a substitute and risking acting on the
    larger system with bad parameters. The return value is irrelevant then.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        errors.append(f"Invalid {name}={raw!r}: not an integer")
        return default


def _env_bool(name: str, default: bool, errors: list[str]) -> bool:
    """Read a boolean env var (1/0, true/false, yes/no, on/off).

    Unset → ``default``. Set to an unrecognized value → fatal error (never
    silently coerced to False — see _env_int).
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    errors.append(f"Invalid {name}={raw!r}: not a boolean (use 1/0, true/false, yes/no, on/off)")
    return default


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration."""

    # --- v1.x contract (defaults must not drift) ---
    config_file: str = "/config/sites.conf"
    log_file: str = "/logs/gluetun-monitor.log"
    check_interval: int = 30
    timeout: int = 10
    fail_threshold: int = 2
    gluetun_container: str = "gluetun"
    healthy_wait_timeout: int = 120
    dependent_containers: str = "auto"
    docker_host: str | None = None

    # --- v2.0.0 additions (dependent-aware; ADR-0004/0005/0006) ---
    # Consecutive per-dependent viability failures before remediation. Mirrors
    # FAIL_THRESHOLD so the stack has one mental model (ADR-0006, step 5).
    dependent_container_failures: int = 2
    # Bound the per-loop docker exec fan-out across live dependents (ADR-0006 Load).
    max_parallel_checks: int = 6
    # Recreate a dependent whose netns target moved to a new gluetun id
    # (ADR-0004/0005). On by default (Tenet 7); set 0 to disable -> FAILED state.
    auto_recreate: bool = True
    # Seconds to wait for gluetun DNS to stabilize after a restart (ADR-0003).
    dns_wait_timeout: int = 30
    log_level: str = "INFO"
    # Optional comma-separated test URLs, unioned with sites.conf (config-via-env
    # parity with the other knobs). None = not set. Unlike the file, this is fixed
    # at process start (no live reload).
    sites_env: str | None = None
    # Comma-separated container names to NEVER manage (denylist). Filters auto
    # discovery and subtracts from an explicit list; exclude wins on overlap
    # ("first, do no harm"). Empty = exclude nothing.
    exclude_containers: str = ""

    # Fatal config errors (malformed env values), collected during from_env and
    # surfaced by the CLI once the logger exists — the CLI then refuses to start.
    # Not an env var.
    errors: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Config:
        """Build a Config from the process environment, applying v1.x defaults.

        Malformed values are collected in ``errors`` (the CLI treats any as
        fatal) rather than guessed around — a watchdog acting on the larger
        system must not run with parameters it couldn't parse.
        """
        errors: list[str] = []
        fail_threshold = _env_int("FAIL_THRESHOLD", 2, errors)
        docker_host = os.environ.get("DOCKER_HOST") or None
        log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        if log_level not in _VALID_LOG_LEVELS:
            errors.append(
                f"Invalid LOG_LEVEL={log_level!r}: expected one of {sorted(_VALID_LOG_LEVELS)}"
            )
        return cls(
            config_file=os.environ.get("CONFIG_FILE", "/config/sites.conf"),
            log_file=os.environ.get("LOG_FILE", "/logs/gluetun-monitor.log"),
            check_interval=_env_int("CHECK_INTERVAL", 30, errors),
            timeout=_env_int("TIMEOUT", 10, errors),
            fail_threshold=fail_threshold,
            gluetun_container=os.environ.get("GLUETUN_CONTAINER", "gluetun"),
            healthy_wait_timeout=_env_int("HEALTHY_WAIT_TIMEOUT", 120, errors),
            dependent_containers=os.environ.get("DEPENDENT_CONTAINERS", "auto"),
            docker_host=docker_host,
            # DEPENDENT_CONTAINER_FAILURES defaults to FAIL_THRESHOLD (ADR-0006).
            dependent_container_failures=_env_int(
                "DEPENDENT_CONTAINER_FAILURES", fail_threshold, errors
            ),
            max_parallel_checks=_env_int("MAX_PARALLEL_CHECKS", 6, errors),
            auto_recreate=_env_bool("AUTO_RECREATE", True, errors),
            dns_wait_timeout=_env_int("DNS_WAIT_TIMEOUT", 30, errors),
            log_level=log_level,
            sites_env=os.environ.get("SITES") or None,
            exclude_containers=os.environ.get("EXCLUDE_CONTAINERS", ""),
            errors=tuple(errors),
        )
