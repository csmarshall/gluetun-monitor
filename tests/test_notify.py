"""The notification sink (#22, ADR-0010 + ADR-0011).

The tier filter, the per-loop rollup, and the Tenet-7 swallow are tested with a fake
Apprise object (no library, no network). Re-notification of ongoing problems lives in
``AlertState`` (see ``test_alert_state.py``). The real library is exercised in
``test_notify_apprise_real.py``; the monitor wiring in ``test_monitor.py``.
"""

from __future__ import annotations

import io
from typing import Any

from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.notify import (
    AppriseNotifier,
    NotifyEvent,
    NullNotifier,
    build_notifier,
    tier_rank,
)


def _log() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


class _FakeApprise:
    """Stand-in for apprise.Apprise — records sends; can fail on demand."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []  # (title, body, notify_type)
        self.raise_on_notify = False
        self.return_value = True

    def notify(self, *, title: str, body: str, notify_type: str) -> bool:
        if self.raise_on_notify:
            raise RuntimeError("boom")
        self.sent.append((title, body, notify_type))
        return self.return_value


def _notifier(fake: _FakeApprise, **kw: Any) -> AppriseNotifier:
    kw.setdefault("level", "attention")
    return AppriseNotifier(("memory://",), logger=_log(), apprise_factory=lambda _urls: fake, **kw)


def _ev(tier: str, key: str = "k") -> NotifyEvent:
    return NotifyEvent(tier=tier, title=f"{tier}-title", body=f"{tier}-body", key=key)


def test_tier_rank_orders_quiet_floor_to_firehose() -> None:
    assert tier_rank("attention") < tier_rank("recovery") < tier_rank("activity") < tier_rank("all")
    assert tier_rank("ATTENTION") == tier_rank("attention")
    assert tier_rank("bogus") == tier_rank("attention")  # unknown → loud floor


def test_null_notifier_is_noop() -> None:
    n = NullNotifier()
    n.notify(_ev("attention"))
    n.notify_batch([_ev("attention"), _ev("recovery")])
    assert n.test() is False


def test_tier_floor_filters_deeper_tiers() -> None:
    fake = _FakeApprise()
    n = _notifier(fake, level="attention")
    n.notify(_ev("recovery", "r"))  # deeper than attention → dropped
    assert fake.sent == []
    n.notify(_ev("attention", "a"))  # at the floor → sent
    assert len(fake.sent) == 1
    assert fake.sent[0][2] == "failure"  # attention → apprise "failure"


def test_higher_level_includes_lower_tiers() -> None:
    fake = _FakeApprise()
    n = _notifier(fake, level="activity")
    n.notify(_ev("recovery", "r"))  # within activity floor
    assert len(fake.sent) == 1
    assert fake.sent[0][2] == "success"  # recovery → apprise "success"


def test_rollup_groups_a_batch_into_one_send() -> None:
    fake = _FakeApprise()
    n = _notifier(fake, level="all")
    n.notify_batch([_ev("attention", "a"), _ev("recovery", "r"), _ev("activity", "c")])
    assert len(fake.sent) == 1  # one digest, not three
    title, body, ntype = fake.sent[0]
    assert "3 events" in title
    assert "attention-body" in body and "recovery-body" in body and "activity-body" in body
    assert ntype == "failure"  # worst tier present (attention) sets the color


def test_rollup_only_includes_surviving_tiers() -> None:
    fake = _FakeApprise()
    n = _notifier(fake, level="recovery")
    # attention + recovery survive; activity is below the floor and is filtered out.
    n.notify_batch([_ev("attention", "a"), _ev("recovery", "r"), _ev("activity", "c")])
    title, body, _ = fake.sent[0]
    assert "2 events" in title
    assert "activity-body" not in body


def test_single_survivor_sends_as_itself_not_a_digest() -> None:
    fake = _FakeApprise()
    n = _notifier(fake, level="recovery")
    n.notify_batch([_ev("recovery", "r"), _ev("activity", "c")])  # only recovery survives
    title, body, _ = fake.sent[0]
    assert title == "recovery-title"  # not the "N events" digest framing
    assert body == "recovery-body"


def test_empty_or_all_filtered_batch_sends_nothing() -> None:
    fake = _FakeApprise()
    n = _notifier(fake, level="attention")
    n.notify_batch([])  # nothing
    n.notify_batch([_ev("all", "x"), _ev("activity", "y")])  # all below floor
    assert fake.sent == []


def test_send_exception_is_swallowed() -> None:
    fake = _FakeApprise()
    fake.raise_on_notify = True
    n = _notifier(fake)
    n.notify(_ev("attention"))  # must not raise (Tenet 7)
    assert fake.sent == []


def test_test_method_sends_and_reports() -> None:
    fake = _FakeApprise()
    n = _notifier(fake)
    assert n.test() is True
    assert fake.sent and fake.sent[0][2] == "info"


def test_build_notifier_null_without_urls() -> None:
    assert isinstance(build_notifier(Config(), _log()), NullNotifier)


def test_build_notifier_apprise_with_urls() -> None:
    cfg = Config(apprise_urls=("json://localhost",))
    assert isinstance(build_notifier(cfg, _log()), AppriseNotifier)


def test_config_parses_notify_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("APPRISE_URLS", " ntfy://h/t , , discord://x ")
    monkeypatch.setenv("NOTIFY_LEVEL", "Recovery")
    monkeypatch.setenv("NOTIFY_REPEAT_INTERVAL", "20")
    cfg = Config.from_env()
    assert cfg.apprise_urls == ("ntfy://h/t", "discord://x")  # trimmed, blanks dropped
    assert cfg.notify_level == "recovery"
    assert cfg.notify_repeat_interval == 20
    assert cfg.errors == ()


def test_config_rejects_bad_notify_level(monkeypatch: Any) -> None:
    monkeypatch.setenv("NOTIFY_LEVEL", "loud")
    cfg = Config.from_env()
    assert any("NOTIFY_LEVEL" in e for e in cfg.errors)


def test_hung_backend_does_not_leak_a_thread_per_dispatch() -> None:
    """A backend that ignores its own timeout must not accumulate one abandoned
    daemon thread per loop (#85): at most one send is in flight at a time."""
    import threading

    release = threading.Event()

    class Hang:
        def notify(self, *, title: str, body: str, notify_type: str) -> bool:
            release.wait(5)  # never returns until the test releases it
            return True

    n = _notifier(Hang(), timeout_seconds=0)  # join(0) returns, worker stays alive
    before = threading.active_count()
    for _ in range(5):
        n.notify_batch([_ev("attention")])
    try:
        assert threading.active_count() - before <= 1  # one in-flight worker, not five
    finally:
        release.set()


def test_unknown_notify_level_warns() -> None:
    """#91: an unrecognized NOTIFY_LEVEL silently ranks 0 (loudest); warn once at
    construction so the operator isn't left guessing why everything sends."""
    stream = io.StringIO()
    logger = Logger(log_file=None, level="DEBUG", stream=stream)
    AppriseNotifier(
        ("memory://",), level="bogus", logger=logger,
        apprise_factory=lambda _urls: _FakeApprise(),
    )
    assert "not a recognized tier" in stream.getvalue()
