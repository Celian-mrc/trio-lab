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
}


@pytest.fixture
def pg_sync():
    """Connexion sync au Postgres de test, migrations appliquées, tables tronquées."""
    db.apply_migrations(TEST_DSN)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE players, matches, match_fetch_journal,"
            " agg_champion, agg_duo, agg_trio,"
            " agg_trio_duration, agg_duo_duration, agg_matchup,"
            " score_duo, score_trio, score_matchup, score_win_factors, score_gold_factors,"
            " score_champion_resilience, champion_cc_theoretical CASCADE"
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


def test_api_trios_champion_filter_per_role(pg_sync, client):
    _seed_scores(pg_sync)
    payload = client.get("/api/trios", params={"mid": "Ahri"}).json()
    assert [r["mid_champion"] for r in payload["rows"]] == [2]
    # Ahri ne joue pas jungle dans le jeu de données.
    payload = client.get("/api/trios", params={"jgl": "Ahri"}).json()
    assert payload["rows"] == []
    assert client.get("/api/trios", params={"jgl": "Inconnu"}).status_code == 404


def test_api_trios_champion_filters_combine_with_and(pg_sync, client):
    """3 champs indépendants, combinables — pas un simple champion+rôle unique."""
    _seed_scores(pg_sync)
    payload = client.get("/api/trios", params={"jgl": "Lee Sin", "mid": "Ahri"}).json()
    assert [r["jgl_champion"] for r in payload["rows"]] == [1]
    # Vi (jungle du 2e trio) combiné à Ahri (mid du 1er trio) : aucun trio ne matche les 2.
    payload = client.get("/api/trios", params={"jgl": "Vi", "mid": "Ahri"}).json()
    assert payload["rows"] == []


def test_api_trio_detail_stats(pg_sync, client):
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
    # Par minute (2026-07-13), pas cumulé : (90/25 + 70/35) / 2 = 2.8.
    assert stats["vision_score"] == pytest.approx(2.8)
    assert stats["avg_duration_win_s"] == pytest.approx(1500.0)
    assert stats["avg_duration_loss_s"] == pytest.approx(2100.0)
    assert payload["duos"][0]["champ_a_name"] == "Lee Sin"
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


