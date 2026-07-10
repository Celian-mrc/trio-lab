"""Tests d'intégration du backfill : matchs pré-Phase 2 → stats trio.

Postgres de test réel (gated), client Riot remplacé par un fake servant le
detail depuis les fixtures — aucun appel réseau.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from trio_lab.collector import parsing, storage
from trio_lab.stats import backfill

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "riot"
MATCH_ID = "EUW1_7913858220"


def _load(kind: str) -> dict:
    with gzip.open(FIXTURES / f"{MATCH_ID}.{kind}.json.gz", "rt", encoding="utf-8") as fh:
        return json.load(fh)


class _FakeClient:
    """Sert le detail de la fixture (le backfill re-fetch le detail, pas la timeline)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def get_match(self, match_id, *, platform):
        assert match_id == MATCH_ID
        return _load("detail")


async def test_backfill_extracts_from_archive(pg_conn, tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "RiotClient", _FakeClient)
    detail = _load("detail")
    # Match ingéré sans stats trio (état pré-Phase 2) + timeline archivée.
    await storage.insert_match(
        pg_conn, parsing.match_row(detail, platform="euw1"), parsing.participant_rows(detail)
    )
    storage.archive_timeline(tmp_path, "euw1", "16.13", MATCH_ID, _load("timeline"))

    counts = await backfill.run(data_dir=tmp_path, dsn=TEST_DSN)
    assert counts == {"backfilled": 1}

    cur = await pg_conn.execute(
        "SELECT team_id, drakes_taken, nashor_first FROM match_trio_stats ORDER BY team_id"
    )
    assert await cur.fetchall() == [(100, 0, False), (200, 3, False)]
    cur = await pg_conn.execute("SELECT count(*) FROM match_objective_events")
    assert (await cur.fetchone())[0] == 16

    # Relance : plus rien à faire.
    assert await backfill.run(data_dir=tmp_path, dsn=TEST_DSN) == {}


async def test_backfill_counts_missing_archives(pg_conn, tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "RiotClient", _FakeClient)
    detail = _load("detail")
    await storage.insert_match(
        pg_conn, parsing.match_row(detail, platform="euw1"), parsing.participant_rows(detail)
    )
    # Pas d'archive : le match est compté et sauté, pas d'échec.
    counts = await backfill.run(data_dir=tmp_path, dsn=TEST_DSN)
    assert counts == {"missing_timeline": 1}
