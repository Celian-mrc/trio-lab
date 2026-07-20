"""Tests d'intégration de synergy/gold_factors.py (OLS pondéré 2 blocs).

Jeu de données synthétique avec une relation CONNUE entre le draft (WR
baseline des picks) et `gold_diff_15` : 40 games "bleu fort" (bleu pose les
5 picks à 60 % WR, rouge les 5 à 45 %, bleu finit +1000 gold) + 10 de bruit
(inversé — bleu pose les picks FAIBLES, finit −1000). Le reste tenu
CONSTANT ou sans signal :
- matchup/synergie : aucune ligne score_matchup/score_trio semée → COALESCE
  à 0 pour tout le monde, donc aucune information.
- jgl_cs_diff_15 : valeur identique partout (continue → standardisée à 0).
- first_blood_team : alterne indépendamment du groupe fort/faible (variance
  réelle, mais sans corrélation avec le gold) — contrairement à une valeur
  parfaitement constante, qui serait colinéaire avec l'intercept (booléenne,
  jamais standardisée).
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import gold_factors, windows

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)

_ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
_STRONG_CHAMPS = (1, 2, 3, 4, 5)  # WR baseline 60 %
_WEAK_CHAMPS = (21, 22, 23, 24, 25)  # WR baseline 45 %
_BASE_GOLD_15 = 1000
_CONSTANT_JGL_CS_DIFF = 3


async def _seed_baselines(conn, patch: str) -> None:
    for champs, wr in ((_STRONG_CHAMPS, 0.60), (_WEAK_CHAMPS, 0.45)):
        for i, role in enumerate(_ROLES):
            games = 1000
            await conn.execute(
                "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
                " VALUES (%s, 'euw1', %s, %s, %s, %s)",
                (patch, role, champs[i], games, int(games * wr)),
            )


async def _seed_match(
    conn, match_id: str, patch: str, *, blue_strong: bool, gold_diff: int, first_blood_blue: bool
) -> None:
    blue = _STRONG_CHAMPS if blue_strong else _WEAK_CHAMPS
    red = _WEAK_CHAMPS if blue_strong else _STRONG_CHAMPS
    await conn.execute(
        "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
        " game_creation, game_duration_s, winning_team)"
        " VALUES (%s, 'euw1', %s, %s, 420, now(), 1800, 100)",
        (match_id, patch, f"{patch}.1"),
    )
    await conn.execute(
        "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
        " sup_champion, win, herald_taken, soul_taken, first_tower, jgl_cs_diff_15)"
        " VALUES (%s, 100, %s, %s, %s, true, false, false, false, %s),"
        "        (%s, 200, %s, %s, %s, false, false, false, false, %s)",
        (
            match_id,
            blue[1],
            blue[2],
            blue[4],
            _CONSTANT_JGL_CS_DIFF,
            match_id,
            red[1],
            red[2],
            red[4],
            _CONSTANT_JGL_CS_DIFF,
        ),
    )
    per_role_delta = gold_diff // 5
    for team_id, champs, gold, first_blood in (
        (100, blue, _BASE_GOLD_15 + per_role_delta, first_blood_blue),
        (200, red, _BASE_GOLD_15, not first_blood_blue),
    ):
        for i, role in enumerate(_ROLES):
            await conn.execute(
                "INSERT INTO match_role_stats (match_id, team_id, role, champion_id, win,"
                " gold_15, first_blood)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (match_id, team_id, role, champs[i], team_id == 100, gold, first_blood),
            )


async def _seed_rows(conn, patch: str) -> None:
    await _seed_baselines(conn, patch)
    i = 0
    for _ in range(40):
        await _seed_match(
            conn,
            f"{patch}_{i}",
            patch,
            blue_strong=True,
            gold_diff=1000,
            first_blood_blue=(i % 2 == 0),
        )
        i += 1
    for _ in range(10):
        await _seed_match(
            conn,
            f"{patch}_{i}",
            patch,
            blue_strong=False,
            gold_diff=-1000,
            first_blood_blue=(i % 2 == 0),
        )
        i += 1


async def test_refresh_fits_draft_signal_and_reports_r2_blocks(pg_conn):
    await _seed_rows(pg_conn, "16.13")
    count = gold_factors.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_rows=50)
    assert count == len(gold_factors.FEATURES) + 1 + 2  # +intercept +2 lignes _r2_*

    cur = await pg_conn.execute(
        "SELECT coef, n FROM score_gold_factors"
        " WHERE window_label = '16.13' AND feature = 'team_baseline_wr'"
    )
    coef, n = await cur.fetchone()
    assert n == 100  # 2 perspectives × 50 games semées
    assert coef > 0  # WR baseline plus haut → plus de gold, signal net

    # Features sans information (matchup/synergie jamais semées → COALESCE
    # à 0 partout, jgl_cs_diff_15 constant) : coefficient proche de 0.
    cur = await pg_conn.execute(
        "SELECT feature, coef FROM score_gold_factors"
        " WHERE window_label = '16.13'"
        " AND feature IN ('team_matchup_delta', 'team_trio_synergy', 'jgl_cs_diff_15')"
    )
    for feature, coef in await cur.fetchall():
        assert abs(coef) < 1e-3, feature

    # R²(draft seul) et R²(complet) : le bloc exécution n'ajoute quasi rien
    # (jgl_cs_diff_15 constant, first_blood_team sans corrélation au signal).
    cur = await pg_conn.execute(
        "SELECT feature, coef FROM score_gold_factors"
        " WHERE window_label = '16.13' AND feature IN ('_r2_draft_only', '_r2_full')"
    )
    r2 = dict(await cur.fetchall())
    assert r2["_r2_draft_only"] > 0.5  # le draft explique l'essentiel du signal
    assert r2["_r2_full"] >= r2["_r2_draft_only"] - 1e-6
    assert r2["_r2_full"] - r2["_r2_draft_only"] < 0.05  # peu d'apport du bloc exécution


async def test_refresh_skips_window_below_min_rows(pg_conn):
    await _seed_rows(pg_conn, "16.13")
    count = gold_factors.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_rows=1000)
    assert count == 0
    cur = await pg_conn.execute("SELECT count(*) FROM score_gold_factors")
    assert (await cur.fetchone())[0] == 0