def test_api_duo_detail_for_extended_role_pair(pg_sync, client):
    """Paire hors trio jgl/mid/sup (Phase 7) : source match_role_stats, pas de
    notion de « meilleur 3e membre » (best_trios vide)."""
    # available_windows lit score_trio : une ligne minimale pour que la
    # fenêtre '16.13' soit résolue (sinon 503 "aucun score matérialisé").
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier,"
        " cc_theoretical_pct, cc_empirical_pct, cc_blended_pct, scaling)"
        " VALUES ('16.13', 'euw1', 'top_jgl', 1, 2, 40, 40.0, 0.55, 0.02, 0.3, 0.7, 'moyen',"
        " NULL, NULL, NULL, NULL)"
    )
    for role, champ_id, games, wins in (("TOP", 1, 20, 11), ("JUNGLE", 2, 20, 9)):
        pg_sync.execute(
            "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, %s)",
            (role, champ_id, games, wins),
        )
    pg_sync.execute(
        "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
        " game_creation, game_duration_s, winning_team)"
        " VALUES ('EUW1_TOPJGL', 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)"
    )
    # match_trio_stats (objectifs team-level + CS jungle) : requis, la jointure
    # de duo_role_match_rows est un INNER JOIN (toujours présent en prod, une
    # ligne par équipe existe pour tout match valide).
    pg_sync.execute(
        "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
        " sup_champion, win, grubs_taken, herald_taken, drakes_taken, soul_taken,"
        " first_tower, towers_destroyed, plates_taken, jgl_cs_diff_15)"
        " VALUES ('EUW1_TOPJGL', 100, 2, 3, 5, true, 3, true, 2, false, true, 2, 4, 5),"
        " ('EUW1_TOPJGL', 200, 12, 13, 15, false, 1, false, 1, false, false, 1, 2, -5)"
    )
    for team, role, champ_id, gold_10, cc, dpg, dmg, fb, kp, win in (
        (100, "TOP", 1, 1200, 5, 0.8, 3000, True, 1.0, True),
        (100, "JUNGLE", 2, 1300, 7, 1.2, 4000, False, 0.5, True),
        (100, "MIDDLE", 3, 1100, 2, 0.5, 2000, False, 0.0, True),
        (100, "BOTTOM", 4, 1400, 1, 1.5, 5000, False, 0.5, True),
        (100, "UTILITY", 5, 900, 6, 0.3, 1000, False, 0.5, True),
        (200, "TOP", 99, 1000, 4, 0.6, 2500, False, 0.0, False),
        (200, "JUNGLE", 98, 1050, 6, 0.9, 3500, False, 0.0, False),
        (200, "MIDDLE", 97, 950, 1, 0.4, 1500, False, 0.0, False),
        (200, "BOTTOM", 96, 1250, 2, 1.1, 4500, False, 0.0, False),
        (200, "UTILITY", 95, 800, 5, 0.2, 900, False, 0.0, False),
    ):
        pg_sync.execute(
            "INSERT INTO match_role_stats (match_id, team_id, role, champion_id, win,"
            " gold_10, cc_time_s, dmg_per_gold, damage, first_blood, kp_pre15)"
            " VALUES ('EUW1_TOPJGL', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (team, role, champ_id, win, gold_10, cc, dpg, dmg, fb, kp),
        )

    payload = client.get("/api/duos/top_jgl/1/2").json()
    assert payload["score"]["wr"] == pytest.approx(0.55)
    assert payload["best_trios"] == []
    stats = payload["stats"]
    assert stats["games"] == 1
    # (1200+1300) − (1000+1050) = 450.
    assert stats["gold_diff"]["10"] == pytest.approx(450.0)
    # Objectifs team-level (match_trio_stats), gratuits pour cette paire.
    assert stats["grubs_taken"] == pytest.approx(3)
    assert stats["herald_taken"] == pytest.approx(1.0)
    assert stats["first_tower"] == pytest.approx(1.0)
    assert stats["jgl_cs_diff_15"] == pytest.approx(5)
    # First blood : OR exact (Top a le first blood, Jungle non).
    assert stats["first_blood_trio"] == pytest.approx(1.0)
    # Part de dégâts : (3000+4000) / (3000+4000+2000+5000+1000) = 7000/15000.
    assert stats["damage_share"] == pytest.approx(7000 / 15000)
    # KP individuelle (pas combinée) : Top 1.0, Jungle 0.5.
    assert stats["champ_a_kp_pre15"] == pytest.approx(1.0)
    assert stats["champ_b_kp_pre15"] == pytest.approx(0.5)
    # cc_time_s brut / (durée en minutes) : 5/30, 7/30.
    assert stats["champ_a_cc_time_s"] == pytest.approx(5 / 30)
    assert stats["champ_b_cc_time_s"] == pytest.approx(7 / 30)
    # dmg_per_gold : ratio direct, pas de normalisation par durée.
    assert stats["champ_a_dmg_per_gold"] == pytest.approx(0.8)
    assert stats["champ_b_dmg_per_gold"] == pytest.approx(1.2)

    detail = client.get("/duo/top_jgl/1/2")
    assert detail.status_code == 200
    assert "Meilleurs" not in detail.text  # pas de section "meilleur 3e membre"

    duos_page = client.get("/duos", params={"roles": "top_jgl"})
    assert duos_page.status_code == 200
    assert "/duo/top_jgl/1/2" in duos_page.text


def test_html_pages_render(pg_sync, client):
    _seed_scores(pg_sync)
    _seed_matches(pg_sync)
    home = client.get("/")
    assert home.status_code == 200
    assert "Lee Sin" in home.text
    assert "Scaling" in home.text
    detail = client.get("/trio/1/2/3")
    assert detail.status_code == 200
    assert "Détail du calcul théorique" in detail.text
    assert "Mélangé" in detail.text
    assert "+1.50 %" in detail.text  # card Scaling (0.015 → signed_pct(2))
    assert "/duo/jgl_mid/1/2" in detail.text  # lien depuis les duos internes
    duos = client.get("/duos")
    assert duos.status_code == 200
    assert "Ahri" in duos.text
    assert "/duo/jgl_mid/1/2" in duos.text  # lien vers la page détail duo
    duo_detail = client.get("/duo/jgl_mid/1/2")
    assert duo_detail.status_code == 200
    assert "Meilleurs supports" in duo_detail.text  # roles=jgl_mid → 3e rôle libre = support
    # Avantage gold/Objectifs/Combat/Vision affichés aussi sur la page duo
    # (retour utilisateur, 2026-07-19) : stats d'équipe dans les games de ce
    # duo pour les 3 paires historiques (via match_trio_stats, comme le trio),
    # vraiment décomposées à 2 membres pour les 7 nouvelles (match_role_stats).
    assert "Avantage gold du trio" in duo_detail.text  # roles=jgl_mid → paire historique
    assert "Objectifs" in duo_detail.text
    assert "Combat" in duo_detail.text
    assert "Héraut" in duo_detail.text


