"""cli.main([]) wiring, signal handling, and the Monitor.run() loop."""

from __future__ import annotations

import io
import random
import signal
from pathlib import Path

import pytest

from gluetun_monitor import cli
from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger
from gluetun_monitor.monitor import Monitor

from .fakes import FakeDockerClient

GLUETUN_ID = "a" * 64


class _StopLoop(Exception):
    """Sentinel used to break the otherwise-infinite run() loop in tests."""


def _logger(stream: io.StringIO) -> Logger:
    return Logger(log_file=None, level="DEBUG", stream=stream)


# ----- Monitor.run() loop -----


def test_run_calls_run_once_then_sleeps(tmp_path: Path) -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    mon = Monitor(fake, Config(), _logger(io.StringIO()), rng=random.Random(0),
                  sleep=lambda _s: None)
    calls: list[int] = []
    mon.run_once = lambda: calls.append(1)  # type: ignore[method-assign]

    def sleeper(_s: float) -> None:
        raise _StopLoop

    mon._sleep = sleeper  # type: ignore[assignment]
    with pytest.raises(_StopLoop):
        mon.run()
    assert calls == [1]


def test_run_survives_a_failing_iteration() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    stream = io.StringIO()
    mon = Monitor(fake, Config(), _logger(stream), rng=random.Random(0), sleep=lambda _s: None)

    seq = iter([RuntimeError("boom"), None])

    def run_once() -> None:
        item = next(seq)
        if item is not None:
            raise item

    mon.run_once = run_once  # type: ignore[method-assign]

    sleeps = [0]

    def sleeper(_s: float) -> None:
        sleeps[0] += 1
        if sleeps[0] >= 2:  # let the loop run twice, then stop
            raise _StopLoop

    mon._sleep = sleeper  # type: ignore[assignment]
    with pytest.raises(_StopLoop):
        mon.run()
    assert "Unhandled error in monitor loop" in stream.getvalue()


def test_announce_logs_connection_and_endpoint() -> None:
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    stream = io.StringIO()
    cfg = Config(docker_host="tcp://proxy:2375")
    Monitor(fake, cfg, _logger(stream)).announce()
    out = stream.getvalue()
    assert "socket proxy (tcp://proxy:2375)" in out
    assert "[ENDPOINT]" in out


# ----- cli.main([]) -----


def _prep_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    conf = tmp_path / "sites.conf"
    conf.write_text("https://www.google.com\n")
    monkeypatch.setenv("CONFIG_FILE", str(conf))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "monitor.log"))
    monkeypatch.setenv("GLUETUN_CONTAINER", "gluetun")


def test_main_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _prep_env(monkeypatch, tmp_path)
    fake = FakeDockerClient()
    fake.add_container("gluetun", id=GLUETUN_ID)
    monkeypatch.setattr(cli, "DockerPyClient", lambda **_kw: fake)
    ran: list[int] = []
    monkeypatch.setattr(cli.Monitor, "announce", lambda self: None)
    monkeypatch.setattr(cli.Monitor, "run", lambda self: ran.append(1))
    assert cli.main([]) == 0
    assert ran == [1]


def test_main_client_init_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prep_env(monkeypatch, tmp_path)

    def boom(**_kw: object) -> object:
        raise RuntimeError("no docker")

    monkeypatch.setattr(cli, "DockerPyClient", boom)
    assert cli.main([]) == 1


def test_main_prereq_failure_returns_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prep_env(monkeypatch, tmp_path)
    fake = FakeDockerClient()  # no gluetun container -> prereq fails
    monkeypatch.setattr(cli, "DockerPyClient", lambda **_kw: fake)
    assert cli.main([]) == 1


def test_signal_handler_exits_cleanly() -> None:
    logger = _logger(io.StringIO())
    cli._install_signal_handlers(logger)
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    with pytest.raises(SystemExit) as exc:
        handler(signal.SIGTERM, None)
    assert exc.value.code == 0
