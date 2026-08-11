"""Tests d'intégration de synergy/flex.py (profils de ressources par
(champion, rôle) matérialisés pour /flex).

Jeu de données synthétique : champion 1 (JUNGLE) a 35 games (≥ seuil de 30),
champion 2 (JUNGLE) n'en a que 10 (< seuil) — vérifie que la table `_profile`
exclut les combos sous le seuil (HAVING) tandis que `_baseline` (pas de
seuil, moyenne tous champions confondus du rôle) les inclut quand même.
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import flex, windows

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


async def _seed_role_games(
    conn, *, patch: str, champion_id: int, role: str, count: int, gold_15: int
) -> None:
    for i in range(count):
        match_id = f"{patch}_{role}_{champion_id}_{i}"
        await conn.execute(
            "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
            " game_creation, game_duration_s, winning_team)"
            " VALUES (%s, 'euw1', %s, %s, 420, now(), 1800, 100)",
            (match_id, patch, f"{patch}.1"),
        )
        await conn.execute(
            "INSERT INTO match_role_stats (match_id, team_id, role, champion_id, win,"
            " gold_15, dmg_per_gold)"
            " VALUES (%s, 100, %s, %s, true, %s, 1.5)",
            (match_id, role, champion_id, gold_15),
        )


async def test_refresh_excludes_profile_below_threshold_but_keeps_baseline(pg_conn):
    await _seed_role_games(
        pg_conn, patch="16.13", champion_id=1, role="JUNGLE", count=35, gold_15=1000
    )
    await _seed_role_games(
        pg_conn, patch="16.13", champion_id=2, role="JUNGLE", count=10, gold_15=500
    )

    n_profile, n_baseline = flex.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_games=30)
    assert n_profile == 1  # seul le champion 1 (35 ≥ 30) passe le seuil
    assert n_baseline == 1  # 1 ligne par rôle, pas de seuil

    cur = await pg_conn.execute(
        "SELECT champion_id, n, avg_gold_15, avg_dmg_per_gold"
        " FROM score_role_resource_profile"
        " WHERE window_label = '16.13' AND role = 'JUNGLE'"
    )
    rows = await cur.fetchall()
    assert rows == [(1, 35, pytest.approx(1000.0), pytest.approx(1.5))]

    cur = await pg_conn.execute(
        "SELECT n, avg_gold_15 FROM score_role_resource_baseline"
        " WHERE window_label = '16.13' AND role = 'JUNGLE'"
    )
    row = await cur.fetchone()
    # Moyenne des 45 games (35 à 1000 + 10 à 500) = (35*1000 + 10*500) / 45.
    assert row == (45, pytest.approx((35 * 1000 + 10 * 500) / 45))


async def test_refresh_deletes_previous_window_rows_before_reinserting(pg_conn):
    await _seed_role_games(
        pg_conn, patch="16.13", champion_id=1, role="JUNGLE", count=30, gold_15=1000
    )
    flex.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_games=30)

    # Un 2e appel sans nouvelle donnée (fenêtre inchangée) ne doit pas dupliquer les lignes.
    n_profile, _ = flex.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_games=30)
    assert n_profile == 1

    cur = await pg_conn.execute(
        "SELECT count(*) FROM score_role_resource_profile WHERE window_label = '16.13'"
    )
    row = await cur.fetchone()
    assert row == (1,)