def test_champion_page_shows_baseline_partners_and_trios(pg_sync, client):
    _seed_scores(pg_sync)  # score_trio (1,2,3) + score_duo jgl_mid (1,2), tier='faible'
    _seed_matches(pg_sync)  # 2 matchs du trio (1,2,3) : champion 1 en jungle dans les 2
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
    # Pas de tableaux gold/objectifs/combat & vision sur cette page : ce sont des
    # stats de trio complet (match_trio_stats), pas propres à ce champion seul —
    # source de confusion (retour utilisateur, 2026-07-13), retirées de l'HTML.
    assert "Avantage gold" not in response.text
    assert "Objectifs" not in response.text
    assert "Combat & vision" not in response.text


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


def _seed_tied_wr_trios(conn) -> None:
    """3 trios, même WR (0.5) et mêmes games (50) : un tri sur wr seul retombe
    sur le tie-break par défaut (jgl_champion croissant, cf. queries.py), qui
    donne l'ordre 301/302/303. Les synergies sont choisies dans l'ordre
    INVERSE (301 = pire, 303 = meilleure) pour que trier ensuite sur
    `wr,synergy` produise un ordre manifestement différent — la preuve que le
    2e critère est bien appliqué, pas juste le tie-break par défaut."""
    for jgl, synergy in ((301, -0.05), (302, 0.05), (303, 0.10)):
        conn.execute(
            "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
            " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
            " ci_low, ci_high, tier)"
            " VALUES ('16.13', 'euw1', %s, 900, 901, 50, 50.0, 0.5, %s, 0.0, %s,"
            " 0.3, 0.7, 'moyen')",
            (jgl, synergy, synergy),
        )


def test_multi_sort_applies_second_criterion_not_just_default_tiebreak(pg_sync, client):
    _seed_tied_wr_trios(pg_sync)
    # Tri sur wr seul : tous à égalité -> tie-break par défaut (jgl croissant).
    single = client.get("/api/trios", params={"sort": "wr", "dir": "desc"}).json()
    assert [r["jgl_champion"] for r in single["rows"]] == [301, 302, 303]
    # Tri wr puis synergy (les deux décroissants) : la synergie décide, ordre inversé.
    multi = client.get("/api/trios", params={"sort": "wr,synergy", "dir": "desc,desc"}).json()
    assert [r["jgl_champion"] for r in multi["rows"]] == [303, 302, 301]


def test_multi_sort_html_page_shows_priority_numbers(pg_sync, client):
    _seed_tied_wr_trios(pg_sync)
    response = client.get("/", params={"sort": "wr,synergy", "dir": "desc,desc"})
    assert response.status_code == 200
    assert 'data-sort-key="wr"' in response.text
    assert 'data-sort-key="synergy"' in response.text
    # Numéros de priorité affichés uniquement à partir de 2 critères actifs.
    assert "<sup>1</sup>" in response.text
    assert "<sup>2</sup>" in response.text


def test_multi_sort_rejects_mismatched_lengths(pg_sync, client):
    _seed_scores(pg_sync)
    assert client.get("/api/trios", params={"sort": "wr,synergy", "dir": "desc"}).status_code == 404


def test_multi_sort_rejects_unknown_column(pg_sync, client):
    _seed_scores(pg_sync)
    assert (
        client.get("/api/trios", params={"sort": "wr,bogus", "dir": "desc,desc"}).status_code == 404
    )


def test_multi_sort_rejects_too_many_levels(pg_sync, client):
    _seed_scores(pg_sync)
    response = client.get(
        "/api/trios",
        params={"sort": "wr,synergy,games,gold10,cc", "dir": "desc,desc,desc,desc,desc"},
    )
    assert response.status_code == 404


def _seed_threshold_trios(conn) -> None:
    """3 trios dont WR/CC/gold@15 varient indépendamment, pour tester les
    filtres par seuil combinés (retour utilisateur, 2026-07-13) : trouver les
    combos bons sur plusieurs axes à la fois, ce qu'un tri seul ne permet pas
    quand la 1re colonne triée est presque toujours unique."""
    rows = (
        (401, 0.60, 5.0, 800),  # haut sur les 3 axes
        (402, 0.60, 1.0, 800),  # même WR/gold, CC trop bas
        (403, 0.40, 5.0, 800),  # même CC/gold, WR trop bas
    )
    for jgl, wr, cc, gold15 in rows:
        conn.execute(
            "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
            " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
            " ci_low, ci_high, tier, cc_time_s, gold_diff_15)"
            " VALUES ('16.13', 'euw1', %s, 900, 901, 50, 50.0, %s, 0.0, 0.0, 0.0,"
            " 0.3, 0.7, 'moyen', %s, %s)",
            (jgl, wr, cc, gold15),
        )


