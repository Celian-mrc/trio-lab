"""Tests d'intégration de synergy/win_factors.py (régression logistique IRLS).

Jeux de données synthétiques avec une relation CONNUE entre `gold_diff_15` et
`win` (bruitée pour rester bien conditionnée, pas parfaitement séparable) —
les autres features sont tenues CONSTANTES : après standardisation, leur
colonne devient nulle et leur coefficient doit rester ~0 (aucune information).
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import win_factors, windows

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)

_CONSTANT = {
    "herald_taken": False,
    "soul_taken": False,
    "first_tower": False,
    "kill_participation_pre15": 0.5,
    "damage_share": 0.4,
    "vision_score": 60,
    "cc_time_s": 90,
    "jgl_dmg_per_gold": 1.5,
    "mid_dmg_per_gold": 1.8,
    "sup_dmg_per_gold": 1.2,
}


async def _seed_rows(conn, patch: str, rows: list[tuple[int, bool]]) -> None:
    """`rows` : (gold_diff_15, win) — le reste tient de `_CONSTANT`."""
    for i, (gold_diff_15, win) in enumerate(rows):
        match_id = f"{patch}_{i}"
        await conn.execute(
            "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
            " game_creation, game_duration_s, winning_team)"
            " VALUES (%s, 'euw1', %s, %s, 420, now(), 1800, 100)",
            (match_id, patch, f"{patch}.1"),
        )
        await conn.execute(
            "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
            " sup_champion, win, gold_diff_15, herald_taken, soul_taken, first_tower,"
            " kill_participation_pre15, damage_share, vision_score, cc_time_s,"
            " jgl_dmg_per_gold, mid_dmg_per_gold, sup_dmg_per_gold)"
            " VALUES (%s, 100, 1, 2, 3, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                match_id,
                win,
                gold_diff_15,
                _CONSTANT["herald_taken"],
                _CONSTANT["soul_taken"],
                _CONSTANT["first_tower"],
                _CONSTANT["kill_participation_pre15"],
                _CONSTANT["damage_share"],
                _CONSTANT["vision_score"],
                _CONSTANT["cc_time_s"],
                _CONSTANT["jgl_dmg_per_gold"],
                _CONSTANT["mid_dmg_per_gold"],
                _CONSTANT["sup_dmg_per_gold"],
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


async def test_refresh_fits_signal_and_ignores_constant_features(pg_conn):
    await _seed_rows(pg_conn, "16.13", _separable_with_noise())
    counts = win_factors.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_rows=50)
    assert counts == {
        "all": len(win_factors.FEATURES) + 1,
        "behind_gold15": len(win_factors.FEATURES) + 1,
    }

    cur = await pg_conn.execute(
        "SELECT coef, odds_ratio, n FROM score_win_factors"
        " WHERE window_label = '16.13' AND population = 'all' AND feature = 'gold_diff_15'"
    )
    coef, odds_ratio, n = await cur.fetchone()
    assert n == 100
    assert coef > 1.0  # signal net (80/100 correctement séparé par le signe)
    assert odds_ratio > 2.0

    # Features tenues constantes dans le jeu de données : aucune information,
    # coefficient proche de 0 (colonne nulle après standardisation + ridge).
    cur = await pg_conn.execute(
        "SELECT feature, coef FROM score_win_factors"
        " WHERE window_label = '16.13' AND population = 'all'"
        " AND feature IN ('cc_per_min', 'damage_share', 'herald_taken')"
    )
    for feature, coef in await cur.fetchall():
        assert abs(coef) < 1e-3, feature


async def test_refresh_weights_patches_by_window(pg_conn):
    """16.13 (poids 1.0) : gold_diff_15 positif → win. 16.12 (poids 0.6) :
    relation inversée. Le coefficient net doit suivre le patch le plus
    lourd (16.13), pas s'annuler."""
    await _seed_rows(pg_conn, "16.13", _separable_with_noise(flip=False))
    await _seed_rows(pg_conn, "16.12", _separable_with_noise(flip=True))
    win_factors.refresh(windows.make_window(["16.13", "16.12"]), dsn=TEST_DSN, min_rows=50)

    cur = await pg_conn.execute(
        "SELECT coef FROM score_win_factors"
        " WHERE window_label = '16.13+16.12' AND population = 'all' AND feature = 'gold_diff_15'"
    )
    (coef,) = await cur.fetchone()
    assert coef > 0  # le patch le plus lourd (16.13, poids 1.0) l'emporte


async def test_refresh_skips_population_below_min_rows(pg_conn):
    await _seed_rows(pg_conn, "16.13", _separable_with_noise())
    counts = win_factors.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, min_rows=1000)
    assert counts == {"all": 0, "behind_gold15": 0}
    cur = await pg_conn.execute("SELECT count(*) FROM score_win_factors")
    assert (await cur.fetchone())[0] == 0
