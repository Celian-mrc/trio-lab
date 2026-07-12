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
    # score_trio est une table à part (purge_stale_scores, testée plus bas) :
    # purge_old_patches ne la touche jamais.
    cur = await pg_conn.execute("SELECT count(*) FROM score_trio")
    assert (await cur.fetchone())[0] == 1
    # Le journal n'est jamais purgé (anti re-téléchargement d'un match exclu).
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


# --- purge_stale_objective_events : cadence horaire, indépendante du patch ---


async def test_purge_events_removes_only_old_rows(pg_conn):
    await pg_conn.execute(
        "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
        " game_creation, game_duration_s, winning_team, collected_at)"
        " VALUES ('EUW1_OLD', 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100,"
        " now() - interval '2 days'),"
        " ('EUW1_NEW', 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100, now())"
    )
    for match_id in ("EUW1_OLD", "EUW1_NEW"):
        await pg_conn.execute(
            "INSERT INTO match_objective_events (match_id, seq, ts_s, event_type, team_id)"
            " VALUES (%s, 0, 60, 'FIRST_BLOOD', 100)",
            (match_id,),
        )
    report = maintenance.purge_stale_objective_events(older_than_hours=24, dsn=TEST_DSN)
    assert report == {"events_deleted": 1}
    cur = await pg_conn.execute("SELECT match_id FROM match_objective_events")
    assert [r[0] for r in await cur.fetchall()] == ["EUW1_NEW"]
    # matches/match_trio_stats ne sont pas concernés par cette purge.
    cur = await pg_conn.execute("SELECT count(*) FROM matches")
    assert (await cur.fetchone())[0] == 2


# --- purge_stale_participants : profondeur 1 patch (relu seulement pour le patch en cours) ---


async def test_purge_participants_keeps_only_current_patch(pg_conn):
    for i, patch in enumerate(["16.12", "16.13"]):
        await pg_conn.execute(
            "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
            " game_creation, game_duration_s, winning_team)"
            " VALUES (%s, 'euw1', %s, %s, 420, now(), 1800, 100)",
            (f"EUW1_{i}", patch, f"{patch}.1"),
        )
        await pg_conn.execute(
            "INSERT INTO match_participants (match_id, team_id, role, champion_id, win)"
            " VALUES (%s, 100, 'JUNGLE', 1, true)",
            (f"EUW1_{i}",),
        )
    report = maintenance.purge_stale_participants(keep=1, dsn=TEST_DSN)
    assert report == {"purged_patches": ["16.12"], "participants_deleted": 1}
    cur = await pg_conn.execute(
        "SELECT m.patch FROM match_participants p JOIN matches m USING (match_id)"
    )
    assert [r[0] for r in await cur.fetchall()] == ["16.13"]
    # matches lui-même n'est pas touché par cette purge (rôle de purge_old_patches).
    cur = await pg_conn.execute("SELECT count(*) FROM matches")
    assert (await cur.fetchone())[0] == 2


# --- purge_stale_aggregates : source de vérité = agg_trio, pas matches ---


async def _seed_agg(conn, patches: list[str]) -> None:
    for patch in patches:
        await conn.execute(
            "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
            " VALUES (%s, 'euw1', 'JUNGLE', 1, 1, 1)",
            (patch,),
        )
        await conn.execute(
            "INSERT INTO agg_duo (patch, platform, roles, champ_a, champ_b, games, wins)"
            " VALUES (%s, 'euw1', 'jgl_mid', 1, 2, 1, 1)",
            (patch,),
        )
        await conn.execute(
            "INSERT INTO agg_trio (patch, platform, jgl_champion, mid_champion, sup_champion,"
            " games, wins) VALUES (%s, 'euw1', 1, 2, 3, 1, 1)",
            (patch,),
        )
        await conn.execute(
            "INSERT INTO agg_trio_vs_champion (patch, platform, jgl_champion, mid_champion,"
            " sup_champion, enemy_role, enemy_champion, games, wins)"
            " VALUES (%s, 'euw1', 1, 2, 3, 'TOP', 9, 1, 1)",
            (patch,),
        )
        await conn.execute(
            "INSERT INTO agg_trio_with_ally (patch, platform, jgl_champion, mid_champion,"
            " sup_champion, ally_role, ally_champion, games, wins)"
            " VALUES (%s, 'euw1', 1, 2, 3, 'TOP', 9, 1, 1)",
            (patch,),
        )
        await conn.execute(
            "INSERT INTO agg_trio_duration (patch, platform, jgl_champion, mid_champion,"
            " sup_champion, duration_bucket, games, wins)"
            " VALUES (%s, 'euw1', 1, 2, 3, 20, 1, 1)",
            (patch,),
        )
        await conn.execute(
            "INSERT INTO agg_duo_duration (patch, platform, roles, champ_a, champ_b,"
            " duration_bucket, games, wins) VALUES (%s, 'euw1', 'jgl_mid', 1, 2, 20, 1, 1)",
            (patch,),
        )