def test_min_value_filters_combine_on_multiple_columns(pg_sync, client):
    _seed_threshold_trios(pg_sync)
    payload = client.get("/api/trios", params={"min_wr": "55", "min_cc": "3"}).json()
    assert [r["jgl_champion"] for r in payload["rows"]] == [401]


def test_min_value_filters_default_to_no_filtering(pg_sync, client):
    _seed_threshold_trios(pg_sync)
    payload = client.get("/api/trios").json()
    assert sorted(r["jgl_champion"] for r in payload["rows"]) == [401, 402, 403]


def test_min_value_filters_accept_empty_string_not_422(pg_sync, client):
    """Un champ numérique vidé dans le formulaire envoie `min_wr=` (chaîne
    vide) : doit être traité comme absent, pas une 422 (même piège que `role`,
    cf. test_empty_role_param_is_accepted)."""
    _seed_threshold_trios(pg_sync)
    response = client.get("/api/trios", params={"min_wr": "", "min_cc": "", "min_gold15": ""})
    assert response.status_code == 200
    assert len(response.json()["rows"]) == 3
    assert client.get("/", params={"min_wr": "", "min_cc": ""}).status_code == 200


def test_min_value_filters_reject_out_of_range_or_invalid(pg_sync, client):
    _seed_scores(pg_sync)
    assert client.get("/api/trios", params={"min_wr": "150"}).status_code == 404
    assert client.get("/api/trios", params={"min_wr": "-5"}).status_code == 404
    assert client.get("/api/trios", params={"min_cc": "abc"}).status_code == 404


def _seed_generic_threshold_trios(conn) -> None:
    """Trios variant sur synergie et scaling (pas WR/CC/gold15) pour prouver
    que le filtre par seuil fonctionne sur n'importe quelle colonne triable,
    pas seulement les 3 d'origine (retour utilisateur, 2026-07-13)."""
    rows = (
        (411, 0.10, 0.02),  # synergie et scaling hauts
        (412, -0.05, 0.02),  # synergie basse
        (413, 0.10, -0.01),  # scaling bas
    )
    for jgl, synergy, scaling in rows:
        conn.execute(
            "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
            " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
            " ci_low, ci_high, tier, scaling)"
            " VALUES ('16.13', 'euw1', %s, 910, 911, 50, 50.0, 0.5, 0.0, 0.0, %s,"
            " 0.3, 0.7, 'moyen', %s)",
            (jgl, synergy, scaling),
        )


def test_min_value_filters_work_on_any_sortable_column(pg_sync, client):
    """Pas seulement WR/CC/Gold@15 : n'importe quelle colonne de TRIO_SORTS,
    y compris négative (synergie, scaling — un seuil négatif doit rester
    acceptable, contrairement à WR qui est borné à [0, 100])."""
    _seed_generic_threshold_trios(pg_sync)
    payload = client.get("/api/trios", params={"min_synergy": "0", "min_scaling": "0"}).json()
    assert [r["jgl_champion"] for r in payload["rows"]] == [411]
    payload = client.get("/api/trios", params={"min_synergy": "-10"}).json()
    assert sorted(r["jgl_champion"] for r in payload["rows"]) == [411, 412, 413]


def test_threshold_filter_tooltip_on_span_not_label(pg_sync, client):
    """L'icône ⓘ (CSS ::after) se place après le DERNIER enfant de l'élément
    portant `data-tooltip` : sur un <label> contenant aussi l'<input>, elle
    apparaissait après le champ au lieu du texte (retour utilisateur,
    2026-07-13). Le tooltip doit être porté par un <span> autour du seul
    texte du label, pas le <label> entier."""
    _seed_scores(pg_sync)
    html = client.get("/").text
    assert "<label data-tooltip=" not in html
    assert 'span data-tooltip="Ne montre que les combos avec au moins ce WR' in html


def test_threshold_filter_only_active_fields_visible_by_default(pg_sync, client):
    """Montrer les 13 champs vides d'un coup était illisible (retour
    utilisateur, 2026-07-14) : seul un filtre actif (valeur dans l'URL) doit
    être visible au chargement, les autres restent masqués (`[hidden]`,
    ajout/retrait ensuite géré côté client par static/thresholds.js — non
    testable en pytest). L'option correspondante disparaît du sélecteur
    "+ ajouter" pour ne pas pouvoir l'ajouter deux fois."""
    _seed_scores(pg_sync)
    html = client.get("/", params={"min_wr": "55"}).text
    assert '<label class="threshold-field" data-key="wr" >' in html
    assert '<label class="threshold-field" data-key="synergy" hidden>' in html
    assert '<option value="wr" hidden>' in html
    assert '<option value="synergy" >' in html


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


