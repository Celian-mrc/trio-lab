"""Tests d'intégration de synergy/matchups.py (agg_matchup → score_matchup).

Counter 1v1 même rôle (migration 026) : delta = WR(champ_a vs champ_b, même
rôle) − WR baseline de champ_a dans ce rôle (agg_champion), lissé vers 0
comme synergy/compute.py — voir ce module pour la mécanique de lissage.
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import matchups, windows

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


async def _seed(conn, patch: str = "16.13", platform: str = "euw1") -> None:
    # Baseline JUNGLE champ 1 : WR .55 sur 100 games.
    await conn.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES (%s, %s, 'JUNGLE', 1, 100, 55)",
        (patch, platform),
    )
    # Matchup champ 1 vs champ 12 (même rôle) : WR .65 sur 40 games.
    await conn.execute(
        "INSERT INTO agg_matchup (patch, platform, role, champ_a, champ_b, games, wins)"
        " VALUES (%s, %s, 'JUNGLE', 1, 12, 40, 26)",
        (patch, platform),
    )


async def test_refresh_computes_smoothed_delta(pg_conn):
    await _seed(pg_conn)
    counts = matchups.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, k=200.0)
    # Vue « toutes régions » en plus de euw1 (une seule plateforme semée).
    assert counts == {"score_matchup": 2}

    cur = await pg_conn.execute(
        "SELECT games, games_eff, wr, delta_raw, delta, tier FROM score_matchup"
        " WHERE role = 'JUNGLE' AND champ_a = 1 AND champ_b = 12 AND platform = 'euw1'"
    )
    games, games_eff, wr, delta_raw, delta, tier = await cur.fetchone()
    assert (games, games_eff) == (40, pytest.approx(40.0))
    assert wr == pytest.approx(0.65)
    assert delta_raw == pytest.approx(0.10)  # .65 − .55
    # REAL (précision simple) : tolérance relâchée, cf. mémoire railway-deployment.
    assert delta == pytest.approx(40 * 0.10 / 240, rel=1e-4)  # lissage k=200
    assert tier == "faible"  # 40 games_eff < 50


async def test_refresh_skips_combo_without_baseline(pg_conn):
    """Un matchup dont le champ_a n'a pas de baseline agg_champion (rework
    hors fenêtre par ex.) est ignoré plutôt que planter."""
    await pg_conn.execute(
        "INSERT INTO agg_matchup (patch, platform, role, champ_a, champ_b, games, wins)"
        " VALUES ('16.13', 'euw1', 'JUNGLE', 99, 12, 10, 5)"
    )
    counts = matchups.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN)
    assert counts == {"score_matchup": 0}


async def test_refresh_is_idempotent_per_window(pg_conn):
    await _seed(pg_conn)
    window = windows.make_window(["16.13"])
    first = matchups.refresh(window, dsn=TEST_DSN)
    second = matchups.refresh(window, dsn=TEST_DSN)
    assert first == second
    cur = await pg_conn.execute("SELECT count(*) FROM score_matchup")
    assert (await cur.fetchone())[0] == 2
