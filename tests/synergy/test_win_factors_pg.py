"""Tests d'intégration de synergy/win_factors.py (régression logistique IRLS).

Jeux de données synthétiques avec une relation CONNUE entre le gold diff
d'ÉQUIPE (5 rôles, via match_role_stats) et `win` (bruitée pour rester bien
conditionnée, pas parfaitement séparable) — les autres features sont tenues
CONSTANTES : après standardisation, leur colonne devient nulle et leur
coefficient doit rester ~0 (aucune information).
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import win_factors, windows

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)

_ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
_BASE_GOLD_15 = 1000  # par rôle ; la somme des 5 donne le gold d'équipe
_CONSTANT_TRIO = {
    "herald_taken": False,
    "soul_taken": False,
    "first_tower": False,
    "jgl_cs_diff_15": 3,
}
_CONSTANT_ROLE = {
    "cc_time_s": 18,
    "vision_score": 12,
    "dmg_per_gold": 1.5,
}  # identique pour les 2 équipes


async def _seed_match(conn, match_id: str, patch: str, *, gold_diff: int, win: bool) -> None:
    """Une game : équipe 100 gagne `gold_diff` de plus que l'équipe 200 (réparti
    sur les 5 rôles), `win` = résultat de l'équipe 100. Tout le reste constant."""
    await conn.execute(
        "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
        " game_creation, game_duration_s, winning_team)"
        " VALUES (%s, 'euw1', %s, %s, 420, now(), 1800, %s)",
        (match_id, patch, f"{patch}.1", 100 if win else 200),
    )
    await conn.execute(
        "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
        " sup_champion, win, herald_taken, soul_taken, first_tower, jgl_cs_diff_15)"
        " VALUES (%s, 100, 1, 2, 3, %s, %s, %s, %s, %s),"
        "        (%s, 200, 11, 12, 13, %s, %s, %s, %s, %s)",
        (
            match_id,
            win,
            _CONSTANT_TRIO["herald_taken"],
            _CONSTANT_TRIO["soul_taken"],
            _CONSTANT_TRIO["first_tower"],
            _CONSTANT_TRIO["jgl_cs_diff_15"],
            match_id,
            not win,
            _CONSTANT_TRIO["herald_taken"],
            _CONSTANT_TRIO["soul_taken"],
            _CONSTANT_TRIO["first_tower"],
            _CONSTANT_TRIO["jgl_cs_diff_15"],
        ),
    )
    per_role_delta = gold_diff // 5
    for team_id, champ_base, gold in (
        (100, 1, _BASE_GOLD_15 + per_role_delta),
        (200, 11, _BASE_GOLD_15),
    ):
        for i, role in enumerate(_ROLES):
            await conn.execute(
                "INSERT INTO match_role_stats (match_id, team_id, role, champion_id, win,"
                " gold_15, cc_time_s, vision_score, dmg_per_gold)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    match_id,
                    team_id,
                    role,
                    champ_base + i,
                    win if team_id == 100 else not win,
                    gold,
                    _CONSTANT_ROLE["cc_time_s"],
                    _CONSTANT_ROLE["vision_score"],
                    _CONSTANT_ROLE["dmg_per_gold"],
                ),
            )


def _separable_with_noise(*, flip: bool = False) -> list[tuple[int, bool]]:
    """40 games +1000/win, 40 −1000/loss, 10+10 de bruit (chevauchement) —
    signal net mais pas parfaitement séparable (IRLS bien conditionné).
    `flip` inverse la relation (pour tester la pondération par patch)."""
    positive_win = not flip
    rows = [(1000, positive_win)] * 40 + [(-1000, not positive_win)] * 40
    rows += [(1000, not positive_win)] * 10 + [(-1000, positive_win)] * 10
    return rows


async def _seed_rows(conn, patch: str, rows: list[tuple[int, bool]]) -> None:
    for i, (gold_diff, win) in enumerate(rows):
        await _seed_match(conn, f"{patch}_{i}", patch, gold_diff=gold_diff, win=win)


async def test_refresh_fits_signal_and_ignores_constant_features(pg_conn):
    await _seed_rows(pg_conn, "16.13", _separable_with_noise())
    counts = win_factors.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_rows=50)
    assert counts == {
        "all": len(win_factors.FEATURES) + 1,
        "behind_gold15": len(win_factors.FEATURES) + 1,
    }

    cur = await pg_conn.execute(
        "SELECT coef, odds_ratio, n FROM score_win_factors"
        " WHERE window_label = '16.13' AND population = 'all' AND feature = 'team_gold_diff_15'"
    )
    coef, odds_ratio, n = await cur.fetchone()
    assert n == 200  # 2 perspectives (une par équipe) × 100 games semées
    assert coef > 1.0  # signal net (80 % des games correctement séparées par le signe)
    assert odds_ratio > 2.0

    # Features tenues constantes dans le jeu de données (dmg/gold identique
    # sur les 5 rôles, les 2 équipes) : aucune information, coefficient
    # proche de 0 (colonne nulle après standardisation + ridge).
    cur = await pg_conn.execute(
        "SELECT feature, coef FROM score_win_factors"
        " WHERE window_label = '16.13' AND population = 'all'"
        " AND feature IN ('team_cc_per_min', 'top_dmg_per_gold', 'herald_taken', 'jgl_cs_diff_15')"
    )
    for feature, coef in await cur.fetchall():
        assert abs(coef) < 1e-3, feature


async def test_refresh_weights_patches_by_window(pg_conn):
    """16.13 (poids 1.0) : gold d'équipe positif → win. 16.12 (poids 0.6) :
    relation inversée. Le coefficient net doit suivre le patch le plus
    lourd (16.13), pas s'annuler."""
    await _seed_rows(pg_conn, "16.13", _separable_with_noise(flip=False))
    await _seed_rows(pg_conn, "16.12", _separable_with_noise(flip=True))
    win_factors.refresh(windows.make_window(["16.13", "16.12"]), dsn=TEST_DSN, min_rows=50)

    cur = await pg_conn.execute(
        "SELECT coef FROM score_win_factors WHERE window_label = '16.13+16.12'"
        " AND population = 'all' AND feature = 'team_gold_diff_15'"
    )
    (coef,) = await cur.fetchone()
    assert coef > 0  # le patch le plus lourd (16.13, poids 1.0) l'emporte


async def test_refresh_skips_population_below_min_rows(pg_conn):
    await _seed_rows(pg_conn, "16.13", _separable_with_noise())
    counts = win_factors.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_rows=1000)
    assert counts == {"all": 0, "behind_gold15": 0}
    cur = await pg_conn.execute("SELECT count(*) FROM score_win_factors")
    assert (await cur.fetchone())[0] == 0