def test_draft_page_combines_synergy_and_counter(pg_sync, client):
    """Simulateur de draft (Phase 8) : edge = Σ synergie alliés + counter
    ennemi même rôle. Lee Sin (jgl, id 1), Ahri (mid, id 2), Vi (mid, id 4)."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES ('16.13', 'euw1', 'MIDDLE', 2, 100, 55)"
    )
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 'jgl_mid', 1, 2, 60, 60.0, 0.6, 0.08, 0.4, 0.8, 'moyen')"
    )
    pg_sync.execute(
        "INSERT INTO score_matchup (window_label, platform, role, champ_a, champ_b, games,"
        " games_eff, wr, delta_raw, delta, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 'MIDDLE', 2, 4, 60, 60.0, 0.55, 0.02, 0.02, 0.3, 0.7, 'moyen')"
    )

    # Grille = celle du seul slot "actif" (façon champ select) — le mettre
    # explicitement sur blue_mid pour tester ces suggestions.
    # 1er pick, aucun allié/ennemi verrouillé : repli sur le WR baseline.
    resp = client.get("/draft", params={"active": "blue_mid"})
    assert resp.status_code == 200
    assert "Ahri" in resp.text
    assert "55.0 % WR" in resp.text

    # Allié jungle verrouillé (Lee Sin) : suggestion mid par synergie seule.
    resp = client.get("/draft", params={"blue_jgl": "Lee Sin", "active": "blue_mid"})
    assert resp.status_code == 200
    assert "+8.0 %" in resp.text  # synergy .08

    # + ennemi mid verrouillé (Vi) : edge cumulé synergie + counter.
    resp = client.get(
        "/draft", params={"blue_jgl": "Lee Sin", "red_mid": "Vi", "active": "blue_mid"}
    )
    assert resp.status_code == 200
    assert "+10.0 %" in resp.text  # .08 + .02

    # Ban : Ahri (seule candidate connue pour mid) disparaît de la grille,
    # y compris du repli baseline.
    resp = client.get(
        "/draft",
        params={"blue_jgl": "Lee Sin", "red_mid": "Vi", "bans": "Ahri", "active": "blue_mid"},
    )
    assert resp.status_code == 200
    assert "Aucun champion disponible" in resp.text


def test_draft_page_locked_slot_and_clear_link(pg_sync, client):
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    resp = client.get("/draft", params={"blue_jgl": "Lee Sin"})
    assert resp.status_code == 200
    assert "Lee Sin" in resp.text
    assert 'class="draft-clear"' in resp.text
    # Le slot bleu jungle est verrouillé : pas de champ de recherche visible
    # pour lui (il reste en hidden dans les formulaires des autres slots).
    assert 'name="blue_jgl" list="champion-names"' not in resp.text


def test_draft_page_blind_grid_sorts_reliable_before_low_sample(pg_sync, client):
    """Retour utilisateur 2026-07-19 : en mode blind, le WR baseline n'est
    jamais lissé (contrairement à `edge`) — sans tri par fiabilité, un
    champion à quelques games peut passer devant un champion à 1000+ games
    pour un écart de WR qui n'est que du bruit. Vi (10 games, 80 % WR,
    low_sample) ne doit jamais apparaître avant Lee Sin (1000 games, 55 %
    WR) dans la grille — les deux restent visibles, juste dans cet ordre."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES ('16.13', 'euw1', 'TOP', 1, 1000, 550),"  # Lee Sin : 55 % WR, gros échantillon
        "        ('16.13', 'euw1', 'TOP', 4, 10, 8)"  # Vi : 80 % WR, échantillon minuscule
    )
    resp = client.get("/draft", params={"active": "blue_top"})
    assert resp.status_code == 200
    # Se limiter à la grille (pas au <datalist>, alphabétique, qui mettrait
    # "Lee Sin" avant "Vi" même si le tri par fiabilité était cassé).
    grid_html = resp.text.split('<div class="champ-grid">')[1]
    assert "Lee Sin" in grid_html
    assert "Vi" in grid_html
    assert grid_html.index("Lee Sin") < grid_html.index("Vi")


