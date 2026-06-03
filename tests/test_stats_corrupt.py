"""A corrupt stats sidecar degrades to "start fresh", never crashes startup.

Why (Tenet 1): the stats file is best-effort telemetry, not load-bearing state.
A malformed file — truncated, wrong top-level type, or a wrong-typed "sites"/
"monitor" value — must never take the monitor down. SiteStatsStore is built in
Monitor.__init__ before the loop, so a crash here would be a boot loop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gluetun_monitor.site_stats import SiteStatsStore

GARBAGE = [
    "not json at all {{{",          # invalid JSON -> ValueError
    "[1, 2, 3]",                    # top level is a list -> .get() AttributeError
    '"just a string"',             # top level is a string
    "12345",                        # top level is a number
    '{"sites": [1, 2, 3]}',        # sites is a list -> .items() AttributeError
    '{"sites": {"x": "notadict"}}',  # a site value is a string -> .get() AttributeError
    '{"sites": {"x": 5}}',          # a site value is a number
    '{"monitor": [1, 2]}',         # monitor is a list
    '{"recent_restarts": "nope"}',  # wrong type for recent_restarts
    "",                              # empty file
]


@pytest.mark.parametrize("payload", GARBAGE)
def test_corrupt_stats_file_starts_fresh(tmp_path: Path, payload: str) -> None:
    p = tmp_path / "site-stats.json"
    p.write_text(payload, encoding="utf-8")
    store = SiteStatsStore(str(p))  # must not raise
    assert store.sites == {}
    assert store.recent_restarts == []
    assert store.monitor.total_loops == 0


def test_valid_stats_file_still_loads(tmp_path: Path) -> None:
    """Sanity: the broadened except didn't swallow a genuinely good file."""
    p = tmp_path / "site-stats.json"
    p.write_text(json.dumps({
        "sites": {"https://a.example": {"first_seen": 1.0, "total_polls": 7,
                                        "total_failures": 2}},
        "recent_restarts": [{"ts": 1.0, "site": "https://a.example"}],
        "monitor": {"total_loops": 5},
    }), encoding="utf-8")
    store = SiteStatsStore(str(p))
    assert store.sites["https://a.example"].total_polls == 7
    assert store.recent_restarts == [{"ts": 1.0, "site": "https://a.example"}]
    assert store.monitor.total_loops == 5
