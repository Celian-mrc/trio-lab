"""Tests de `trio_lab.observability` : no-op sans config, initialise sinon."""

from __future__ import annotations

import logging
import sys

from trio_lab import observability


def test_init_sentry_is_noop_without_dsn(monkeypatch):
    monkeypatch.setattr(observability.config, "SENTRY_DSN", None)
    observability.init_sentry()  # ne doit pas lever, même si sentry_sdk absent


def test_init_sentry_initializes_with_dsn(monkeypatch):
    monkeypatch.setattr(observability.config, "SENTRY_DSN", "https://example.invalid/1")
    calls = {}

    class _FakeSentrySdk:
        @staticmethod
        def init(*, dsn, traces_sample_rate):
            calls["dsn"] = dsn
            calls["traces_sample_rate"] = traces_sample_rate

    monkeypatch.setitem(sys.modules, "sentry_sdk", _FakeSentrySdk)
    observability.init_sentry()
    assert calls == {"dsn": "https://example.invalid/1", "traces_sample_rate": 0.0}


def test_init_loki_logging_is_noop_without_config(monkeypatch):
    monkeypatch.setattr(observability.config, "LOKI_URL", None)
    monkeypatch.setattr(observability.config, "LOKI_USER", None)
    monkeypatch.setattr(observability.config, "LOKI_TOKEN", None)
    before = list(logging.getLogger().handlers)
    observability.init_loki_logging("collector")
    assert logging.getLogger().handlers == before


def test_init_loki_logging_adds_handler_with_config(monkeypatch):
    monkeypatch.setattr(
        observability.config, "LOKI_URL", "https://example.invalid/loki/api/v1/push"
    )
    monkeypatch.setattr(observability.config, "LOKI_USER", "user")
    monkeypatch.setattr(observability.config, "LOKI_TOKEN", "token")
    calls = {}

    class _FakeHandler(logging.Handler):
        def __init__(self, queue_, *, url, tags, auth, version):
            super().__init__()
            calls.update(url=url, tags=tags, auth=auth, version=version)

    class _FakeLoggingLoki:
        LokiQueueHandler = _FakeHandler

    monkeypatch.setitem(sys.modules, "logging_loki", _FakeLoggingLoki)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        observability.init_loki_logging("collector")
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
    assert calls == {
        "url": "https://example.invalid/loki/api/v1/push",
        "tags": {"service": "collector"},
        "auth": ("user", "token"),
        "version": "1",
    }
