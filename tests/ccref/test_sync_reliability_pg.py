"""Test d'intégration de `sync_reliability` (base réelle, connexion sync)."""

from __future__ import annotations

import psycopg
import pytest

from trio_lab import db
from trio_lab.ccref import sync_reliability

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
            " agg_champion, agg_duo, agg_trio, agg_trio_vs_champion,"
            " score_duo, score_trio, score_trio_vs_champion,"
            " champion_cc_theoretical, champion_cc_reliability CASCADE"
        )
        yield conn


def _seed(conn, champion_id: int, n: int, cc: int, immo: int) -> None:
    for i in range(n):
        match_id = f"EUW1_{champion_id}_{i}"
        conn.execute(
            "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
            " game_creation, game_duration_s, winning_team)"
            " VALUES (%s, 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)",
            (match_id,),
        )
        conn.execute(
            "INSERT INTO match_participants (match_id, team_id, role, champion_id, win,"
            " cc_time_s, immobilizations) VALUES (%s, 100, 'JUNGLE', %s, true, %s, %s)",
            (match_id, champion_id, cc, immo),
        )


def _seed_normal_population(conn) -> None:
    """8 champions au profil de kit normal (ratio 0.5 à 2.3 s/immo) — la
    barrière de Tukey a besoin d'au moins 4 points pour une estimation de
    quartiles stable (cf. ccref.reliability.OUTLIER_IQR_MULTIPLIER)."""
    for champ_id, ratio in enumerate((0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.3), start=1001):
        cc = 100
        _seed(conn, champ_id, 60, cc, round(cc / ratio))


def test_sync_writes_reliability_table(pg_sync):
    _seed_normal_population(pg_sync)
    _seed(pg_sync, 56, 60, 2150, 100)  # Nocturne : ratio 21.5, très au-delà de la barrière
    n = sync_reliability.sync(dsn=TEST_DSN, min_games=50)
    assert n == 9

    rows = dict(
        pg_sync.execute("SELECT champion_id, reliability FROM champion_cc_reliability").fetchall()
    )
    assert rows[1001] == pytest.approx(1.0)
    assert rows[56] < 1.0


def test_sync_with_backfill_updates_match_trio_stats(pg_sync):
    _seed_normal_population(pg_sync)
    _seed(pg_sync, 56, 60, 2150, 100)
    _seed(pg_sync, 2, 60, 40, 40)
    pg_sync.execute(
        "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
        " game_creation, game_duration_s, winning_team)"
        " VALUES ('EUW1_TRIO1', 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)"
    )
    for role, champ, cc in (("JUNGLE", 56, 200), ("MIDDLE", 2, 40), ("UTILITY", 3, 20)):
        pg_sync.execute(
            "INSERT INTO match_participants (match_id, team_id, role, champion_id, win,"
            " cc_time_s, immobilizations) VALUES ('EUW1_TRIO1', 100, %s, %s, true, %s, 5)",
            (role, champ, cc),
        )
    pg_sync.execute(
        "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
        " sup_champion, win, cc_time_s) VALUES ('EUW1_TRIO1', 100, 56, 2, 3, true, 260)"
    )
    sync_reliability.sync(dsn=TEST_DSN, min_games=50, backfill=True)

    reliability_56 = pg_sync.execute(
        "SELECT reliability FROM champion_cc_reliability WHERE champion_id = 56"
    ).fetchone()[0]
    corrected = pg_sync.execute(
        "SELECT cc_time_s FROM match_trio_stats WHERE match_id = 'EUW1_TRIO1' AND team_id = 100"
    ).fetchone()[0]
    # champ 3 absent de champion_cc_reliability → coalesce 1.0.
    assert corrected == pytest.approx(200 * reliability_56 + 40 * 1.0 + 20 * 1.0, abs=1)
