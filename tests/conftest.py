"""Shared fixtures: a fake client, a captured-output logger, and a base Config."""

from __future__ import annotations

import io

import pytest

from gluetun_monitor.config import Config
from gluetun_monitor.logging_setup import Logger

from .fakes import FakeDockerClient


@pytest.fixture
def fake_client() -> FakeDockerClient:
    return FakeDockerClient()


@pytest.fixture
def log_stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def logger(log_stream: io.StringIO) -> Logger:
    # DEBUG level so tests can assert on debug lines; no file (stream only).
    return Logger(log_file=None, level="DEBUG", stream=log_stream)


@pytest.fixture
def config() -> Config:
    # Defaults match the v1.x contract; tests override fields as needed.
    return Config()


@pytest.fixture
def no_sleep():
    """A sleep callable that does nothing — keeps recovery tests instant."""
    return lambda _seconds: None
