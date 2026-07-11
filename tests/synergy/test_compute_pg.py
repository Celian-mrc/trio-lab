"""Tests d'intégration du pipeline de scores (agg_* semées → score_duo/score_trio).

Valeurs attendues calculées à la main (voir commentaires). Gated sur
`TEST_DATABASE_URL` ; la fixture `pg_conn` tronque aussi les tables de scores
via la migration 004 (TRUNCATE étendu dans le conftest).
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import compute, windows

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)

# Jeu de données : patch 16.13, euw1. WR indiv : jgl(1)=.60, mid(2)=.50, sup(3)=.40
# (moyenne .50). Duos : jgl_mid .60 (syn +.05 vs base .55), jgl_sup .50 (syn 0),
# mid_sup .45 (syn 0). Le prior trio utilise les synergies de duo RÉTRÉCIES
# vers 0 (k=200) : jgl_mid → 40×.05/240 = .008333, autres 0 → pred = .002778.
# Trio 10 games wr .70 → raw = .20 ; lissé = (10×.2 + 200×.002778)/210 = .012169.
_SEED_CHAMPIONS = [
    ("JUNGLE", 1, 100, 60),
    ("MIDDLE", 2, 100, 50),
    ("UTILITY", 3, 100, 40),
]
_SEED_DUOS = [
    ("jgl_mid", 1, 2, 40, 24),
    ("jgl_sup", 1, 3, 40, 20),
    ("mid_sup", 2, 3, 40, 18),
]
_SEED_TRIO = (1, 2, 3, 10, 7)


async def _seed(conn, patch: str = "16.13", platform: str = "euw1") -> None:
    for role, champ, games, wins in _SEED_CHAMPIONS:
        await conn.execute(
            "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (patch, platform, role, champ, games, wins),
        )
    for roles, a, b, games, wins in _SEED_DUOS:
        await conn.execute(
            "INSERT INTO agg_duo (patch, platform, roles, champ_a, champ_b, games, wins)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (patch, platform, roles, a, b, games, wins),
        )
    jgl, mid, sup, games, wins = _SEED_TRIO
    await conn.execute(
        "INSERT INTO agg_trio (patch, platform, jgl_champion, mid_champion, sup_champion,"
        " games, wins) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (patch, platform, jgl, mid, sup, games, wins),
    )


async def test_refresh_computes_expected_scores(pg_conn):
    await _seed(pg_conn)
    counts = compute.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN, k=200.0)
    # Chaque combinaison est aussi matérialisée en vue « toutes régions »
    # (platform='all') — une seule plateforme semée, donc le double exact.
    assert counts == {"score_duo": 6, "score_trio": 2}

    cur = await pg_conn.execute(
        "SELECT wr, synergy, tier FROM score_duo WHERE roles = 'jgl_mid' AND platform = 'euw1'"
    )
    wr, synergy, tier = await cur.fetchone()
    assert wr == pytest.approx(0.6)
    assert synergy == pytest.approx(0.05)  # .60 − (.60+.50)/2
    assert tier == "faible"  # 40 games < 50

    cur = await pg_conn.execute(
        "SELECT games, games_eff, wr, synergy_raw, synergy_pred, synergy, ci_low, ci_high"
        " FROM score_trio WHERE platform = 'euw1'"
    )
    games, games_eff, wr, raw, pred, smoothed, ci_low, ci_high = await cur.fetchone()
    assert (games, games_eff) == (10, pytest.approx(10.0))
    assert wr == pytest.approx(0.7)
    assert raw == pytest.approx(0.2)  # .70 − moyenne(.60, .50, .40)
    shrunk_jgl_mid = 40 * 0.05 / 240  # synergie duo rétrécie vers 0 (prior neutre)
    assert pred == pytest.approx(shrunk_jgl_mid / 3)
    assert smoothed == pytest.approx((10 * 0.2 + 200 * shrunk_jgl_mid / 3) / 210)
    assert 0.0 <= ci_low < 0.7 < ci_high <= 1.0


async def test_all_platforms_view_combines_and_averages_stats(pg_conn):
    """La vue 'all' somme les agrégats entre plateformes ; les stats sont moyennées."""
    await _seed(pg_conn, platform="euw1")
    await _seed(pg_conn, platform="kr")
    # Stats matérialisées (007) sur le trio : euw1 gold@10 +800 sur 8 games
    # renseignées, kr +200 sur 2 ; vision seulement côté euw1.
    await pg_conn.execute(
        "UPDATE agg_trio SET gold10_sum = 6400, gold10_n = 8, vision_sum = 1500, vision_n = 10"
        " WHERE platform = 'euw1'"
    )
    await pg_conn.execute(
        "UPDATE agg_trio SET gold10_sum = 400, gold10_n = 2 WHERE platform = 'kr'"
    )
    compute.refresh(windows.make_window(["16.13"]), dsn=TEST_DSN)

    cur = await pg_conn.execute(
        "SELECT games, wr, gold_diff_10, vision_score, drakes FROM score_trio"
        " WHERE platform = 'all'"
    )
    games, wr, gold10, vision, drakes = await cur.fetchone()
    assert games == 20  # 10 euw1 + 10 kr
    assert wr == pytest.approx(0.7)  # 7 + 7 wins
    assert gold10 == pytest.approx((6400 + 400) / (8 + 2))  # 680
    assert vision == pytest.approx(150.0)  # seul euw1 renseigne : 1500/10
    assert drakes is None  # aucune donnée nulle part

    # La vue régionale reste intacte à côté de la vue combinée.
    cur = await pg_conn.execute("SELECT gold_diff_10 FROM score_trio WHERE platform = 'euw1'")
    assert (await cur.fetchone())[0] == pytest.approx(800.0)


async def test_refresh_weights_multi_patch_window(pg_conn):
    await _seed(pg_conn, patch="16.13")
    # Le duo jgl_mid a aussi un historique 16.12 moins bon, pondéré 0.6.
    await pg_conn.execute(
        "INSERT INTO agg_duo (patch, platform, roles, champ_a, champ_b, games, wins)"
        " VALUES ('16.12', 'euw1', 'jgl_mid', 1, 2, 100, 40)"
    )
    for role, champ, games, wins in _SEED_CHAMPIONS:
        await pg_conn.execute(
            "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
            " VALUES ('16.12', 'euw1', %s, %s, %s, %s)",
            (role, champ, games, wins),
        )
    compute.refresh(windows.make_window(["16.13", "16.12"]), dsn=TEST_DSN)

    cur = await pg_conn.execute(
        "SELECT games, games_eff, wr FROM score_duo WHERE roles = 'jgl_mid' AND platform = 'euw1'"
    )
    games, games_eff, wr = await cur.fetchone()
    assert games == 140
    assert games_eff == pytest.approx(100.0)  # 40×1.0 + 100×0.6
    assert wr == pytest.approx(0.48)  # (24 + 40×0.6) / 100
    # La fenêtre est matérialisée sous son étiquette propre.
    cur = await pg_conn.execute("SELECT DISTINCT window_label FROM score_duo")
    assert await cur.fetchall() == [("16.13+16.12",)]


async def test_refresh_is_idempotent_per_window(pg_conn):
    await _seed(pg_conn)
    window = windows.make_window(["16.13"])
    first = compute.refresh(window, dsn=TEST_DSN)
    second = compute.refresh(window, dsn=TEST_DSN)
    assert first == second
    cur = await pg_conn.execute("SELECT count(*) FROM score_trio")
    assert (await cur.fetchone())[0] == 2  # euw1 + 'all'


async def test_rework_cut_drops_combos_without_effective_games(pg_conn, monkeypatch):
    """Un trio dont toutes les données précèdent le rework d'un membre n'est pas scoré."""
    await _seed(pg_conn, patch="16.12")
    monkeypatch.setattr(windows, "REWORKS", {1: "16.13"})  # jungler retravaillé au 16.13
    counts = compute.refresh(windows.make_window(["16.13", "16.12"]), dsn=TEST_DSN)
    # Les combos impliquant le champion 1 (2 duos + le trio) tombent ; mid_sup
    # survit (en euw1 et dans la vue 'all').
    assert counts == {"score_duo": 2, "score_trio": 0}
    cur = await pg_conn.execute("SELECT DISTINCT roles FROM score_duo")
    assert await cur.fetchall() == [("mid_sup",)]
