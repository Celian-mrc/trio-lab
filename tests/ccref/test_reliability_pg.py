"""Tests d'intégration de `ccref.reliability.backfill_trio_cc` (Postgres réel, connexion sync)."""

from __future__ import annotations

import psycopg
import pytest

from trio_lab import db
from trio_lab.ccref import reliability

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


@pytest.fixture
def pg_sync():
    db.apply_migrations(TEST_DSN)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE players, matches, match_fetch_journal,"
            " agg_champion, agg_duo, agg_trio, agg_trio_vs_champion, agg_trio_with_ally,"
            " score_duo, score_trio, score_trio_vs_champion, score_trio_with_ally,"
            " champion_cc_theoretical CASCADE"
        )
        yield conn


def test_backfill_trio_cc_applies_hardcoded_reliability(pg_sync):
    # Trio jgl(56)=Nocturne, mid(2), sup(3) — un seul match, on vérifie que
    # match_trio_stats.cc_time_s est recalculé depuis match_participants
    # avec le coefficient codé en dur, pas juste copié.
    pg_sync.execute(
        "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
        " game_creation, game_duration_s, winning_team)"
        " VALUES ('EUW1_BF1', 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)"
    )
    for role, champ, cc in (("JUNGLE", 56, 200), ("MIDDLE", 2, 40), ("UTILITY", 3, 20)):
        pg_sync.execute(
            "INSERT INTO match_participants (match_id, team_id, role, champion_id, win,"
            " cc_time_s, immobilizations) VALUES ('EUW1_BF1', 100, %s, %s, true, %s, 5)",
            (role, champ, cc),
        )
    pg_sync.execute(
        "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
        " sup_champion, win, cc_time_s) VALUES ('EUW1_BF1', 100, 56, 2, 3, true, 260)"
    )
    n = reliability.backfill_trio_cc(pg_sync, reliability={56: 0.25})
    assert n == 1
    corrected = pg_sync.execute(
        "SELECT cc_time_s FROM match_trio_stats WHERE match_id = 'EUW1_BF1' AND team_id = 100"
    ).fetchone()[0]
    # 200×0.25 + 40×1.0 + 20×1.0 = 110 (arrondi, champs 2/3 absents du dict → coalesce 1.0).
    assert corrected == 110


def test_backfill_trio_cc_defaults_to_module_constant(pg_sync):
    """Sans argument `reliability`, utilise `CC_TIME_RELIABILITY` (Nocturne 0.22)."""
    pg_sync.execute(
        "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
        " game_creation, game_duration_s, winning_team)"
        " VALUES ('EUW1_BF2', 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)"
    )
    for role, champ, cc in (("JUNGLE", 56, 200), ("MIDDLE", 2, 40), ("UTILITY", 3, 20)):
        pg_sync.execute(
            "INSERT INTO match_participants (match_id, team_id, role, champion_id, win,"
            " cc_time_s, immobilizations) VALUES ('EUW1_BF2', 100, %s, %s, true, %s, 5)",
            (role, champ, cc),
        )
    pg_sync.execute(
        "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
        " sup_champion, win, cc_time_s) VALUES ('EUW1_BF2', 100, 56, 2, 3, true, 260)"
    )
    reliability.backfill_trio_cc(pg_sync)
    corrected = pg_sync.execute(
        "SELECT cc_time_s FROM match_trio_stats WHERE match_id = 'EUW1_BF2' AND team_id = 100"
    ).fetchone()[0]
    expected = round(200 * reliability.CC_TIME_RELIABILITY[56] + 40 + 20)
    assert corrected == expected


def test_backfill_trio_cc_scoped_by_patch(pg_sync):
    for match_id, patch in (("EUW1_P1", "16.13"), ("EUW1_P2", "16.12")):
        pg_sync.execute(
            "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
            " game_creation, game_duration_s, winning_team)"
            " VALUES (%s, 'euw1', %s, %s, 420, now(), 1800, 100)",
            (match_id, patch, f"{patch}.1"),
        )
        pg_sync.execute(
            "INSERT INTO match_participants (match_id, team_id, role, champion_id, win,"
            " cc_time_s, immobilizations) VALUES (%s, 100, 'JUNGLE', 56, true, 200, 5)",
            (match_id,),
        )
        for role, champ in (("MIDDLE", 2), ("UTILITY", 3)):
            pg_sync.execute(
                "INSERT INTO match_participants (match_id, team_id, role, champion_id, win,"
                " cc_time_s, immobilizations) VALUES (%s, 100, %s, %s, true, 0, 5)",
                (match_id, role, champ),
            )
        pg_sync.execute(
            "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
            " sup_champion, win, cc_time_s) VALUES (%s, 100, 56, 2, 3, true, 260)",
            (match_id,),
        )
    n = reliability.backfill_trio_cc(pg_sync, patch="16.13", reliability={56: 0.5})
    assert n == 1  # seul EUW1_P1 (16.13) touché
    cc_p1, cc_p2 = (
        pg_sync.execute(
            "SELECT cc_time_s FROM match_trio_stats WHERE match_id = %s", (m,)
        ).fetchone()[0]
        for m in ("EUW1_P1", "EUW1_P2")
    )
    assert cc_p1 == 100  # 200×0.5
    assert cc_p2 == 260  # inchangé, hors patch ciblé
