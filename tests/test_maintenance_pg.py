"""Tests d'intégration de la rétention par patch (purge des matchs anciens)."""

from __future__ import annotations

import pytest

from trio_lab import maintenance

from .conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


async def _seed(conn, patches: list[str]) -> None:
    for i, patch in enumerate(patches):
        await conn.execute(
            "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
            " game_creation, game_duration_s, winning_team)"
            " VALUES (%s, 'euw1', %s, %s, 420, now(), 1800, 100)",
            (f"EUW1_{i}", patch, f"{patch}.1"),
        )
        await conn.execute(
            "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
            " sup_champion, win) VALUES (%s, 100, 1, 2, 3, true)",
            (f"EUW1_{i}",),
        )
    # Scores et journal : doivent SURVIVRE à la purge.
    await conn.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES (%s, 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')",
        (patches[0],),
    )
    await conn.execute(
        "INSERT INTO match_fetch_journal (match_id, platform, status, reason)"
        " VALUES ('EUW1_OLD_EXCLUDED', 'euw1', 'excluded', 'duration')"
    )


async def test_purge_keeps_recent_patches_and_scores(pg_conn):
    await _seed(pg_conn, ["16.10", "16.11", "16.12", "16.13"])
    report = maintenance.purge_old_patches(keep=3, dsn=TEST_DSN)
    assert report == {"purged_patches": ["16.10"], "matches_deleted": 1}

    cur = await pg_conn.execute("SELECT DISTINCT patch FROM matches ORDER BY patch")
    assert [r[0] for r in await cur.fetchall()] == ["16.11", "16.12", "16.13"]
    # Cascade : les trio_stats du match purgé sont partis avec lui.
    cur = await pg_conn.execute("SELECT count(*) FROM match_trio_stats")
    assert (await cur.fetchone())[0] == 3
    # Scores et journal intacts (historique gratuit + anti re-téléchargement).
    cur = await pg_conn.execute("SELECT count(*) FROM score_trio")
    assert (await cur.fetchone())[0] == 1
    cur = await pg_conn.execute("SELECT count(*) FROM match_fetch_journal")
    assert (await cur.fetchone())[0] == 1


async def test_purge_noop_when_few_patches(pg_conn):
    await _seed(pg_conn, ["16.12", "16.13"])
    report = maintenance.purge_old_patches(keep=3, dsn=TEST_DSN)
    assert report == {"purged_patches": [], "matches_deleted": 0}
    cur = await pg_conn.execute("SELECT count(*) FROM matches")
    assert (await cur.fetchone())[0] == 2


async def test_dry_run_deletes_nothing(pg_conn):
    await _seed(pg_conn, ["16.10", "16.11", "16.12", "16.13"])
    report = maintenance.purge_old_patches(keep=1, dry_run=True, dsn=TEST_DSN)
    assert report["purged_patches"] == ["16.12", "16.11", "16.10"]  # du plus récent au plus vieux
    assert report["matches_deleted"] == 0
    cur = await pg_conn.execute("SELECT count(*) FROM matches")
    assert (await cur.fetchone())[0] == 4


def test_keep_must_be_positive():
    with pytest.raises(ValueError, match="keep"):
        maintenance.purge_old_patches(keep=0, dsn="postgres://unused")