def test_draft_page_active_slot_defaults_then_advances_after_pick(pg_sync, client):
    """Interface façon champ select (retour utilisateur 2026-07-19) : un seul
    slot "actif" à la fois. Sans param `active`, c'est le 1er slot vide
    (ordre fixe blue_top puis blue_jgl...). Un pick dans le slot actif
    avance automatiquement vers le slot vide suivant."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    resp = client.get("/draft")
    assert resp.status_code == 200
    assert "Blue — Top" in resp.text

    # Un pick verrouillé ailleurs (blue_jgl) sans `active` explicite : le
    # 1er slot vide reste blue_top (l'ordre ignore les slots déjà remplis).
    resp = client.get("/draft", params={"blue_jgl": "Lee Sin"})
    assert resp.status_code == 200
    assert "Blue — Top" in resp.text

    # blue_top rempli : le slot actif par défaut avance à blue_mid (jgl
    # aussi rempli, sup/bot suivent après mid dans l'ordre des rôles).
    resp = client.get("/draft", params={"blue_jgl": "Lee Sin", "blue_top": "Thresh"})
    assert resp.status_code == 200
    assert "Blue — Mid" in resp.text


def test_draft_page_blind_pick_shows_worst_matchup_safety(pg_sync, client):
    """Sécurité blind pick (retour utilisateur 2026-07-19, clarification :
    « un blind pick est un pick qui a peu de counter, ou du moins des
    counters qui n'ont pas un énorme winrate contre ce champion ») : quand
    aucun ennemi même rôle n'est verrouillé, la grille affiche le NOMBRE de
    contres notables (delta ≤ DRAFT_NOTABLE_COUNTER_DELTA) et le pire
    d'entre eux — pas seulement le pire cas isolé (2e retour utilisateur,
    2026-07-19 : un champion avec dix contres modérés est un risque
    différent d'un champion avec un seul contre sévère)."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES ('16.13', 'euw1', 'JUNGLE', 1, 100, 50),"
        "        ('16.13', 'euw1', 'JUNGLE', 2, 100, 50),"
        "        ('16.13', 'euw1', 'JUNGLE', 4, 100, 50)"
    )
    pg_sync.execute(
        "INSERT INTO score_matchup (window_label, platform, role, champ_a, champ_b, games,"
        " games_eff, wr, delta_raw, delta, ci_low, ci_high, tier)"
        # Champion 1 (Lee Sin) : 1 contre notable (-0.1) + 1 sous le seuil (-0.02).
        " VALUES ('16.13', 'euw1', 'JUNGLE', 1, 5, 60, 60.0, 0.40, -0.1, -0.1, -0.3, 0.1, 'moyen'),"
        "        ('16.13', 'euw1', 'JUNGLE', 1, 6, 60, 60.0, 0.48, -0.02, -0.02, -0.2,"
        " 0.2, 'moyen'),"
        # Champion 2 (Ahri) : données de matchup, mais rien sous le seuil.
        "        ('16.13', 'euw1', 'JUNGLE', 2, 5, 60, 60.0, 0.49, -0.01, -0.01, -0.2,"
        " 0.2, 'moyen')"
        # Champion 4 (Vi) : aucune ligne score_matchup — pas de données.
    )
    resp = client.get("/draft", params={"active": "blue_jgl"})
    assert resp.status_code == 200
    assert 'class="badge-blind"' in resp.text
    assert '<span class="sub neg">1 contre notable (pire -10.0 %)</span>' in resp.text
    assert '<span class="sub pos">aucun contre notable</span>' in resp.text
    assert '<span class="sub meta">pas de données de contre</span>' in resp.text

    # Un ennemi jungle verrouillé : la synergie/contre réel prime, la
    # sécurité blind pick disparaît (elle ne dit rien sur CET adversaire) —
    # aucune des 3 lignes de sécurité par champion ne doit plus apparaître
    # (le paragraphe d'intro, lui, mentionne toujours "contre notable" en
    # général : on cible les balises par champion, pas le texte libre).
    resp = client.get("/draft", params={"active": "blue_jgl", "red_jgl": "Orianna"})
    assert resp.status_code == 200
    assert 'class="badge-blind"' not in resp.text
    assert 'sub neg">1 contre notable' not in resp.text
    assert 'class="sub pos">aucun contre notable</span>' not in resp.text
    assert 'class="sub meta">pas de données de contre</span>' not in resp.text
    assert "pire contre" not in resp.text


def test_insights_page_empty_state(pg_sync, client):
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    resp = client.get("/insights")
    assert resp.status_code == 200
    assert "python -m trio_lab.synergy.win_factors" in resp.text


