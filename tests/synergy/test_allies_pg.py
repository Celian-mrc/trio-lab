"""Tests d'intégration des meilleurs alliés (agg_* semées → score_trio_with_ally).

Miroir de `test_counters_pg.py`, côté allié. Gated sur `TEST_DATABASE_URL` ;
la fixture `pg_conn` tronque aussi les tables de la migration 014.
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import allies, windows

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)

# Jeu de données : patch 16.13, euw1, trio (1, 2, 3) — 100 games, 55 wins
# (baseline .55). Avec le Top 40 : 20 games, 12 wins → wr .60,
# uplift_raw = +.05, uplift = 20×.05/220 = .004545 (k=200). Avec l'ADC 77 :
# 10 games, 3 wins → wr .30, uplift_raw = −.25, uplift = 10×−.25/210 = −.011905.
_SEED_TRIO = (1, 2, 3, 100, 55)
_SEED_ALLIES = [
    ("TOP", 40, 20, 12),
    ("BOTTOM", 77, 10, 3),
]


async def _seed(conn, patch: str = "16.13", platform: str = "euw1") -> None:
    jgl, mid, sup, games, wins = _SEED_TRIO
    await conn.execute(
        "INSERT INTO agg_trio (patch, platform, jgl_champion, mid_champion, sup_champion,"
        " games, wins) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (patch, platform, jgl, mid, sup, games, wins),
    )
    for role, ally, ally_games, ally_wins in _SEED_ALLIES:
        await conn.execute(
            "INSERT INTO agg_trio_with_ally (patch, platform, jgl_champion, mid_champion,"
            " sup_champion, ally_role, ally_champion, games, wins)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (patch, platform, jgl, mid, sup, role, ally, ally_games, ally_wins),
        )


async def test_refresh_computes_expected_uplifts(pg_conn):
    await _seed(pg_conn)
    counts = allies.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, k=200.0)
    # ×2 : chaque allié est aussi matérialisé en vue 'all'.
    assert counts == {"score_trio_with_ally": 4}

    cur = await pg_conn.execute(
        "SELECT games, games_eff, wr, uplift_raw, uplift, ci_low, ci_high, tier"
        " FROM score_trio_with_ally WHERE ally_champion = 40 AND platform = 'euw1'"
    )
    games, games_eff, wr, uplift_raw, uplift, ci_low, ci_high, tier = await cur.fetchone()
    assert (games, games_eff) == (20, pytest.approx(20.0))
    assert wr == pytest.approx(0.60)
    assert uplift_raw == pytest.approx(0.05)  # .60 − baseline .55
    assert uplift == pytest.approx(20 * 0.05 / 220, rel=1e-4)
    assert 0.0 <= ci_low < 0.60 < ci_high <= 1.0
    assert tier == "faible"  # 20 games < 50

    cur = await pg_conn.execute(
        "SELECT uplift_raw, uplift FROM score_trio_with_ally"
        " WHERE ally_champion = 77 AND platform = 'euw1'"
    )
    uplift_raw, uplift = await cur.fetchone()
    assert uplift_raw == pytest.approx(-0.25)
    # Tolérance relâchée (rel=1e-4) : colonne REAL (simple précision), cf.
    # commentaire équivalent dans test_counters_pg.py.
    assert uplift == pytest.approx(10 * -0.25 / 210, rel=1e-4)


async def test_refresh_weights_multi_patch_window(pg_conn):
    await _seed(pg_conn, patch="16.13")
    # Historique 16.12 : trio 100 games 45 wins, avec le Top 40 : 30 games 21 wins.
    await pg_conn.execute(
        "INSERT INTO agg_trio (patch, platform, jgl_champion, mid_champion, sup_champion,"
        " games, wins) VALUES ('16.12', 'euw1', 1, 2, 3, 100, 45)"
    )
    await pg_conn.execute(
        "INSERT INTO agg_trio_with_ally (patch, platform, jgl_champion, mid_champion,"
        " sup_champion, ally_role, ally_champion, games, wins)"
        " VALUES ('16.12', 'euw1', 1, 2, 3, 'TOP', 40, 30, 21)"
    )
    allies.refresh(windows.make_window(["16.13", "16.12"]), dsn=TEST_DSN)

    cur = await pg_conn.execute(
        "SELECT games, games_eff, wr, uplift_raw FROM score_trio_with_ally"
        " WHERE ally_champion = 40 AND platform = 'euw1'"
    )
    games, games_eff, wr, uplift_raw = await cur.fetchone()
    assert games == 50
    assert games_eff == pytest.approx(38.0)  # 20×1.0 + 30×0.6
    assert wr == pytest.approx((12 + 21 * 0.6) / 38)
    # Baseline pondérée sur la MÊME fenêtre : (55 + 45×0.6) / 160 = .512500.
    assert uplift_raw == pytest.approx((12 + 21 * 0.6) / 38 - (55 + 45 * 0.6) / 160, rel=1e-4)
    cur = await pg_conn.execute("SELECT DISTINCT window_label FROM score_trio_with_ally")
    assert await cur.fetchall() == [("16.13+16.12",)]


async def test_refresh_is_idempotent_per_window(pg_conn):
    await _seed(pg_conn)
    window = windows.make_window(["16.13"])
    first = allies.refresh(window, dsn=TEST_DSN)
    second = allies.refresh(window, dsn=TEST_DSN)
    assert first == second
    cur = await pg_conn.execute("SELECT count(*) FROM score_trio_with_ally")
    assert (await cur.fetchone())[0] == 4  # 2 alliés × (euw1 + 'all')


async def test_ally_rework_cuts_the_pairing(pg_conn, monkeypatch):
    """Une paire dont toutes les données précèdent le rework de L'ALLIÉ tombe."""
    await _seed(pg_conn, patch="16.12")
    monkeypatch.setattr(windows, "REWORKS", {40: "16.13"})  # l'allié 40 retravaillé
    counts = allies.refresh(windows.make_window(["16.13", "16.12"]), dsn=TEST_DSN)
    # La paire avec 40 tombe (poids nuls) ; avec 77 survit (euw1 + 'all').
    assert counts == {"score_trio_with_ally": 2}
    cur = await pg_conn.execute("SELECT DISTINCT ally_champion FROM score_trio_with_ally")
    assert await cur.fetchall() == [(77,)]
