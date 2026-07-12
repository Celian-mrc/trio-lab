"""Tests du mode service : patch auto, enchaînement des cycles, résilience."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trio_lab.collector import patches, service

# --- patches : résolution du patch courant et bornes de repli ---


def test_from_version_keeps_major_minor():
    assert patches.from_version("16.14.1") == "16.14"
    assert patches.from_version("16.9") == "16.9"


def test_current_patch_uses_latest_ddragon_version(monkeypatch):
    monkeypatch.setattr(patches, "_fetch_versions", lambda: ["16.14.1", "16.13.1"])
    assert patches.current_patch() == "16.14"


def test_service_bounds_known_patch_uses_patch_dates():
    assert patches.service_bounds_for("16.13") == patches.PATCH_DATES["16.13"]


def test_service_bounds_unknown_patch_falls_back_around_now():
    start, end = patches.service_bounds_for("16.99")
    now = datetime.now(UTC)
    assert start < now < end
    assert now - start <= timedelta(days=17)  # lookback large mais borné
    assert end - now <= timedelta(days=3)


# --- run_service : enchaînement batch → refresh → purge, et résilience ---


class _Recorder:
    """Espionne l'enchaînement d'un cycle sans réseau ni base."""

    def __init__(self, monkeypatch, *, patch_values=("16.14",)):
        self.calls: list[tuple] = []
        values = iter(patch_values)

        def fake_current_patch():
            value = next(values)
            self.calls.append(("patch", value))
            if isinstance(value, Exception):
                raise value
            return value

        async def fake_collect_run(**kwargs):
            self.calls.append(("collect", kwargs["patch"], kwargs["target"]))
            assert kwargs["strict_patch_bounds"] is False
            return {}

        monkeypatch.setattr(service.patches, "current_patch", fake_current_patch)
        monkeypatch.setattr(service.collect, "run", fake_collect_run)
        monkeypatch.setattr(
            service, "refresh_scores", lambda patch, dsn=None: self.calls.append(("refresh", patch))
        )
        monkeypatch.setattr(
            service.maintenance,
            "purge_stale_objective_events",
            lambda dsn=None: self.calls.append(("purge_events",)),
        )
        monkeypatch.setattr(
            service.maintenance,
            "run_daily",
            lambda dsn=None: self.calls.append(("purge_daily",)),
        )
        monkeypatch.setattr(service.time, "sleep", lambda s: self.calls.append(("sleep", s)))


def test_cycle_chains_batch_refresh_and_daily_purge(monkeypatch):
    rec = _Recorder(monkeypatch, patch_values=("16.13", "16.14"))
    cycles = service.run_service(platforms=["euw1"], batch_target=10, max_cycles=2)
    assert cycles == 2
    # Le patch est re-résolu à CHAQUE cycle (rollover sans intervention) ;
    # les events sont purgés à CHAQUE cycle ; la purge quotidienne (patchs
    # bruts + participants + agrégats) ne tourne qu'une fois (intervalle pas
    # encore écoulé au 2e cycle).
    assert rec.calls == [
        ("patch", "16.13"),
        ("collect", "16.13", 10),
        ("refresh", "16.13"),
        ("purge_events",),
        ("purge_daily",),
        ("patch", "16.14"),
        ("collect", "16.14", 10),
        ("refresh", "16.14"),
        ("purge_events",),
    ]


def test_failing_cycle_pauses_then_retries(monkeypatch):
    rec = _Recorder(monkeypatch, patch_values=(RuntimeError("ddragon down"), "16.14"))
    cycles = service.run_service(platforms=["euw1"], max_cycles=2)
    assert cycles == 2
    # Cycle 1 : échec → pause ; cycle 2 : complet. Le service ne meurt pas.
    assert ("sleep", service.CYCLE_ERROR_PAUSE_S) in rec.calls
    assert ("refresh", "16.14") in rec.calls


# --- refresh_scores : enchaîne agrégats → scores/counters → purge des scores ---


def test_refresh_scores_chains_and_prunes(monkeypatch):
    calls: list[tuple] = []
    fake_window = object()
    monkeypatch.setattr(
        service.aggregate, "refresh", lambda patch, dsn=None: calls.append(("agg", patch))
    )
    monkeypatch.setattr(service, "scoring_window", lambda dsn=None: fake_window)
    monkeypatch.setattr(
        service.compute, "refresh", lambda window, dsn=None: calls.append(("compute", window))
    )
    monkeypatch.setattr(
        service.counters, "refresh", lambda window, dsn=None: calls.append(("counters", window))
    )
    monkeypatch.setattr(
        service.allies, "refresh", lambda window, dsn=None: calls.append(("allies", window))
    )
    monkeypatch.setattr(
        service.maintenance, "purge_stale_scores", lambda dsn=None: calls.append(("prune_scores",))
    )
    service.refresh_scores("16.13")
    assert calls == [
        ("agg", "16.13"),
        ("compute", fake_window),
        ("counters", fake_window),
        ("allies", fake_window),
        ("prune_scores",),
    ]


def test_refresh_scores_skips_compute_when_window_empty(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        service.aggregate, "refresh", lambda patch, dsn=None: calls.append(("agg", patch))
    )
    monkeypatch.setattr(service, "scoring_window", lambda dsn=None: None)
    monkeypatch.setattr(
        service.maintenance, "purge_stale_scores", lambda dsn=None: calls.append(("prune_scores",))
    )
    service.refresh_scores("16.13")
    # Base vide : ni compute/counters ni purge des scores (rien à purger).
    assert calls == [("agg", "16.13")]


# --- scoring_window : fenêtre bornée aux 3 patchs les plus récents ---


def test_scoring_window_orders_and_caps_patches(monkeypatch):
    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def execute(self, sql):
            class _Cur:
                @staticmethod
                def fetchall():
                    return [("16.11",), ("16.13",), ("16.10",), ("16.12",)]

            return _Cur()

    monkeypatch.setattr(service.psycopg, "connect", lambda dsn: _FakeConn())
    monkeypatch.setattr(service.db, "require_dsn", lambda dsn: "postgres://fake")
    window = service.scoring_window()
    assert window.patches == ("16.13", "16.12", "16.11")  # 16.10 hors fenêtre


def test_scoring_window_empty_db_returns_none(monkeypatch):
    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def execute(self, sql):
            class _Cur:
                @staticmethod
                def fetchall():
                    return []

            return _Cur()

    monkeypatch.setattr(service.psycopg, "connect", lambda dsn: _FakeConn())
    monkeypatch.setattr(service.db, "require_dsn", lambda dsn: "postgres://fake")
    assert service.scoring_window() is None
