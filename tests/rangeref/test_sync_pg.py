"""Test d'intégration de `rangeref.sync` (Data Dragon simulé, base réelle)."""

from __future__ import annotations

import psycopg
import pytest

from trio_lab.rangeref import sync

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


async def test_sync_writes_all_fetched_champions(pg_conn, monkeypatch):
    monkeypatch.setattr(sync.score, "fetch_champion_scores", lambda: {89: 1025.0, 238: 1275.0})
    n = sync.sync(dsn=TEST_DSN)
    assert n == 2

    with psycopg.connect(TEST_DSN) as conn:
        rows = dict(
            conn.execute("SELECT champion_id, score FROM champion_range_theoretical").fetchall()
        )
    assert rows == {89: pytest.approx(1025.0), 238: pytest.approx(1275.0)}


async def test_sync_is_idempotent(pg_conn, monkeypatch):
    monkeypatch.setattr(sync.score, "fetch_champion_scores", lambda: {89: 1025.0})
    first = sync.sync(dsn=TEST_DSN)
    monkeypatch.setattr(sync.score, "fetch_champion_scores", lambda: {89: 1100.0})
    second = sync.sync(dsn=TEST_DSN)
    assert (first, second) == (1, 1)

    with psycopg.connect(TEST_DSN) as conn:
        cur = conn.execute("SELECT score FROM champion_range_theoretical WHERE champion_id = 89")
        assert cur.fetchone()[0] == pytest.approx(1100.0)
