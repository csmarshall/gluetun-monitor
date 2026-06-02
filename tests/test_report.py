"""The gluetun-monitor-stats report renders the stats sidecar (read-only).

Why: operators need the per-site matrix (latency percentiles, failure rate,
restart-effectiveness) without parsing JSON by hand. build_report is the single
source both the table and --json share, so the views can't drift; these tests pin
the data shape, the table, the sorts, the n/a rendering, and the safe-on-bad-input
behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

from gluetun_monitor.report import build_report, format_table, main
from gluetun_monitor.site_stats import SiteStatsStore


def _store_with_data() -> SiteStatsStore:
    s = SiteStatsStore(None)
    # fast.com: healthy, no restarts -> effectiveness n/a
    for ms in (100, 110, 90, 120, 105):
        s.record_poll("https://fast.com", True, duration_ms=ms)
    # slow.com: slower + one failure + a restart that cleared
    for ms in (900, 1100, 1000, 1200):
        s.record_poll("https://slow.com", True, duration_ms=ms)
    s.record_poll("https://slow.com", False, reason="timed out")
    s.record_restart("https://slow.com")
    s.record_restart_outcome("https://slow.com", cleared=True)
    s.record_loop()
    return s


def test_build_report_shape() -> None:
    rep = build_report(_store_with_data())
    assert set(rep) == {"monitor", "sites"}
    assert {r["site"] for r in rep["sites"]} == {"https://fast.com", "https://slow.com"}
    slow = next(r for r in rep["sites"] if r["site"] == "https://slow.com")
    assert slow["polls"] == 5 and slow["failures"] == 1
    assert slow["restart_effectiveness"] == 1.0
    assert slow["latency_ms"]["samples"] == 4
    fast = next(r for r in rep["sites"] if r["site"] == "https://fast.com")
    assert fast["restart_effectiveness"] is None  # no restarts


def test_build_report_is_json_serializable() -> None:
    rep = build_report(_store_with_data())
    round_tripped = json.loads(json.dumps(rep))
    assert round_tripped["sites"]


def test_table_has_headers_and_na() -> None:
    table = format_table(build_report(_store_with_data()))
    for header in ("site", "p90", "p99", "rate%", "eff%", "last_fail"):
        assert header in table
    assert "n/a" in table  # fast.com has no restarts
    assert "https://slow.com" in table


def test_table_sort_p90_puts_slowest_first() -> None:
    table = format_table(build_report(_store_with_data()), sort="p90")
    assert table.index("https://slow.com") < table.index("https://fast.com")


def test_table_sort_by_name() -> None:
    table = format_table(build_report(_store_with_data()), sort="name")
    assert table.index("https://fast.com") < table.index("https://slow.com")


def test_table_empty_store() -> None:
    table = format_table(build_report(SiteStatsStore(None)))
    assert "no sites recorded yet" in table


def test_main_json_output(tmp_path: Path, capsys) -> None:
    path = str(tmp_path / "stats.json")
    s = _store_with_data()
    s._path = Path(path)  # type: ignore[attr-defined]
    assert s.save() is True
    rc = main(["--file", path, "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sites"] and "monitor" in out


def test_main_table_output(tmp_path: Path, capsys) -> None:
    path = str(tmp_path / "stats.json")
    s = _store_with_data()
    s._path = Path(path)  # type: ignore[attr-defined]
    s.save()
    rc = main(["--file", path, "--sort", "avg"])
    assert rc == 0
    assert "https://slow.com" in capsys.readouterr().out


def test_main_missing_file_is_clean_error(tmp_path: Path, capsys) -> None:
    rc = main(["--file", str(tmp_path / "nope.json")])
    assert rc == 1
    assert "No stats file" in capsys.readouterr().err


def test_main_tolerates_corrupt_file(tmp_path: Path, capsys) -> None:
    """A garbage stats file yields an empty report, never a crash."""
    p = tmp_path / "stats.json"
    p.write_text("{ this is not json", encoding="utf-8")
    rc = main(["--file", str(p)])
    assert rc == 0
    assert "no sites recorded yet" in capsys.readouterr().out
