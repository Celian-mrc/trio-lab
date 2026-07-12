"""Tests d'intégration de l'interface (TestClient FastAPI sur le Postgres de test).

Tests SYNCHRONES : le TestClient de Starlette gère sa propre boucle et ne doit
pas être appelé depuis un test async — le seeding passe par une connexion
psycopg sync locale. L'index champion est injecté : aucun appel Data Dragon.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from trio_lab import db
from trio_lab.web.app import create_app
from trio_lab.web.champions import Champion

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)

_INDEX = {
    1: Champion(1, "Lee Sin", ""),
    2: Champion(2, "Ahri", ""),
    3: Champion(3, "Thresh", ""),
    4: Champion(4, "Vi", ""),
    5: Champion(5, "Orianna", ""),
    6: Champion(6, "Leona", ""),
    44: Champion(44, "Renekton", ""),
    64: Champion(64, "Nocturne", ""),
}


@pytest.fixture
def pg_sync():
    """Connexion sync au Postgres de test, migrations appliquées, tables tronquées."""
    db.apply_migrations(TEST_DSN)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE players, matches, match_fetch_journal,"
            " agg_champion, agg_duo, agg_trio, agg_trio_vs_champion, agg_trio_with_ally,"
            " agg_trio_duration, agg_duo_duration,"
            " score_duo, score_trio, score_trio_vs_champion, score_trio_with_ally,"
            " champion_cc_theoretical CASCADE"
        )
        yield conn


@pytest.fixture
def client():
    app = create_app(dsn=TEST_DSN, champion_index=_INDEX)
    with TestClient(app) as test_client:
        yield test_client


def _seed_scores(conn) -> None:
    """Deux trios scorés sur euw1/16.13 : (1,2,3) synergie +.05 (+ CC matérialisé,
    valeurs arbitraires cohérentes utilisées telles quelles par la page détail,
    jamais recalculées), (4,5,6) −.02 (CC non matérialisé, teste le chemin None)."""
    rows = (
        (1, 2, 3, 40, 0.60, 0.05, 42.0, 50.0, 43.7, 0.015),
        (4, 5, 6, 80, 0.48, -0.02, None, None, None, None),
    )
    for jgl, mid, sup, games, wr, syn, cc_theo, cc_emp, cc_blend, scaling in rows:
        conn.execute(
            "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
            " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
            " ci_low, ci_high, tier, cc_theoretical_pct, cc_empirical_pct, cc_blended_pct,"
            " scaling)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, %s, %s, %s,"
            " %s, 0.0, %s, 0.3, 0.8, 'faible', %s, %s, %s, %s)",
            (jgl, mid, sup, games, float(games), wr, syn, syn, cc_theo, cc_emp, cc_blend, scaling),
        )
    conn.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier,"
        " cc_theoretical_pct, cc_empirical_pct, cc_blended_pct, scaling)"
        " VALUES ('16.13', 'euw1', 'jgl_mid', 1, 2, 60, 60.0, 0.58, 0.03, 0.4, 0.7, 'moyen',"
        " 37.5, 45.0, 40.2, -0.01)"
    )
    conn.execute(
        "INSERT INTO score_trio_vs_champion (window_label, platform, jgl_champion,"
        " mid_champion, sup_champion, enemy_role, enemy_champion, games, games_eff, wr,"
        " delta_raw, delta, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 1, 2, 3, 'JUNGLE', 64, 12, 12.0, 0.35,"
        " -0.25, -0.014, 0.1, 0.6, 'faible')"
    )
    conn.execute(
        "INSERT INTO score_trio_with_ally (window_label, platform, jgl_champion,"
        " mid_champion, sup_champion, ally_role, ally_champion, games, games_eff, wr,"
        " uplift_raw, uplift, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 1, 2, 3, 'TOP', 44, 15, 15.0, 0.70,"
        " 0.10, 0.045, 0.4, 0.9, 'faible')"
    )


def _seed_matches(conn) -> None:
    """Deux matchs du trio (1,2,3) : une win courte gold +1000@10, une loss longue −400@10.

    CC empirique 100 s / 140 s → moyenne 120 s (plafond de normalisation 240 s).
    """
    for match_id, duration, win, gold_10, vision, cc in (
        ("EUW1_W1", 1500, True, 1000, 90, 100),
        ("EUW1_L1", 2100, False, -400, 70, 140),
    ):
        conn.execute(
            "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
            " game_creation, game_duration_s, winning_team)"
            " VALUES (%s, 'euw1', '16.13', '16.13.1', 420, now(), %s, 100)",
            (match_id, duration),
        )
        conn.execute(
            "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
            " sup_champion, win, gold_diff_10, herald_taken, soul_taken, vision_score, cc_time_s)"
            " VALUES (%s, 100, 1, 2, 3, %s, %s, %s, false, %s, %s)",
            (match_id, win, gold_10, win, vision, cc),
        )


def test_api_trios_sorted_by_synergy(pg_sync, client):
    _seed_scores(pg_sync)
    payload = client.get("/api/trios").json()
    assert payload["window"] == "16.13"
    assert payload["platform"] == "euw1"
    assert payload["total"] == 2
    assert [r["jgl_champion"] for r in payload["rows"]] == [1, 4]  # synergie décroissante
    assert payload["rows"][0]["jgl_champion_name"] == "Lee Sin"


def test_api_trios_sorted_by_scaling_nulls_last(pg_sync, client):
    _seed_scores(pg_sync)  # trio (1,2,3) scaling=.015, trio (4,5,6) scaling=NULL
    payload = client.get("/api/trios", params={"sort": "scaling"}).json()
    assert [r["jgl_champion"] for r in payload["rows"]] == [1, 4]
    payload = client.get("/api/trios", params={"sort": "scaling", "dir": "asc"}).json()
    assert [r["jgl_champion"] for r in payload["rows"]] == [1, 4]  # NULL toujours en dernier


def test_api_trios_champion_filter_by_name_and_role(pg_sync, client):
    _seed_scores(pg_sync)
    payload = client.get("/api/trios", params={"champion": "Ahri", "role": "mid"}).json()
    assert [r["mid_champion"] for r in payload["rows"]] == [2]
    # Ahri ne joue pas jungle dans le jeu de données.
    payload = client.get("/api/trios", params={"champion": "Ahri", "role": "jgl"}).json()
    assert payload["rows"] == []
    assert client.get("/api/trios", params={"champion": "Inconnu"}).status_code == 404


def test_api_trio_detail_stats_and_counters(pg_sync, client):
    _seed_scores(pg_sync)
    _seed_matches(pg_sync)
    for champ_id, cc_score in ((1, 3.0), (2, 4.5), (3, 1.5)):
        pg_sync.execute(
            "INSERT INTO champion_cc_theoretical (champion_id, score) VALUES (%s, %s)",
            (champ_id, cc_score),
        )
    for role, champ_id, games, wins in (
        ("JUNGLE", 1, 20, 11),
        ("MIDDLE", 2, 20, 9),
        ("UTILITY", 3, 20, 10),
    ):
        pg_sync.execute(
            "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, %s)",
            (role, champ_id, games, wins),
        )
    payload = client.get("/api/trios/1/2/3").json()
    assert payload["score"]["wr"] == pytest.approx(0.60)
    assert payload["score"]["scaling"] == pytest.approx(0.015)
    # WR individuel baseline (agg_champion), utilisé pour la synergie brute
    # mais jamais matérialisé — recalculé en lecture pour la page détail.
    member_wr = payload["member_wr"]
    assert member_wr["jgl"] == pytest.approx(0.55)
    assert member_wr["mid"] == pytest.approx(0.45)
    assert member_wr["sup"] == pytest.approx(0.50)
    stats = payload["stats"]
    assert stats["games"] == 2
    assert stats["wr"] == pytest.approx(0.5)
    assert stats["gold_diff"]["10"] == pytest.approx(300.0)  # (1000 − 400) / 2
    assert stats["herald_taken"] == pytest.approx(0.5)
    assert stats["wr_with_soul"] is None  # aucune des 2 parties n'a l'âme
    assert stats["wr_without_soul"] == pytest.approx(0.5)  # les 2 parties sans âme
    assert stats["vision_score"] == pytest.approx(80.0)
    assert stats["avg_duration_win_s"] == pytest.approx(1500.0)
    assert stats["avg_duration_loss_s"] == pytest.approx(2100.0)
    assert payload["duos"][0]["champ_a_name"] == "Lee Sin"
    worst = payload["counters_worst"][0]
    assert (worst["enemy_champion_name"], worst["enemy_role"]) == ("Nocturne", "JUNGLE")
    assert worst["delta"] == pytest.approx(-0.014)
    best_ally = payload["allies_best"][0]
    assert (best_ally["ally_champion_name"], best_ally["ally_role"]) == ("Renekton", "TOP")
    assert best_ally["uplift"] == pytest.approx(0.045)
    # Score CC théorique brut par champion : lu depuis `champion_cc_theoretical`
    # (table matérialisée, jamais le fichier gelé — absent de l'image Docker
    # du service web, cf. Dockerfile).
    cc = payload["cc_theoretical"]
    assert (cc["jgl"], cc["mid"], cc["sup"]) == (3.0, 4.5, 1.5)
    assert cc["trio"] == pytest.approx(9.0)

    # Pourcentages 0-100 : lus tels quels depuis score_trio (mêmes valeurs que
    # la tier list), jamais recalculés côté page détail — cf. `_seed_scores`.
    cc_scores = payload["cc_scores"]
    assert cc_scores["theoretical_pct"] == pytest.approx(42.0)
    assert cc_scores["empirical_pct"] == pytest.approx(50.0)
    assert cc_scores["blended_pct"] == pytest.approx(43.7)


def test_api_duo_detail_stats_and_best_trios(pg_sync, client):
    _seed_scores(pg_sync)
    _seed_matches(pg_sync)
    for champ_id, cc_score in ((1, 3.0), (2, 4.5)):
        pg_sync.execute(
            "INSERT INTO champion_cc_theoretical (champion_id, score) VALUES (%s, %s)",
            (champ_id, cc_score),
        )
    for role, champ_id, games, wins in (("JUNGLE", 1, 20, 11), ("MIDDLE", 2, 20, 9)):
        pg_sync.execute(
            "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, %s)",
            (role, champ_id, games, wins),
        )
    payload = client.get("/api/duos/jgl_mid/1/2").json()
    assert payload["score"]["wr"] == pytest.approx(0.58)
    assert payload["score"]["champ_a_name"] == "Lee Sin"
    assert payload["score"]["champ_b_name"] == "Ahri"
    assert payload["score"]["scaling"] == pytest.approx(-0.01)
    assert payload["member_wr"]["a"] == pytest.approx(0.55)
    assert payload["member_wr"]["b"] == pytest.approx(0.45)
    # Stats du duo = celles du trio complet dans les parties où il apparaît,
    # quel que soit le 3e membre (_seed_matches ne sème que le trio 1/2/3,
    # qui contient bien le duo jgl_mid 1/2) — mêmes valeurs que la page trio.
    stats = payload["stats"]
    assert stats["games"] == 2
    assert stats["gold_diff"]["10"] == pytest.approx(300.0)
    # Le trio (1,2,3) contient le duo jgl_mid (1,2) : remonte en meilleur 3e membre.
    best = payload["best_trios"][0]
    assert (best["jgl_champion"], best["mid_champion"], best["sup_champion"]) == (1, 2, 3)
    assert best["synergy"] == pytest.approx(0.05)
    cc = payload["cc_theoretical"]
    assert (cc["a"], cc["b"]) == (3.0, 4.5)
    assert cc["duo"] == pytest.approx(7.5)
    cc_scores = payload["cc_scores"]
    assert cc_scores["theoretical_pct"] == pytest.approx(37.5)
    assert cc_scores["empirical_pct"] == pytest.approx(45.0)
    assert cc_scores["blended_pct"] == pytest.approx(40.2)


def test_html_pages_render(pg_sync, client):
    _seed_scores(pg_sync)
    _seed_matches(pg_sync)
    home = client.get("/")
    assert home.status_code == 200
    assert "Lee Sin" in home.text
    assert "Scaling" in home.text
    detail = client.get("/trio/1/2/3")
    assert detail.status_code == 200
    assert "Nocturne" in detail.text
    assert "Détail du calcul théorique" in detail.text
    assert "Mélangé (recommandé)" in detail.text
    assert "Meilleurs alliés" in detail.text
    assert "+1.50 %" in detail.text  # card Scaling (0.015 → signed_pct(2))
    assert "/duo/jgl_mid/1/2" in detail.text  # lien depuis les duos internes
    duos = client.get("/duos")
    assert duos.status_code == 200
    assert "Ahri" in duos.text
    assert "/duo/jgl_mid/1/2" in duos.text  # lien vers la page détail duo
    duo_detail = client.get("/duo/jgl_mid/1/2")
    assert duo_detail.status_code == 200
    assert "Meilleurs supports" in duo_detail.text  # roles=jgl_mid → 3e rôle libre = support


def test_champion_page_shows_baseline_partners_and_trios(pg_sync, client):
    _seed_scores(pg_sync)  # score_trio (1,2,3) + score_duo jgl_mid (1,2), tier='faible'
    # Fiabilité relevée à 'moyen' : `_seed_scores` sème du 'faible' (utilisé ailleurs
    # pour tester le filtre par défaut de la tier list), mais la page champion
    # exige 'moyen'+ pour ses listes "meilleurs" (cf. test dédié plus bas).
    pg_sync.execute("UPDATE score_trio SET tier = 'moyen'")
    pg_sync.execute("UPDATE score_duo SET tier = 'moyen'")
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES ('16.13', 'euw1', 'JUNGLE', 1, 20, 11)"
    )
    pg_sync.execute("INSERT INTO champion_cc_theoretical (champion_id, score) VALUES (1, 3.0)")
    response = client.get("/champion/jgl/1")
    assert response.status_code == 200
    assert "Lee Sin" in response.text
    assert "20 games" in response.text
    assert "Meilleurs mids" in response.text
    assert "Ahri" in response.text  # meilleur mid via score_duo jgl_mid (1,2)
    assert "/trio/1/2/3" in response.text  # meilleurs trios


def test_champion_page_hides_low_reliability_partners_and_trios(pg_sync, client):
    """Régression (retour utilisateur, 2026-07-12) : un duo/trio à 1-2 games
    avec une synergie extrême ne doit pas squatter les listes "meilleurs"."""
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES ('16.13', 'euw1', 'JUNGLE', 1, 20, 11)"
    )
    # Duo et trio à 1 game, synergie extrême, tier 'faible' — exactement le cas
    # qui polluait le classement avant le plancher de fiabilité.
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 'jgl_mid', 1, 2, 1, 1.0, 1.0, 0.9, 0.1, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.9, 0.0, 0.9, 0.1, 1.0, 'faible')"
    )
    response = client.get("/champion/jgl/1")
    assert response.status_code == 200
    assert "Ahri" not in response.text  # duo jgl_mid (1,2) reste tier 'faible'
    assert "/trio/1/2/3" not in response.text  # trio (1,2,3) reste tier 'faible'
    assert "Aucun duo scoré" in response.text
    assert "Aucun trio scoré" in response.text


def test_champion_page_unknown_role_is_404(pg_sync, client):
    _seed_scores(pg_sync)
    assert client.get("/champion/top/1").status_code == 404


def test_champion_page_unscored_champion_is_404(pg_sync, client):
    _seed_scores(pg_sync)
    assert client.get("/champion/jgl/999").status_code == 404


def test_context_bar_shows_window_volume_and_freshness(pg_sync, client):
    """Nombre de games de la fenêtre + fraîcheur de la collecte (en-tête)."""
    _seed_scores(pg_sync)
    _seed_matches(pg_sync)  # 2 matchs bruts, patch 16.13, collected_at = now()
    home = client.get("/")
    assert "2 games" in home.text
    assert "maj il y a quelques secondes" in home.text


def test_unknown_window_and_trio_are_404(pg_sync, client):
    _seed_scores(pg_sync)
    assert client.get("/api/trios", params={"window": "15.01"}).status_code == 404
    assert client.get("/api/trios/9/9/9").status_code == 404


def test_no_scores_yields_503(pg_sync, client):
    assert client.get("/api/trios").status_code == 503


def test_empty_role_param_is_accepted(pg_sync, client):
    """Régression : `role=` vide (select « tous ») renvoyait 422, que hx-boost
    avalait — le bouton Filtrer semblait mort. Idem pour les nouveaux tris."""
    _seed_scores(pg_sync)
    response = client.get("/", params={"role": "", "min_tier": "moyen", "min_games": 3})
    assert response.status_code == 200
    assert "Aucun trio" in response.text  # tout le seed est tier 'faible'
    assert client.get("/", params={"sort": "gold10"}).status_code == 200
    payload = client.get("/api/trios", params={"role": "", "min_tier": "eleve"}).json()
    assert payload["rows"] == []


def test_api_status_reports_collection(pg_sync, client):
    _seed_scores(pg_sync)
    _seed_matches(pg_sync)
    pg_sync.execute(
        "INSERT INTO match_fetch_journal (match_id, platform, status, reason)"
        " VALUES ('EUW1_X', 'euw1', 'excluded', 'duration')"
    )
    payload = client.get("/api/status").json()
    assert payload["total_matches"] == 2
    assert payload["last_collected_at"] is not None
    assert payload["matches_per_patch"] == [{"patch": "16.13", "matches": 2}]
    assert payload["journal"] == {"excluded": 1}
    # Les 2 matchs semés datent d'aujourd'hui : présents dans la vue 7 jours.
    assert sum(d["matches"] for d in payload["matches_per_day"]) == 2
