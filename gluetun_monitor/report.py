"""``gluetun-monitor-stats`` — render the persisted site-stats sidecar.

A read-only operator command that ships in the image: it loads the same
``site-stats.json`` the monitor writes (via the same SiteStatsStore code, so the
numbers always match what the monitor computes) and prints a per-site matrix
(latency percentiles, failure rate, restart-effectiveness) plus the monitor-wide
totals. ``--json`` emits the same data for jq/dashboards.

Usage::

    docker exec gluetun-monitor gluetun-monitor-stats
    docker exec gluetun-monitor gluetun-monitor-stats --sort avg
    docker exec gluetun-monitor gluetun-monitor-stats --json | jq .

It touches no Docker API and never mutates state — safe to run any time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .site_stats import SiteStatsStore

# Sort keys -> how to extract the sort value from a built site row (descending,
# except "name" which is ascending alphabetical).
_SORT_KEYS = ("p90", "p99", "avg", "max", "p50", "rate", "polls", "eff", "name")


def _fmt_ts(value: float | None) -> str:
    """A short local timestamp, or em dash when never seen."""
    if not value:
        return "—"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def build_report(store: SiteStatsStore) -> dict[str, Any]:
    """A plain-data snapshot of the store: monitor totals + one row per site.

    Pure and JSON-serializable — the table renderer and ``--json`` share it, so
    the two views can never drift.
    """
    m = store.monitor
    sites: list[dict[str, Any]] = []
    for url, st in store.sites.items():
        lat = st.latency_summary()
        sites.append({
            "site": url,
            "polls": st.total_polls,
            "failures": st.total_failures,
            "failure_rate": round(st.failure_rate, 4),
            "failure_episodes": st.failure_episodes,
            "longest_fail_streak": st.longest_fail_streak,
            "restarts_triggered": st.restarts_triggered,
            "restarts_cleared": st.restarts_cleared,
            "restart_effectiveness": st.restart_effectiveness,  # None = n/a
            "latency_ms": lat,  # recent ring ("now")
            "lifetime_latency_ms": st.lifetime_latency.summary(),  # all-time (DDSketch)
            "failure_reasons": dict(st.failure_reasons),
            "last_success": st.last_success,
            "last_failure": st.last_failure,
        })
    return {
        "monitor": {
            "version": m.version,
            "total_loops": m.total_loops,
            "total_runtime_seconds": round(m.total_runtime_seconds),
            "total_gluetun_restarts": m.total_gluetun_restarts,
            "total_dependent_remediations": m.total_dependent_remediations,
            "total_advisories": m.total_advisories,
            "first_started": m.first_started,
            "last_started": m.last_started,
        },
        "sites": sites,
    }


def _sort_value(row: dict[str, Any], key: str, lat_field: str) -> Any:
    if key == "name":
        return row["site"]
    if key == "rate":
        return row["failure_rate"]
    if key == "polls":
        return row["polls"]
    if key == "eff":
        # None (no restarts) sorts last under reverse=True via -1.0 sentinel.
        eff = row["restart_effectiveness"]
        return eff if eff is not None else -1.0
    return row[lat_field][key]  # p50/p90/p99/avg/min/max of the active window


def _sorted_sites(report: dict[str, Any], key: str, lat_field: str) -> list[dict[str, Any]]:
    rows = list(report["sites"])
    # "name" ascending; every numeric key descending (worst first — the tail you act on).
    rows.sort(key=lambda r: _sort_value(r, key, lat_field), reverse=(key != "name"))
    return rows


def format_table(report: dict[str, Any], sort: str = "p90", *, lifetime: bool = False) -> str:
    """Render the report as an aligned, human-readable matrix.

    ``lifetime=False`` shows the recent-ring latencies ("now"); ``lifetime=True``
    shows the all-time histogram percentiles instead.
    """
    lat_field = "lifetime_latency_ms" if lifetime else "latency_ms"
    m = report["monitor"]
    out: list[str] = []
    hrs = m["total_runtime_seconds"] / 3600
    out.append(
        f"monitor v{m['version'] or '?'}  loops={m['total_loops']}  runtime={hrs:.1f}h  "
        f"gluetun_restarts={m['total_gluetun_restarts']}  "
        f"remediations={m['total_dependent_remediations']}  advisories={m['total_advisories']}"
    )
    started = _fmt_ts(m["first_started"])
    if m["first_started"]:
        out.append(f"tracking since {started}")
    out.append(f"latency window: {'all-time' if lifetime else 'recent'}")
    out.append("")

    rows = _sorted_sites(report, sort, lat_field)
    if not rows:
        out.append("(no sites recorded yet)")
        return "\n".join(out)

    headers = ["site", "polls", "fails", "rate%", "avg", "p50", "p90", "p99",
               "max", "eff%", "last_fail"]
    table: list[list[str]] = [headers]
    for r in rows:
        lat = r[lat_field]
        eff = r["restart_effectiveness"]
        table.append([
            r["site"],
            str(r["polls"]),
            str(r["failures"]),
            f"{r['failure_rate'] * 100:.2f}",
            str(lat["avg"]),
            str(lat["p50"]),
            str(lat["p90"]),
            str(lat["p99"]),
            str(lat["max"]),
            "n/a" if eff is None else f"{eff * 100:.0f}",
            _fmt_ts(r["last_failure"]),
        ])

    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    for i, row in enumerate(table):
        # site left-justified; numeric columns right-justified.
        cells = [row[0].ljust(widths[0])] + [
            row[j].rjust(widths[j]) for j in range(1, len(headers))
        ]
        out.append("  ".join(cells))
        if i == 0:
            out.append("  ".join("-" * widths[j] for j in range(len(headers))))
    out.append("")
    out.append("latency in ms; eff% = restart-effectiveness (n/a = no restarts triggered)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for ``gluetun-monitor-stats``; returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="gluetun-monitor-stats",
        description="Render the gluetun-monitor site-stats sidecar (read-only).",
    )
    parser.add_argument(
        "--file",
        default=os.environ.get("STATS_FILE", "/logs/site-stats.json"),
        help="path to site-stats.json (default: $STATS_FILE or /logs/site-stats.json)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument(
        "--lifetime", action="store_true",
        help="show all-time latency percentiles (histogram) instead of the recent window",
    )
    parser.add_argument(
        "--sort", choices=_SORT_KEYS, default="p90",
        help="sort sites by this column (default: p90, worst tail first)",
    )
    args = parser.parse_args(argv)

    if not Path(args.file).exists():
        print(
            f"No stats file at {args.file} — the monitor writes it once it has run a "
            f"loop (set STATS_FILE or --file if it lives elsewhere).",
            file=sys.stderr,
        )
        return 1

    # Read-only load (no clock writes, no save). SiteStatsStore tolerates a corrupt
    # file by starting empty, so a bad file prints an empty report, never crashes.
    store = SiteStatsStore(args.file)
    report = build_report(store)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_table(report, sort=args.sort, lifetime=args.lifetime))
    return 0


if __name__ == "__main__":  # `python -m gluetun_monitor.report`
    sys.exit(main())
