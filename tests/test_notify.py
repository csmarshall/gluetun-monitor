"""The opt-in notification layer (#22, ADR-0010).

The wrapper logic — min-level filter, per-key throttle, and the Tenet-7 swallow —
is tested with a fake Apprise object (no library calls, no network). The real
library is exercised separately in ``test_notify_apprise_real.py`` (the bar that
lets apprise auto-merge). The monitor wiring (which events fire) is asserted with
``FakeNotifier`` against ``FakeDockerClient``.
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
    level_value,
)


def _log() -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=io.StringIO())


class _FakeApprise:
    """Stand-in for apprise.Apprise — records sends; can fail on demand."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.raise_on_notify = False
        self.return_value = True

    def notify(self, *, title: str, body: str, notify_type: str) -> bool:
        if self.raise_on_notify:
            raise RuntimeError("boom")
        self.sent.append((title, body, notify_type))
        return self.return_value


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _notifier(fake: _FakeApprise, clock: _Clock, **kw: Any) -> AppriseNotifier:
    kw.setdefault("min_level", "WARN")
    kw.setdefault("throttle_seconds", 100)
    return AppriseNotifier(
        ("memory://",), logger=_log(), now=clock, apprise_factory=lambda _urls: fake, **kw
    )


def test_level_value_maps_names() -> None:
    assert level_value("INFO") < level_value("WARN") < level_value("ERROR")
    assert level_value("warning") == level_value("WARN")
    assert level_value("bogus") == level_value("WARN")  # unknown → WARN default


def test_null_notifier_is_noop() -> None:
    n = NullNotifier()
    n.notify(NotifyEvent("ERROR", "t", "b", "k"))  # must not raise
    assert n.test() is False


def test_min_level_filters_below_threshold() -> None:
    fake, clock = _FakeApprise(), _Clock()
    n = _notifier(fake, clock, min_level="WARN")
    n.notify(NotifyEvent("INFO", "t", "b", "k1"))  # below WARN → dropped
    assert fake.sent == []
    n.notify(NotifyEvent("ERROR", "t", "b", "k2"))  # at/above → sent
    assert len(fake.sent) == 1
    assert fake.sent[0][2] == "failure"  # ERROR → apprise "failure"


def test_throttle_dedups_same_key_until_window_passes() -> None:
    fake, clock = _FakeApprise(), _Clock()
    n = _notifier(fake, clock, throttle_seconds=100)
    n.notify(NotifyEvent("WARN", "t", "b", "same"))
    n.notify(NotifyEvent("WARN", "t", "b", "same"))  # within window → throttled
    assert len(fake.sent) == 1
    clock.advance(101)
    n.notify(NotifyEvent("WARN", "t", "b", "same"))  # window passed → sent again
    assert len(fake.sent) == 2


def test_throttle_is_per_key() -> None:
    fake, clock = _FakeApprise(), _Clock()
    n = _notifier(fake, clock, throttle_seconds=100)
    n.notify(NotifyEvent("WARN", "t", "b", "a"))
    n.notify(NotifyEvent("WARN", "t", "b", "b"))  # different key → not throttled
    assert len(fake.sent) == 2


def test_throttle_zero_disables_throttling() -> None:
    fake, clock = _FakeApprise(), _Clock()
    n = _notifier(fake, clock, throttle_seconds=0)
    for _ in range(3):
        n.notify(NotifyEvent("WARN", "t", "b", "same"))
    assert len(fake.sent) == 3


def test_send_exception_is_swallowed() -> None:
    fake, clock = _FakeApprise(), _Clock()
    fake.raise_on_notify = True
    n = _notifier(fake, clock)
    n.notify(NotifyEvent("ERROR", "t", "b", "k"))  # must not raise (Tenet 7)
    assert fake.sent == []


def test_failed_send_is_not_recorded_so_it_retries() -> None:
    fake, clock = _FakeApprise(), _Clock()
    fake.return_value = False  # apprise reports no success
    n = _notifier(fake, clock, throttle_seconds=100)
    n.notify(NotifyEvent("WARN", "t", "b", "k"))
    n.notify(NotifyEvent("WARN", "t", "b", "k"))  # not throttled (never recorded a success)
    assert len(fake.sent) == 2


def test_test_method_sends_and_reports() -> None:
    fake, clock = _FakeApprise(), _Clock()
    n = _notifier(fake, clock)
    assert n.test() is True
    assert fake.sent and fake.sent[0][2] == "info"


def test_build_notifier_null_without_urls() -> None:
    assert isinstance(build_notifier(Config(), _log()), NullNotifier)


def test_build_notifier_apprise_with_urls() -> None:
    cfg = Config(apprise_urls=("json://localhost",), notify_throttle=0)
    assert isinstance(build_notifier(cfg, _log()), AppriseNotifier)


def test_config_parses_notify_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("APPRISE_URLS", " ntfy://h/t , , discord://x ")
    monkeypatch.setenv("NOTIFY_MIN_LEVEL", "error")
    monkeypatch.setenv("NOTIFY_THROTTLE", "60")
    cfg = Config.from_env()
    assert cfg.apprise_urls == ("ntfy://h/t", "discord://x")  # trimmed, blanks dropped
    assert cfg.notify_min_level == "ERROR"
    assert cfg.notify_throttle == 60
    assert cfg.errors == ()


def test_config_rejects_bad_notify_min_level(monkeypatch: Any) -> None:
    monkeypatch.setenv("NOTIFY_MIN_LEVEL", "LOUD")
    cfg = Config.from_env()
    assert any("NOTIFY_MIN_LEVEL" in e for e in cfg.errors)
