"""Tests d'intégration de synergy/resilience.py (écarts avance/retard).

Jeu de données synthétique déterministe : champion 75 (JUNGLE, équipe 100)
gagne TOUJOURS quand son équipe est en avance au gold@15, perd TOUJOURS
quand elle est en retard — reproduit artificiellement un cas extrême pour
vérifier que l'agrégation avance/retard par champion capture bien l'écart,
plutôt que de dépendre d'un vrai pattern statistique bruité comme Nasus.
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import resilience, windows

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)

_ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
_BASE_GOLD_15 = 1000


async def _seed_match(conn, match_id: str, patch: str, *, gold_diff: int, win: bool) -> None:
    """Équipe 100 (jungle=champion 75) : gold_diff donné, gagne ssi `win`.
    Équipe 200 (jungle=champion 88) : miroir exact (déjà couvert par les
    tests gold_factors, ici on ne vérifie que le champion 75)."""
    await conn.execute(
        "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
        " game_creation, game_duration_s, winning_team)"
        " VALUES (%s, 'euw1', %s, %s, 420, now(), 1800, %s)",
        (match_id, patch, f"{patch}.1", 100 if win else 200),
    )
    await conn.execute(
        "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
        " sup_champion, win, herald_taken, soul_taken, first_tower, jgl_cs_diff_15)"
        " VALUES (%s, 100, 75, 2, 3, %s, false, false, false, 0),"
        "        (%s, 200, 88, 12, 13, %s, false, false, false, 0)",
        (match_id, win, match_id, not win),
    )
    per_role_delta = gold_diff // 5
    jgl_champs = {100: 75, 200: 88}
    for team_id, gold, w in (
        (100, _BASE_GOLD_15 + per_role_delta, win),
        (200, _BASE_GOLD_15, not win),
    ):
        for i, role in enumerate(_ROLES):
            champion_id = jgl_champs[team_id] if role == "JUNGLE" else team_id + i
            await conn.execute(
                "INSERT INTO match_role_stats (match_id, team_id, role, champion_id, win,"
                " gold_15, first_blood)"
                " VALUES (%s, %s, %s, %s, %s, %s, false)",
                (match_id, team_id, role, champion_id, w, gold),
            )


async def test_refresh_captures_champion_specific_ahead_behind_gap(pg_conn):
    for i in range(10):
        await _seed_match(pg_conn, f"16.13_ahead_{i}", "16.13", gold_diff=500, win=True)
    for i in range(10):
        await _seed_match(pg_conn, f"16.13_behind_{i}", "16.13", gold_diff=-500, win=False)

    count = resilience.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_rows=50)
    # Picks identiques sur les 20 games (5 rôles × 2 équipes = 10 combos
    # (role, champion) distincts) × 3 facteurs.
    assert count == 10 * len(resilience.FACTORS)

    cur = await pg_conn.execute(
        "SELECT games_ahead, wins_ahead, games_behind, wins_behind"
        " FROM score_champion_resilience"
        " WHERE window_label = '16.13' AND role = 'JUNGLE' AND champion_id = 75"
        " AND factor = 'team_gold_diff_15'"
    )
    row = await cur.fetchone()
    assert row == (10, 10, 10, 0)  # gagne toujours en avance, jamais en retard