def test_insights_page_shows_aligned_combined_table(pg_sync, client):
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    # Inséré dans le désordre, et 'behind_gold15' n'a qu'une partie des
    # features (jgl_cs_diff_15 manquant) : la page doit quand même aligner
    # chaque feature sur la même ligne dans les 2 colonnes, ordre FEATURES
    # fixe, 'intercept' jamais affiché (pas actionnable pour un coach).
    rows = (
        ("all", "soul_taken", 2.1, 8.2),
        ("all", "team_gold_diff_15", 0.96, 2.6),
        ("all", "jgl_cs_diff_15", 0.05, 1.05),
        ("all", "intercept", -0.9, 0.4),
        ("behind_gold15", "soul_taken", 2.3, 10.0),
        ("behind_gold15", "team_gold_diff_15", 0.48, 1.61),
    )
    for population, feature, coef, odds in rows:
        pg_sync.execute(
            "INSERT INTO score_win_factors (window_label, population, feature, coef,"
            " odds_ratio, n) VALUES ('16.13', %s, %s, %s, %s, 1000)",
            (population, feature, coef, odds),
        )
    resp = client.get("/insights")
    assert resp.status_code == 200
    assert "équipe complète des 5 rôles" in resp.text
    assert "ÉQUIPE à 15 min" in resp.text  # apostrophe échappée en HTML (d&#39;ÉQUIPE)
    assert "CS jungle vs adverse à 15 min" in resp.text
    assert "Âme de dragon" in resp.text
    assert "×8.20" in resp.text
    assert "×10.00" in resp.text
    # team_gold_diff_15 doit apparaître avant soul_taken : l'ordre suit
    # FEATURES, pas la valeur de l'odds ratio.
    assert resp.text.index("Avantage gold") < resp.text.index("Âme de dragon")
    # jgl_cs_diff_15 n'a une valeur QUE pour 'all' : la ligne existe quand
    # même (alignement garanti, pas de ligne manquante), valeur affichée.
    assert "×1.05" in resp.text
    # Conversion en probabilité absolue (retour utilisateur 2026-07-19) :
    # sigmoid(intercept) → sigmoid(intercept + coef) pour 'all'/soul_taken
    # (intercept=-0.9, coef=2.1) ; 'behind_gold15' n'a pas de ligne intercept
    # dans ce jeu de données, donc pas de conversion pour cette colonne —
    # ne doit pas planter, juste ne rien afficher pour cette cellule.
    assert "29 % → 77 %" in resp.text


def test_insights_page_shows_gold_factors_section(pg_sync, client):
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    rows = (
        (None, "_r2_draft_only", 0.62),
        (None, "_r2_full", 0.68),
        ("draft", "team_baseline_wr", 247.0),
        ("draft", "team_matchup_delta", 5.0),
        ("draft", "team_trio_synergy", -3.0),
        ("execution", "jgl_cs_diff_15", 92.0),
        ("execution", "first_blood_team", -12.0),
    )
    for block, feature, coef in rows:
        pg_sync.execute(
            "INSERT INTO score_gold_factors (window_label, block, feature, coef, n)"
            " VALUES ('16.13', %s, %s, %s, 5000)",
            (block, feature, coef),
        )
    resp = client.get("/insights")
    assert resp.status_code == 200
    assert "Qu'est-ce qui construit cet avantage au gold" in resp.text
    assert "62 %" in resp.text  # R² draft seul
    assert "68 %" in resp.text  # R² complet
    assert "Force brute des picks (WR baseline)" in resp.text
    assert "+247 gold" in resp.text
    assert "-12 gold" in resp.text
    # team_baseline_wr (bloc draft) doit apparaître avant jgl_cs_diff_15
    # (bloc exécution) : ordre fixe GOLD_FACTOR_FEATURES.
    assert resp.text.index("Force brute des picks") < resp.text.index("CS jungle vs adverse")


def test_resilience_page_shows_per_champion_ahead_behind_gap(pg_sync, client):
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    # Champion 1 (Lee Sin) JUNGLE : très résilient (WR haut des 2 côtés,
    # écart faible) ; champion 4 (Vi) JUNGLE : dépend fortement de l'avance
    # (écart large) — sous le seuil de fiabilité (games < 30) pour tester le
    # grisage sans le masquer.
    pg_sync.execute(
        "INSERT INTO score_champion_resilience (window_label, role, champion_id, factor,"
        " games_ahead, wins_ahead, games_behind, wins_behind)"
        " VALUES ('16.13', 'JUNGLE', 1, 'team_gold_diff_15', 100, 70, 100, 60),"
        "        ('16.13', 'JUNGLE', 4, 'team_gold_diff_15', 10, 9, 10, 1)"
    )
    resp = client.get("/resilience", params={"factor": "team_gold_diff_15"})
    assert resp.status_code == 200
    assert "Lee Sin" in resp.text
    assert "70 %" in resp.text  # WR en avance, champion 1
    assert "60 %" in resp.text  # WR en retard, champion 1
    # Champion 4 : sous le seuil de fiabilité des 2 côtés (10 < 30 games) —
    # grisé (classe low-sample) SUR SA LIGNE précisément, jamais masqué.
    vi_row = resp.text[resp.text.rindex("<tr", 0, resp.text.index("Vi")) :]
    assert 'class="low-sample"' in vi_row.split(">", 1)[0]
    assert "90 %" in resp.text  # WR en avance, champion 4 (9/10)

    # Facteur inconnu / rôle inconnu : 404, pas un crash silencieux.
    assert client.get("/resilience", params={"factor": "inconnu"}).status_code == 404
    assert (
        client.get(
            "/resilience", params={"factor": "team_gold_diff_15", "role": "INVALID"}
        ).status_code
        == 404
    )


