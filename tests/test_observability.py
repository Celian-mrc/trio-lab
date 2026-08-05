"""Tests de `trio_lab.observability` : no-op sans DSN, initialise sinon."""

from __future__ import annotations

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

    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", _FakeSentrySdk)
    observability.init_sentry()
    assert calls == {"dsn": "https://example.invalid/1", "traces_sample_rate": 0.0}
