"""Test d'intégration de `sync_theoretical` (Data Dragon simulé, base réelle)."""

from __future__ import annotations

import psycopg
import pytest

from trio_lab.ccref import sync_theoretical

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


async def test_sync_writes_resolved_champions_only(pg_conn, monkeypatch):
    monkeypatch.setattr(
        sync_theoretical.champions, "fetch_name_to_id", lambda: {"Leona": 89, "Zed": 238}
    )
    monkeypatch.setattr(
        sync_theoretical.score,
        "champion_scores",
        lambda: {"Leona": 5.5, "Zed": 0.3, "Inconnu": 9.9},  # "Inconnu" non résolu, ignoré
    )
    n = sync_theoretical.sync(dsn=TEST_DSN)
    assert n == 2

    with psycopg.connect(TEST_DSN) as conn:
        rows = dict(
            conn.execute("SELECT champion_id, score FROM champion_cc_theoretical").fetchall()
        )
    assert rows == {89: pytest.approx(5.5), 238: pytest.approx(0.3)}


async def test_sync_is_idempotent(pg_conn, monkeypatch):
    monkeypatch.setattr(sync_theoretical.champions, "fetch_name_to_id", lambda: {"Leona": 89})
    monkeypatch.setattr(sync_theoretical.score, "champion_scores", lambda: {"Leona": 5.5})
    first = sync_theoretical.sync(dsn=TEST_DSN)
    monkeypatch.setattr(sync_theoretical.score, "champion_scores", lambda: {"Leona": 6.0})
    second = sync_theoretical.sync(dsn=TEST_DSN)
    assert (first, second) == (1, 1)

    with psycopg.connect(TEST_DSN) as conn:
        cur = conn.execute("SELECT score FROM champion_cc_theoretical WHERE champion_id = 89")
        assert cur.fetchone()[0] == pytest.approx(6.0)