async def test_purge_aggregates_survives_raw_purge(pg_conn):
    """agg_* garde une profondeur indépendante des tables brutes déjà purgées."""
    await _seed_agg(pg_conn, ["16.10", "16.11", "16.12", "16.13"])
    report = maintenance.purge_stale_aggregates(keep=2, dsn=TEST_DSN)
    assert report["purged_patches"] == ["16.11", "16.10"]
    assert report["agg_rows_deleted"] == 14  # 2 patchs × 7 tables
    for table in (
        "agg_champion",
        "agg_duo",
        "agg_trio",
        "agg_trio_vs_champion",
        "agg_trio_with_ally",
        "agg_trio_duration",
        "agg_duo_duration",
    ):
        cur = await pg_conn.execute(f"SELECT DISTINCT patch FROM {table} ORDER BY patch")  # noqa: S608
        assert [r[0] for r in await cur.fetchall()] == ["16.12", "16.13"], table


# --- purge_stale_scores : une seule fenêtre visible (available_windows) ---


async def _seed_score_window(conn, window_label: str) -> None:
    await conn.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES (%s, 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')",
        (window_label,),
    )
    await conn.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier)"
        " VALUES (%s, 'euw1', 'jgl_mid', 1, 2, 1, 1.0, 1.0, 0.0, 0.0, 1.0, 'faible')",
        (window_label,),
    )
    await conn.execute(
        "INSERT INTO score_trio_vs_champion (window_label, platform, jgl_champion,"
        " mid_champion, sup_champion, enemy_role, enemy_champion, games, games_eff, wr,"
        " delta_raw, delta, ci_low, ci_high, tier)"
        " VALUES (%s, 'euw1', 1, 2, 3, 'TOP', 9, 1, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 'faible')",
        (window_label,),
    )
    await conn.execute(
        "INSERT INTO score_trio_with_ally (window_label, platform, jgl_champion,"
        " mid_champion, sup_champion, ally_role, ally_champion, games, games_eff, wr,"
        " uplift_raw, uplift, ci_low, ci_high, tier)"
        " VALUES (%s, 'euw1', 1, 2, 3, 'TOP', 9, 1, 1.0, 1.0, 0.0, 0.0, 0.0, 1.0, 'faible')",
        (window_label,),
    )


async def test_purge_scores_keeps_only_most_recent_window(pg_conn):
    await _seed_score_window(pg_conn, "16.12")
    await _seed_score_window(pg_conn, "16.13+16.12")  # plus récent (16.13 en tête)
    report = maintenance.purge_stale_scores(dsn=TEST_DSN)  # keep=1 par défaut
    assert report == {"purged_window_labels": ["16.12"], "score_rows_deleted": 4}
    for table in ("score_trio", "score_duo", "score_trio_vs_champion", "score_trio_with_ally"):
        cur = await pg_conn.execute(
            f"SELECT DISTINCT window_label FROM {table}"  # noqa: S608
        )
        assert [r[0] for r in await cur.fetchall()] == ["16.13+16.12"], table


async def test_purge_scores_dry_run_deletes_nothing(pg_conn):
    await _seed_score_window(pg_conn, "16.12")
    await _seed_score_window(pg_conn, "16.13")
    report = maintenance.purge_stale_scores(dry_run=True, dsn=TEST_DSN)
    assert report["purged_window_labels"] == ["16.12"]
    assert report["score_rows_deleted"] == 0
    cur = await pg_conn.execute("SELECT count(DISTINCT window_label) FROM score_trio")
    assert (await cur.fetchone())[0] == 2


# --- run_daily : les 3 purges à profondeur de patch, en une fois ---


async def test_run_daily_chains_all_patch_depth_purges(pg_conn):
    await _seed(pg_conn, ["16.10", "16.11", "16.12", "16.13"])
    await _seed_agg(pg_conn, ["16.10", "16.11", "16.12", "16.13", "16.14"])
    report = maintenance.run_daily(dsn=TEST_DSN)
    assert report["matches"]["purged_patches"] == ["16.10"]  # RAW_KEEP = 3
    assert report["aggregates"]["purged_patches"] == ["16.10"]  # AGG_KEEP = 4
    cur = await pg_conn.execute("SELECT DISTINCT patch FROM matches ORDER BY patch")
    assert [r[0] for r in await cur.fetchall()] == ["16.11", "16.12", "16.13"]