def test_flex_page_detects_off_role_resource_deviation(pg_sync, client):
    """Champion 1 : Top (300 games, principal) + Support (150 games, 33 % —
    rôle secondaire non anecdotique). Son gold@15 en Support (5200, sur 40
    games récentes) dépasse la moyenne du rôle (mix avec le champion 2, qui
    ne joue QUE support à 4400) — doit remonter dans /flex."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES ('16.13', 'euw1', 'TOP', 1, 300, 150)"
    )
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES ('16.13', 'euw1', 'UTILITY', 1, 150, 70)"
    )
    for champ_id, gold_15, count in ((1, 5200, 40), (2, 4400, 40)):
        for i in range(count):
            match_id = f"FLEX_{champ_id}_{i}"
            pg_sync.execute(
                "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
                " game_creation, game_duration_s, winning_team)"
                " VALUES (%s, 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)",
                (match_id,),
            )
            pg_sync.execute(
                "INSERT INTO match_role_stats (match_id, team_id, role, champion_id, win,"
                " gold_15, dmg_per_gold) VALUES (%s, 100, 'UTILITY', %s, true, %s, 1.5)",
                (match_id, champ_id, gold_15),
            )
    resp = client.get("/flex")
    assert resp.status_code == 200
    assert "Lee Sin" in resp.text  # champion_id=1 dans l'index de test
    assert "Top" in resp.text
    assert "Support" in resp.text
    # Part des games en support : 150 / (300+150) = 33.3 %.
    assert "33.3 %" in resp.text
    # Gold@15 support (5200) vs moyenne du rôle (40×5200+40×4400)/80 = 4800 :
    # ratio = 5200/4800 ≈ 1.08.
    assert "×1.08" in resp.text
    # Phrase en langage clair, pas juste des chiffres bruts (retour utilisateur).
    assert "Lee Sin joue Support dans 33 % de ses games (150/450)" in resp.text
    # Filtre par rôle : Support seulement.
    resp_role = client.get("/flex", params={"role": "UTILITY"})
    assert resp_role.status_code == 200
    assert "Lee Sin" in resp_role.text
    resp_wrong_role = client.get("/flex", params={"role": "TOP"})
    assert "Lee Sin" not in resp_wrong_role.text  # son rôle secondaire est Support, pas Top
    assert client.get("/flex", params={"role": "INVALID"}).status_code == 404


def test_flex_page_hides_deviation_below_threshold(pg_sync, client):
    """Un profil quasi identique à la moyenne du rôle (<5 % d'écart) n'est
    pas un vrai signal hybride — ne doit pas apparaître (retour utilisateur :
    la liste se noyait dans du bruit proche de 0 sans ce plancher)."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES ('16.13', 'euw1', 'TOP', 1, 300, 150)"
    )
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)"
        " VALUES ('16.13', 'euw1', 'UTILITY', 1, 150, 70)"
    )
    # Champion 1 en support : gold_15 = 4520, quasi identique à la moyenne
    # du rôle (champion 2 seul, 4500) — écart < 1 %, sous le seuil de 5 %.
    for champ_id, gold_15, count in ((1, 4520, 40), (2, 4500, 40)):
        for i in range(count):
            match_id = f"NOFLEX_{champ_id}_{i}"
            pg_sync.execute(
                "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
                " game_creation, game_duration_s, winning_team)"
                " VALUES (%s, 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)",
                (match_id,),
            )
            pg_sync.execute(
                "INSERT INTO match_role_stats (match_id, team_id, role, champion_id, win,"
                " gold_15, dmg_per_gold) VALUES (%s, 100, 'UTILITY', %s, true, %s, 1.5)",
                (match_id, champ_id, gold_15),
            )
    resp = client.get("/flex")
    assert resp.status_code == 200
    assert "0 pick" in resp.text
    assert "Lee Sin" not in resp.text
