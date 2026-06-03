#!/bin/sh
# gluetun-monitor entrypoint.
#
# Drop-in by default: with no PUID/PGID set, the monitor runs as root — exactly
# like v1, with nothing to configure and no host-side chown.
#
# Recommended (LSIO-style, opt-in): set PUID/PGID and the container makes the
# /logs mount writable by that user and drops privileges to it. gosu accepts a
# numeric uid:gid with no matching /etc/passwd entry, so no user need exist.
set -e

if [ -n "${PUID}" ] || [ -n "${PGID}" ]; then
    PUID="${PUID:-1000}"
    PGID="${PGID:-1000}"
    # Persisting the log file + site-stats needs /logs writable by the runtime
    # user. Best-effort: if the chown can't happen (e.g. a read-only mount), the
    # monitor still runs and just logs to stdout (Tenet 7 — never block on it).
    chown -R "${PUID}:${PGID}" /logs 2>/dev/null || true
    exec gosu "${PUID}:${PGID}" "$@"
fi

exec "$@"
