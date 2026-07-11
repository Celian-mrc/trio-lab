"""Tests d'intégration des counters (agg_* semées → score_trio_vs_champion).

Valeurs attendues calculées à la main (voir commentaires). Gated sur
`TEST_DATABASE_URL` ; la fixture `pg_conn` tronque aussi les tables de la
migration 006 (TRUNCATE étendu dans le conftest).
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import counters, windows

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)

# Jeu de données : patch 16.13, euw1, trio (1, 2, 3) — 100 games, 55 wins
# (baseline .55). Face au jungler 64 : 20 games, 8 wins → wr .40,
# delta_raw = −.15, delta = 20×(−.15)/220 = −.013636 (k=200). Face au mid 91 :
# 10 games, 7 wins → wr .70, delta_raw = +.15, delta = 10×.15/210 = .007143.
_SEED_TRIO = (1, 2, 3, 100, 55)
_SEED_MATCHUPS = [
    ("JUNGLE", 64, 20, 8),
    ("MIDDLE", 91, 10, 7),
]


async def _seed(conn, patch: str = "16.13", platform: str = "euw1") -> None:
    jgl, mid, sup, games, wins = _SEED_TRIO
    await conn.execute(
        "INSERT INTO agg_trio (patch, platform, jgl_champion, mid_champion, sup_champion,"
        " games, wins) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (patch, platform, jgl, mid, sup, games, wins),
    )
    for role, enemy, vs_games, vs_wins in _SEED_MATCHUPS:
        await conn.execute(
            "INSERT INTO agg_trio_vs_champion (patch, platform, jgl_champion, mid_champion,"
            " sup_champion, enemy_role, enemy_champion, games, wins)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (patch, platform, jgl, mid, sup, role, enemy, vs_games, vs_wins),
        )


async def test_refresh_computes_expected_deltas(pg_conn):
    await _seed(pg_conn)
    counts = counters.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, k=200.0)
    # ×2 : chaque matchup est aussi matérialisé en vue 'all' (une seule
    # plateforme semée → mêmes valeurs sous platform='all').
    assert counts == {"score_trio_vs_champion": 4}

    cur = await pg_conn.execute(
        "SELECT games, games_eff, wr, delta_raw, delta, ci_low, ci_high, tier"
        " FROM score_trio_vs_champion WHERE enemy_champion = 64 AND platform = 'euw1'"
    )
    games, games_eff, wr, delta_raw, delta, ci_low, ci_high, tier = await cur.fetchone()
    assert (games, games_eff) == (20, pytest.approx(20.0))
    assert wr == pytest.approx(0.40)
    assert delta_raw == pytest.approx(-0.15)  # .40 − baseline .55
    assert delta == pytest.approx(20 * -0.15 / 220)  # rétréci vers 0
    assert 0.0 <= ci_low < 0.40 < ci_high <= 1.0
    assert tier == "faible"  # 20 games < 50

    cur = await pg_conn.execute(
        "SELECT delta_raw, delta FROM score_trio_vs_champion"
        " WHERE enemy_champion = 91 AND platform = 'euw1'"
    )
    delta_raw, delta = await cur.fetchone()
    assert delta_raw == pytest.approx(0.15)
    assert delta == pytest.approx(10 * 0.15 / 210)


async def test_refresh_weights_multi_patch_window(pg_conn):
    await _seed(pg_conn, patch="16.13")
    # Historique 16.12 : trio 100 games 45 wins, matchup vs 64 : 30 games 18 wins.
    await pg_conn.execute(
        "INSERT INTO agg_trio (patch, platform, jgl_champion, mid_champion, sup_champion,"
        " games, wins) VALUES ('16.12', 'euw1', 1, 2, 3, 100, 45)"
    )
    await pg_conn.execute(
        "INSERT INTO agg_trio_vs_champion (patch, platform, jgl_champion, mid_champion,"
        " sup_champion, enemy_role, enemy_champion, games, wins)"
        " VALUES ('16.12', 'euw1', 1, 2, 3, 'JUNGLE', 64, 30, 18)"
    )
    counters.refresh(windows.make_window(["16.13", "16.12"]), dsn=TEST_DSN)

    cur = await pg_conn.execute(
        "SELECT games, games_eff, wr, delta_raw FROM score_trio_vs_champion"
        " WHERE enemy_champion = 64 AND platform = 'euw1'"
    )
    games, games_eff, wr, delta_raw = await cur.fetchone()
    assert games == 50
    assert games_eff == pytest.approx(38.0)  # 20×1.0 + 30×0.6
    assert wr == pytest.approx((8 + 18 * 0.6) / 38)  # .494737
    # Baseline pondérée sur la MÊME fenêtre : (55 + 45×0.6) / 160 = .512500.
    assert delta_raw == pytest.approx((8 + 18 * 0.6) / 38 - (55 + 45 * 0.6) / 160)
    cur = await pg_conn.execute("SELECT DISTINCT window_label FROM score_trio_vs_champion")
    assert await cur.fetchall() == [("16.13+16.12",)]


async def test_refresh_is_idempotent_per_window(pg_conn):
    await _seed(pg_conn)
    window = windows.make_window(["16.13"])
    first = counters.refresh(window, dsn=TEST_DSN)
    second = counters.refresh(window, dsn=TEST_DSN)
    assert first == second
    cur = await pg_conn.execute("SELECT count(*) FROM score_trio_vs_champion")
    assert (await cur.fetchone())[0] == 4  # 2 matchups × (euw1 + 'all')


async def test_enemy_rework_cuts_the_matchup(pg_conn, monkeypatch):
    """Un matchup dont toutes les données précèdent le rework de L'ENNEMI tombe."""
    await _seed(pg_conn, patch="16.12")
    monkeypatch.setattr(windows, "REWORKS", {64: "16.13"})  # l'ennemi 64 retravaillé
    counts = counters.refresh(windows.make_window(["16.13", "16.12"]), dsn=TEST_DSN)
    # Le matchup vs 64 tombe (poids nuls) ; vs 91 survit (euw1 + 'all').
    assert counts == {"score_trio_vs_champion": 2}
    cur = await pg_conn.execute("SELECT DISTINCT enemy_champion FROM score_trio_vs_champion")
    assert await cur.fetchall() == [(91,)]
