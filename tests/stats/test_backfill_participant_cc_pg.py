"""Tests d'intégration du backfill des colonnes CC de `match_participants`.

Postgres de test réel (gated), client Riot remplacé par un fake servant le
detail depuis les fixtures — aucun appel réseau.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from trio_lab.collector import parsing, storage
from trio_lab.stats import backfill_participant_cc

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
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def get_match(self, match_id, *, platform):
        assert match_id == MATCH_ID
        return _load("detail")


async def _seed_match_without_participant_cc(conn) -> None:
    """Simule le trou historique : participants insérés sans cc_time_s/immobilizations
    (comme avant la migration 005), mais match_trio_stats déjà présent."""
    detail = _load("detail")
    row = parsing.match_row(detail, platform="euw1")
    participants = [
        {**p, "cc_time_s": None, "immobilizations": None} for p in parsing.participant_rows(detail)
    ]
    jgl, mid, sup = (
        next(p["champion_id"] for p in participants if p["team_id"] == 100 and p["role"] == role)
        for role in ("JUNGLE", "MIDDLE", "UTILITY")
    )
    trio_stats = [
        {
            "match_id": MATCH_ID,
            "team_id": 100,
            "jgl_champion": jgl,
            "mid_champion": mid,
            "sup_champion": sup,
            "win": True,
            **dict.fromkeys(
                (
                    "gold_diff_5",
                    "gold_diff_10",
                    "gold_diff_15",
                    "gold_diff_20",
                    "gold_diff_25",
                    "gold_diff_30",
                    "gold_diff_35",
                    "grubs_taken",
                    "herald_taken",
                    "drakes_taken",
                    "soul_taken",
                    "nashor_first",
                    "nashor_first_s",
                    "first_tower",
                    "towers_destroyed",
                    "plates_taken",
                    "first_blood_trio",
                    "kill_participation_pre15",
                    "damage_share",
                    "vision_score",
                    "jgl_cc_time_s",
                    "mid_cc_time_s",
                    "sup_cc_time_s",
                ),
                None,
            ),
            "cc_time_s": 999,  # ancienne valeur (non corrigée) à remplacer
        }
    ]
    await storage.insert_match(conn, row, participants, trio_stats, [])


async def test_backfill_populates_null_participant_cc(pg_conn, monkeypatch):
    monkeypatch.setattr(backfill_participant_cc, "RiotClient", _FakeClient)
    await _seed_match_without_participant_cc(pg_conn)

    counts = await backfill_participant_cc.run(dsn=TEST_DSN)
    assert counts == {"updated": 1}

    cur = await pg_conn.execute(
        "SELECT team_id, role, champion_id, cc_time_s, immobilizations"
        " FROM match_participants WHERE match_id = %s AND team_id = 100 ORDER BY role",
        (MATCH_ID,),
    )
    rows = {r[1]: r for r in await cur.fetchall()}
    assert rows["JUNGLE"][2:] == (876, 9, 3)
    assert rows["MIDDLE"][2:] == (126, 12, 29)

    # Relance : plus rien à backfiller.
    assert await backfill_participant_cc.run(dsn=TEST_DSN) == {}


async def test_backfill_scoped_to_jgl_champion(pg_conn, monkeypatch):
    monkeypatch.setattr(backfill_participant_cc, "RiotClient", _FakeClient)
    await _seed_match_without_participant_cc(pg_conn)

    # jgl réel = 876 (fixture) : un filtre sur un autre champion ne doit rien toucher.
    assert await backfill_participant_cc.run(dsn=TEST_DSN, jgl_champion=1) == {}
    cur = await pg_conn.execute(
        "SELECT cc_time_s FROM match_participants WHERE match_id = %s AND role = 'JUNGLE'",
        (MATCH_ID,),
    )
    assert (await cur.fetchone())[0] is None

    counts = await backfill_participant_cc.run(dsn=TEST_DSN, jgl_champion=876)
    assert counts == {"updated": 1}
