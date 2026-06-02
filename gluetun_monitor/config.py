"""Runtime configuration, loaded from environment variables.

The env-var contract (names + defaults) is part of the v1.x compatibility surface
pinned by the characterization suite. New v2.0.0 knobs (dependent viability,
recreate) are additive and default to safe, on-by-default behavior per Tenet 8.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARN", "WARNING", "ERROR"}


def _env_int(
    name: str, default: int, errors: list[str],
    *, minimum: int | None = None, maximum: int | None = None,
) -> int:
    """Read an int env var, returning ``default`` when **unset**.

    An *unset* var is just the (sane) default. A var that is **set** to an
    unparseable value — *or* a parseable value outside ``[minimum, maximum]`` — is
    malformed config: record a fatal error so the CLI refuses to start, rather
    than guessing a substitute and risking acting on the larger system with bad
    parameters (e.g. TIMEOUT=0 = infinite wget, WGET_TRIES=0 = infinite retries).
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"Invalid {name}={raw!r}: not an integer")
        return default
    if minimum is not None and value < minimum:
        errors.append(f"Invalid {name}={value}: must be >= {minimum}")
        return default
    if maximum is not None and value > maximum:
        errors.append(f"Invalid {name}={value}: must be <= {maximum}")
        return default
    return value


def _env_float(
    name: str, default: float, errors: list[str],
    *, minimum: float | None = None, maximum: float | None = None,
) -> float:
    """Read a float env var; a set-but-unparseable or out-of-range value is fatal."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"Invalid {name}={raw!r}: not a number")
        return default
    if minimum is not None and value < minimum:
        errors.append(f"Invalid {name}={value}: must be >= {minimum}")
        return default
    if maximum is not None and value > maximum:
        errors.append(f"Invalid {name}={value}: must be <= {maximum}")
        return default
    return value


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
    timeout: int = 10  # per-request network timeout (wget --timeout, ping -W) everywhere
    wget_tries: int = 1  # attempts per wget probe (the shuffle+threshold handle noise)
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
    # (ADR-0004/0005). On by default (Tenet 8); set 0 to disable -> FAILED state.
    auto_recreate: bool = True
    # Seconds to wait for gluetun DNS to stabilize after a restart (ADR-0003).
    dns_wait_timeout: int = 30
    log_level: str = "INFO"
    # Rotate the log file at this size, keeping this many backups (so the watchdog
    # never fills its own disk). ~10 MB x 5 backups ≈ 60 MB cap. 0 = no rotation.
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    # Optional comma-separated test URLs, unioned with sites.conf (config-via-env
    # parity with the other knobs). None = not set. Unlike the file, this is fixed
    # at process start (no live reload).
    sites_env: str | None = None
    # Comma-separated container names to NEVER manage (denylist). Filters auto
    # discovery and subtracts from an explicit list; exclude wins on overlap
    # ("first, do no harm"). Empty = exclude nothing.
    exclude_containers: str = ""
    # Per-dependent L7 viability probe (DNS + connectivity from inside the
    # container) — ADR-0006. On by default; 0 = interface/strand check only (no
    # URL fetch). The interface check is never optional.
    dependent_viability: bool = True
    # How many sites each dependent samples per loop: 1 (default, one shuffled
    # site — optimal, since dependents share gluetun's netns so only their DNS
    # differs), N for N distinct shuffled sites, or -1 for all. >1 is largely
    # redundant; the dial is for completeness (and costs N execs/dependent/loop).
    dependent_viability_samples: int = 1
    # Optional per-dispatch jitter (ms) to de-sync the dependent exec burst.
    # Default 0 = off: the MAX_PARALLEL_CHECKS cap already bounds the burst, so
    # jitter is opt-in for anyone who wants to spread load further (ADR-0006).
    max_jitter_ms: int = 0
    # Observe-only: run all detection/probing (read-only) but take NO mutating
    # action — log "[DRY-RUN] would ..." instead of restarting/recreating. Lets a
    # v2 instance soak-test against a real stack alongside an active monitor
    # without two actors conflicting. Off by default.
    dry_run: bool = False
    # Persistent per-site stats sidecar (ADR-0008). Observability only; best-effort.
    stats_file: str = "/logs/site-stats.json"
    # Drop a site's stats if it hasn't been polled (e.g. removed from sites.conf)
    # for this many days; <=0 keeps them forever.
    stats_retention_days: int = 90
    # Flaky-site advisory: warn when one site dominates the gluetun restarts in a
    # recent window. window in seconds (default 24h).
    advisory_window: int = 86400
    advisory_min_restarts: int = 5
    advisory_dominance: float = 0.5

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
        # Bounds are validated (out-of-range -> fatal) so a parseable-but-nonsensical
        # value can't create a downstream bug (TIMEOUT=0 -> infinite wget, etc.).
        fail_threshold = _env_int("FAIL_THRESHOLD", 2, errors, minimum=1)
        # Samples is -1 (all) or >= 1; 0 is meaningless (probe nothing) and the
        # monitor would silently coerce it to 1, so reject it rather than guess.
        viability_samples = _env_int("DEPENDENT_VIABILITY_SAMPLES", 1, errors, minimum=-1)
        if viability_samples == 0:
            errors.append(
                "Invalid DEPENDENT_VIABILITY_SAMPLES=0: use -1 (all sites) or a positive count"
            )
        docker_host = os.environ.get("DOCKER_HOST") or None
        log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        if log_level not in _VALID_LOG_LEVELS:
            errors.append(
                f"Invalid LOG_LEVEL={log_level!r}: expected one of {sorted(_VALID_LOG_LEVELS)}"
            )
        return cls(
            config_file=os.environ.get("CONFIG_FILE", "/config/sites.conf"),
            log_file=os.environ.get("LOG_FILE", "/logs/gluetun-monitor.log"),
            check_interval=_env_int("CHECK_INTERVAL", 30, errors, minimum=1),
            timeout=_env_int("TIMEOUT", 10, errors, minimum=1),
            wget_tries=_env_int("WGET_TRIES", 1, errors, minimum=1),
            fail_threshold=fail_threshold,
            gluetun_container=os.environ.get("GLUETUN_CONTAINER", "gluetun"),
            healthy_wait_timeout=_env_int("HEALTHY_WAIT_TIMEOUT", 120, errors, minimum=1),
            dependent_containers=os.environ.get("DEPENDENT_CONTAINERS", "auto"),
            docker_host=docker_host,
            # DEPENDENT_CONTAINER_FAILURES defaults to FAIL_THRESHOLD (ADR-0006).
            dependent_container_failures=_env_int(
                "DEPENDENT_CONTAINER_FAILURES", fail_threshold, errors, minimum=1
            ),
            max_parallel_checks=_env_int("MAX_PARALLEL_CHECKS", 6, errors, minimum=1),
            auto_recreate=_env_bool("AUTO_RECREATE", True, errors),
            dns_wait_timeout=_env_int("DNS_WAIT_TIMEOUT", 30, errors, minimum=0),
            log_level=log_level,
            log_max_bytes=_env_int("LOG_MAX_BYTES", 10 * 1024 * 1024, errors, minimum=0),
            log_backup_count=_env_int("LOG_BACKUP_COUNT", 5, errors, minimum=0),
            sites_env=os.environ.get("SITES") or None,
            exclude_containers=os.environ.get("EXCLUDE_CONTAINERS", ""),
            dependent_viability=_env_bool("DEPENDENT_VIABILITY", True, errors),
            dependent_viability_samples=viability_samples,
            max_jitter_ms=_env_int("MAX_JITTER_MS", 0, errors, minimum=0),
            dry_run=_env_bool("DRY_RUN", False, errors),
            stats_file=os.environ.get("STATS_FILE", "/logs/site-stats.json"),
            # No lower bound: <= 0 intentionally means "keep stats forever".
            stats_retention_days=_env_int("STATS_RETENTION_DAYS", 90, errors),
            advisory_window=_env_int("ADVISORY_WINDOW", 86400, errors, minimum=1),
            advisory_min_restarts=_env_int("ADVISORY_MIN_RESTARTS", 5, errors, minimum=1),
            advisory_dominance=_env_float("ADVISORY_DOMINANCE", 0.5, errors,
                                          minimum=0.0, maximum=1.0),
            errors=tuple(errors),
        )
