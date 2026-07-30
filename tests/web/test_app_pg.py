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
    7: Champion(7, "Zed", ""),
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
            " score_champion_resilience, champion_cc_theoretical,"
            " draft_suggestion, draft_suggestion_counter CASCADE"
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


def _seed_mixed_role_duos(conn) -> None:
    """3 duos sur 3 paires de rôles DIFFÉRENTES (retour utilisateur 2026-07-20 :
    filtrer par seuil sans devoir choisir une paire) — WR variée pour tester
    le filtre par seuil en même temps que le mélange."""
    conn.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    rows = (
        ("jgl_mid", 1, 2, 0.60),
        ("top_bot", 4, 5, 0.45),
        ("bot_sup", 6, 7, 0.70),
    )
    for roles, champ_a, champ_b, wr in rows:
        conn.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, 60, 60.0, %s, 0.0, 0.3, 0.7, 'moyen')",
            (roles, champ_a, champ_b, wr),
        )


def test_duos_page_all_roles_mixes_every_pair(pg_sync, client):
    """`roles=all` (retour utilisateur : chercher "peu importe les rôles")
    retourne les 3 paires ensemble, la colonne Duo affiche le bon rôle pour
    chaque ligne (pas figé sur la paire sélectionnée)."""
    _seed_mixed_role_duos(pg_sync)
    payload = client.get("/api/duos", params={"roles": "all"}).json()
    assert sorted((r["roles"], r["champ_a"], r["champ_b"]) for r in payload["rows"]) == [
        ("bot_sup", 6, 7),
        ("jgl_mid", 1, 2),
        ("top_bot", 4, 5),
    ]

    # Sans "all", une seule paire à la fois (comportement inchangé).
    payload = client.get("/api/duos", params={"roles": "jgl_mid"}).json()
    assert [(r["roles"], r["champ_a"]) for r in payload["rows"]] == [("jgl_mid", 1)]


def test_duos_page_all_roles_combines_with_threshold_filters(pg_sync, client):
    """Le vrai cas d'usage : filtrer par seuil SANS choisir de paire — doit
    fonctionner exactement comme avec une paire fixée."""
    _seed_mixed_role_duos(pg_sync)
    payload = client.get("/api/duos", params={"roles": "all", "min_wr": "50"}).json()
    assert sorted(r["roles"] for r in payload["rows"]) == ["bot_sup", "jgl_mid"]


def test_duos_page_all_roles_ignores_role_specific_champion_search(pg_sync, client):
    """champ_a/champ_b sont des recherches PAR RÔLE : sans rôle fixé, elles ne
    veulent plus rien dire — ignorées plutôt que de filtrer sur une colonne
    dont le sens change selon la ligne (pas de 404/crash)."""
    _seed_mixed_role_duos(pg_sync)
    resp = client.get("/duos", params={"roles": "all", "champ_a": "Lee Sin", "champ_b": "Ahri"})
    assert resp.status_code == 200
    assert "3 duos" in resp.text  # les 3 lignes restent visibles, pas filtrées


def test_duos_page_all_roles_hides_role_specific_search_fields(pg_sync, client):
    _seed_mixed_role_duos(pg_sync)
    html = client.get("/duos", params={"roles": "all"}).text
    assert 'name="champ_a"' not in html
    assert 'name="champ_b"' not in html
    assert '<option value="all" selected>' in html


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


def test_tierlist_and_duos_pages_show_team_gold15(pg_sync, client):
    """Diff gold@15 de l'ÉQUIPE ENTIÈRE (migration 032, retour utilisateur
    2026-07-20), en plus du gold@15 du trio/duo — NULL affiché comme '—'
    quand match_role_stats n'a pas la donnée pour ce patch (colonne 2 dans
    `_seed_scores`, jamais mise à jour)."""
    _seed_scores(pg_sync)
    pg_sync.execute(
        "UPDATE score_trio SET team_gold_diff_15 = -320"
        " WHERE jgl_champion = 1 AND mid_champion = 2 AND sup_champion = 3"
    )
    pg_sync.execute("UPDATE score_duo SET team_gold_diff_15 = 210 WHERE roles = 'jgl_mid'")
    home = client.get("/")
    assert "Gold@15 équipe" in home.text
    assert "-320" in home.text
    duos = client.get("/duos")
    assert "Gold@15 équipe" in duos.text
    assert "+210" in duos.text


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


def _seed_gold15_range_trios(conn) -> None:
    """3 trios avec un gold@15 très différent (retour utilisateur 2026-07-20 :
    filtrer par plage, pas juste un plancher — ex. trouver les combos qui
    restent bons même en retard, gold@15 au plus 0)."""
    rows = (
        (421, -900),  # nettement en retard
        (422, -100),  # légèrement en retard
        (423, 700),  # nettement en avance
    )
    for jgl, gold15 in rows:
        conn.execute(
            "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
            " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
            " ci_low, ci_high, tier, gold_diff_15)"
            " VALUES ('16.13', 'euw1', %s, 920, 921, 50, 50.0, 0.5, 0.0, 0.0, 0.0,"
            " 0.3, 0.7, 'moyen', %s)",
            (jgl, gold15),
        )


def test_max_value_filter_applies_upper_bound(pg_sync, client):
    """`max_gold15` filtre "au plus X", symétrique de `min_gold15` — répond au
    retour utilisateur : filtrer un plafond, pas seulement un plancher."""
    _seed_gold15_range_trios(pg_sync)
    payload = client.get("/api/trios", params={"max_gold15": "0"}).json()
    assert sorted(r["jgl_champion"] for r in payload["rows"]) == [421, 422]
    payload = client.get("/api/trios", params={"max_gold15": "-500"}).json()
    assert [r["jgl_champion"] for r in payload["rows"]] == [421]


def test_min_and_max_value_filters_combine_into_a_range(pg_sync, client):
    """min et max sur la même colonne, en même temps : une vraie plage."""
    _seed_gold15_range_trios(pg_sync)
    payload = client.get("/api/trios", params={"min_gold15": "-500", "max_gold15": "0"}).json()
    assert [r["jgl_champion"] for r in payload["rows"]] == [422]


def test_max_value_filters_accept_empty_string_not_422(pg_sync, client):
    _seed_threshold_trios(pg_sync)
    response = client.get("/api/trios", params={"max_wr": "", "max_gold15": ""})
    assert response.status_code == 200
    assert len(response.json()["rows"]) == 3
    assert client.get("/", params={"max_wr": "", "max_cc": ""}).status_code == 200


def test_max_value_filters_reject_out_of_range_or_invalid(pg_sync, client):
    _seed_scores(pg_sync)
    assert client.get("/api/trios", params={"max_wr": "150"}).status_code == 404
    assert client.get("/api/trios", params={"max_wr": "-5"}).status_code == 404
    assert client.get("/api/trios", params={"max_cc": "abc"}).status_code == 404


def test_threshold_filter_tooltip_on_span_not_label(pg_sync, client):
    """L'icône ⓘ (CSS ::after) se place après le DERNIER enfant de l'élément
    portant `data-tooltip` : sur un <label> contenant aussi l'<input>, elle
    apparaissait après le champ au lieu du texte (retour utilisateur,
    2026-07-13). Le tooltip doit être porté par un <span> autour du seul
    texte du label, pas le <label> entier."""
    _seed_scores(pg_sync)
    html = client.get("/").text
    assert "<label data-tooltip=" not in html
    assert 'span data-tooltip="Ne montre que les combos dans cette plage de WR' in html


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


def test_threshold_filter_max_alone_also_activates_the_field(pg_sync, client):
    """min et max sont indépendants (retour utilisateur 2026-07-20) : un
    `max_wr` seul (pas de `min_wr`) doit aussi révéler le champ, pas juste
    min — et les 2 sous-champs affichent chacun leur propre valeur."""
    _seed_scores(pg_sync)
    html = client.get("/", params={"max_wr": "70"}).text
    assert '<label class="threshold-field" data-key="wr" >' in html
    assert 'name="min_wr" value=""' in html
    assert 'name="max_wr" value="70"' in html


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


def test_draft_page_shows_precomputed_compositions_for_platform_all(pg_sync, client):
    """Région par défaut (`platform="all"`) : les compositions précalculées
    (`draft_suggestion(_counter)`, matérialisées par le service collector)
    s'affichent SANS clic sur le bouton (retour utilisateur 2026-07-25 :
    "je voulais garder les drafts proposées sans avoir à cliquer"), et le
    bouton "Proposer des compositions" disparaît (plus la peine, déjà là)."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'all', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO draft_suggestion (window_label, platform, archetype, label,"
        " top_champion, jgl_champion, mid_champion, bot_champion, sup_champion,"
        " total_synergy, seed_roles, seed_champ_a, seed_champ_b, seed_synergy, seed_games,"
        " seed_tier, advice_scaling, advice_cc, advice_gold15)"
        " VALUES ('16.13', 'all', 'synergy', 'Meilleure synergie', 4, 1, 2, 5, 3, 0.51,"
        " 'jgl_mid', 1, 2, 0.30, 60, 'eleve', 0.08, 70.0, 800.0)"
    )
    pg_sync.execute(
        "INSERT INTO draft_suggestion_counter (window_label, platform, archetype, kind, rank,"
        " role, against_champion, champion_id, delta)"
        " VALUES ('16.13', 'all', 'synergy', 'primary', 0, 'jgl', 1, 6, 0.10)"
    )
    resp = client.get("/draft", params={"platform": "all"})
    assert resp.status_code == 200
    assert "Proposer des compositions" not in resp.text
    assert "draft-suggest-card" in resp.text
    for name in ("Vi", "Lee Sin", "Ahri", "Orianna", "Thresh", "Leona"):
        assert name in resp.text
    assert "+51.0 %" in resp.text
    assert "monte en puissance" in resp.text  # advice_scaling = 0.08 > seuil


def test_draft_page_falls_back_to_button_when_nothing_precomputed(pg_sync, client):
    """`platform="all"` mais rien encore matérialisé (ex. juste après un
    déploiement, avant le 1er cycle du collector) : retombe sur le bouton
    "Proposer des compositions", jamais de page cassée/vide sans explication."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'all', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    resp = client.get("/draft", params={"platform": "all"})
    assert resp.status_code == 200
    assert "Proposer des compositions" in resp.text
    assert "draft-suggest-card" not in resp.text


def test_draft_page_suggest_button_shown_but_not_computed_by_default(pg_sync, client):
    """Compositions suggérées (retour utilisateur 2026-07-24) : le calcul
    (~10-15s, 4 archétypes) ne tourne jamais sur un chargement de page
    normal, seulement sur clic explicite (`?suggest=1`) — le bouton reste
    visible dans les 2 cas."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    resp = client.get("/draft")
    assert resp.status_code == 200
    assert "Compositions suggérées" in resp.text
    assert "Proposer des compositions" in resp.text
    # Pas de calcul : ni carte de composition, ni message "pas assez de duos".
    assert "draft-suggest-card" not in resp.text
    assert "Pas assez de duos fiables" not in resp.text


def _seed_suggest_scenario(conn) -> None:
    """Duo de départ jgl_mid (champ 1/2, synergie +30 %, le plus fort de
    tous — gagne l'archétype "Meilleure synergie") étendu rôle par rôle
    (sup puis top puis bot, sans ordre fixe imposé — c'est juste l'ordre
    que donnent ces synergies) vers un draft complet à 5. Chaque paire
    n'a QU'UN seul candidat renseigné : le chemin glouton est donc
    déterministe, vérifiable à la main.
    Index de test : 1=Lee Sin(jgl), 2=Ahri(mid), 3=Thresh(sup), 4=Vi(top),
    5=Orianna(bot).
    Total attendu : seed jgl_mid (.30)
      + étape 1 (sup, vs jgl+mid) : jgl_sup(.05) + mid_sup(.04) = .09
      + étape 2 (top, vs jgl+mid+sup) : top_jgl(.02)+top_mid(.02)+top_sup(.03) = .07
      + étape 3 (bot, vs les 4 autres) : jgl_bot(.01)+mid_bot(.01)+bot_sup(.01)+top_bot(.02) = .05
      = .30 + .09 + .07 + .05 = .51 (couvre bien les 10 paires : 1+2+3+4=10)."""
    # available_windows() lit score_trio (jamais score_duo) pour savoir
    # quelles fenêtres existent — ligne minimale, son contenu n'est jamais
    # lu par l'algorithme de suggestion (100 % duo-based désormais).
    conn.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    conn.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 'jgl_mid', 1, 2, 500, 500.0, 0.55, 0.30, 0.0, 0.30, 'eleve')"
    )
    rows = (
        ("jgl_sup", 1, 3, 0.05),
        ("mid_sup", 2, 3, 0.04),
        ("top_jgl", 4, 1, 0.02),
        ("top_mid", 4, 2, 0.02),
        ("top_sup", 4, 3, 0.03),
        ("jgl_bot", 1, 5, 0.01),
        ("mid_bot", 2, 5, 0.01),
        ("bot_sup", 5, 3, 0.01),
        ("top_bot", 4, 5, 0.02),
    )
    for roles, champ_a, champ_b, synergy in rows:
        conn.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, 500, 500.0, 0.55, %s, 0.0, %s, 'eleve')",
            (roles, champ_a, champ_b, synergy, synergy),
        )


def test_draft_page_suggest_proposes_synergy_based_composition(pg_sync, client):
    _seed_suggest_scenario(pg_sync)
    resp = client.get("/draft", params={"suggest": "1"})
    assert resp.status_code == 200
    assert "draft-suggest-card" in resp.text
    assert "Meilleure synergie" in resp.text
    # Les 5 membres de la composition (index de test : 1=Lee Sin, 2=Ahri,
    # 3=Thresh, 4=Vi, 5=Orianna).
    for name in ("Vi", "Lee Sin", "Ahri", "Orianna", "Thresh"):
        assert name in resp.text
    # Synergie totale exacte : .30 + .09 + .07 + .05 = .51 (cf. docstring
    # de _seed_suggest_scenario pour le détail des 10 paires couvertes).
    assert "+51.0 %" in resp.text
    # Duo de départ JAMAIS affiché sur "Compositions suggérées" (retour
    # utilisateur 2026-07-27 : détail interne, cf. web/app.py
    # `_build_draft_result(include_seed_pairs=False)`).
    assert "eleve (500)" not in resp.text


def test_draft_page_suggest_shows_advice_from_seed_duo_stats(pg_sync, client):
    """Les conseils de jeu (retour utilisateur 2026-07-24) traduisent des
    stats moyennes sur les 10 VRAIES paires du draft COMPLET
    (`_full_draft_stat_averages`), pas seulement du duo de départ (v3,
    2026-07-25) — jamais présentés comme une garantie, juste une phrase.
    Toutes les paires portent ici les mêmes scaling/cc/gold_diff_15 pour que
    la moyenne du draft complet reste ces valeurs, pas seulement le seed."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct, gold_diff_15)"
        " VALUES ('16.13', 'euw1', 'jgl_mid', 1, 2, 500, 500.0, 0.55, 0.30, 0.0, 0.30, 'eleve',"
        " 0.08, 70.0, 800.0)"
    )
    rows = (
        ("jgl_sup", 1, 3, 0.05),
        ("mid_sup", 2, 3, 0.04),
        ("top_jgl", 4, 1, 0.02),
        ("top_mid", 4, 2, 0.02),
        ("top_sup", 4, 3, 0.03),
        ("jgl_bot", 1, 5, 0.01),
        ("mid_bot", 2, 5, 0.01),
        ("bot_sup", 5, 3, 0.01),
        ("top_bot", 4, 5, 0.02),
    )
    for roles, champ_a, champ_b, synergy in rows:
        pg_sync.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct, gold_diff_15)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, 500, 500.0, 0.55, %s, 0.0, %s, 'eleve',"
            " 0.08, 70.0, 800.0)",
            (roles, champ_a, champ_b, synergy, synergy),
        )
    resp = client.get("/draft", params={"suggest": "1"})
    assert resp.status_code == 200
    assert "monte en puissance" in resp.text  # scaling > seuil notable
    assert "contrôle de foule" in resp.text  # cc_blended_pct >= seuil notable
    assert "économique attendu tôt" in resp.text  # gold_diff_15 > seuil notable
    # Winrate + IC (retour utilisateur 2026-07-27) : moyenne simple sur les
    # 10 vraies paires, wr=0.55 partout -> 55.0 % ; ci_low=0.0 partout -> 0.0 % ;
    # ci_high = synergie de chaque paire (.30+.05+.04+.02+.02+.03+.01+.01+.01+.02)/10
    # = .051 -> 5.1 %.
    assert "Winrate : 55.0 %" in resp.text
    assert "[0.0 % – 5.1 %]" in resp.text


def test_draft_page_suggest_skips_archetypes_without_stat_data(pg_sync, client):
    """Sans scaling/CC/gold/drakes/portée renseignés sur aucun duo, les 4
    archétypes pondérés (Scaling/Early/Objectifs/Poke) n'ont aucun candidat
    classable — seuls "Meilleure synergie" et "Meilleur winrate" (qui ne
    dépendent pas de ces colonnes, `_seed_suggest_scenario` renseigne bien
    `wr`) produisent une composition, pas de ligne vide/plantée pour les
    4 autres."""
    _seed_suggest_scenario(pg_sync)
    resp = client.get("/draft", params={"suggest": "1"})
    assert resp.status_code == 200
    assert resp.text.count("draft-suggest-card") == 2
    assert "Meilleure synergie" in resp.text
    assert "Meilleur winrate" in resp.text
    # "Scaling / fin de partie" reste dans le <select> du formulaire "Compose
    # à partir de tes champions" (toujours proposé) : on cible précisément
    # le titre de carte, pas le texte libre de la page.
    assert '<h3 class="draft-suggest-label">Scaling / fin de partie</h3>' not in resp.text


def test_draft_page_suggest_renders_both_archetypes_from_shared_champion_pool(pg_sync, client):
    """Même quand "Meilleure synergie" et "Scaling" retombent sur le même
    univers fermé à 5 champions (leurs duos de départ internes diffèrent
    pourtant bel et bien — vérifié au niveau de la fonction par
    `test_propose_drafts_uses_different_seed_duo_per_archetype`, plus ce
    détail interne n'est plus affiché ici depuis le retour utilisateur
    2026-07-27), les 2 cartes se rendent sans erreur, chacune avec son
    propre libellé."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    # Duo A (jgl_mid, champ 1/2) : synergie dominante (+30 %) mais scaling
    # négatif (-10 %, "early") — gagne "Meilleure synergie", perd "Scaling".
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct, gold_diff_15,"
        " drakes, soul_rate)"
        " VALUES ('16.13', 'euw1', 'jgl_mid', 1, 2, 500, 500.0, 0.55, 0.30, 0.0, 0.30, 'eleve',"
        " -0.10, 20.0, 100.0, 0.02, 0.10)"
    )
    # Duo B (top_bot, champ 4/5) : synergie bien plus faible (+5 %) mais
    # scaling fortement positif (+10 %, "scaling") — perd "Meilleure
    # synergie", gagne "Scaling" (poids scaling = 38.5 %, dominant).
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct, gold_diff_15,"
        " drakes, soul_rate)"
        " VALUES ('16.13', 'euw1', 'top_bot', 4, 5, 500, 500.0, 0.55, 0.05, 0.0, 0.05, 'eleve',"
        " 0.10, 20.0, 100.0, 0.02, 0.10)"
    )
    # Paires structurelles restantes : stats archétype neutres et
    # identiques partout (scaling=0.0, cc/gold/drakes/âme = les mêmes
    # valeurs que les 2 duos de départ) — comme ça un candidat n'est jamais
    # exclu faute de donnée, et son z-score sur ces axes est ~0 (n'influence
    # pas le classement, seule la synergie et le scaling des SEEDS
    # discriminent).
    rows = (
        ("jgl_sup", 1, 3, 0.05),
        ("mid_sup", 2, 3, 0.04),
        ("top_jgl", 4, 1, 0.02),
        ("top_mid", 4, 2, 0.02),
        ("top_sup", 4, 3, 0.03),
        ("jgl_bot", 1, 5, 0.01),
        ("mid_bot", 2, 5, 0.01),
        ("bot_sup", 5, 3, 0.01),
    )
    for roles, champ_a, champ_b, synergy in rows:
        pg_sync.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct,"
            " gold_diff_15, drakes, soul_rate)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, 500, 500.0, 0.55, %s, 0.0, %s, 'eleve',"
            " 0.0, 20.0, 100.0, 0.02, 0.10)",
            (roles, champ_a, champ_b, synergy, synergy),
        )
    resp = client.get("/draft", params={"suggest": "1"})
    assert resp.status_code == 200
    assert "Meilleure synergie" in resp.text
    assert "Scaling / fin de partie" in resp.text


def _insert_pentad(
    conn, base_id: int, seed_synergy: float, platform: str = "euw1", games_eff: float = 60.0
) -> None:
    """Version web de `synergy/test_draft_suggestions_pg._insert_pentad` :
    10 paires d'un pentade FERMÉ (jgl/mid/sup/top/bot = base_id..base_id+4),
    jamais de champion partagé avec un autre pentade — sert à tester le
    rendu des boutons 1/2/3 (retour utilisateur 2026-07-27) sans dépendre de
    l'algorithme de sélection déjà couvert côté synergy."""
    jgl, mid, sup, top, bot = base_id, base_id + 1, base_id + 2, base_id + 3, base_id + 4
    rows = (
        ("jgl_mid", jgl, mid, seed_synergy),
        ("jgl_sup", jgl, sup, 0.01),
        ("mid_sup", mid, sup, 0.01),
        ("top_jgl", top, jgl, 0.01),
        ("top_mid", top, mid, 0.01),
        ("top_sup", top, sup, 0.01),
        ("jgl_bot", jgl, bot, 0.01),
        ("mid_bot", mid, bot, 0.01),
        ("bot_sup", bot, sup, 0.01),
        ("top_bot", top, bot, 0.01),
    )
    for roles, champ_a, champ_b, synergy in rows:
        conn.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier)"
            " VALUES ('16.13', %s, %s, %s, %s, 500, %s, 0.55, %s, 0.0, %s, 'eleve')",
            (platform, roles, champ_a, champ_b, games_eff, synergy, synergy),
        )


def test_draft_page_suggest_shows_variant_tabs_for_multiple_propositions(pg_sync, client):
    """Boutons 1/2/3 (retour utilisateur 2026-07-27) : 3 pentades disjoints
    (aucun champion en commun) produisent 3 propositions DIVERSES pour
    "Meilleure synergie" — la carte doit afficher 3 boutons d'onglet, seule
    la 1ère variante est visible par défaut (les autres portent `hidden`),
    et le bouton de la 3e (la plus fiable, `games_eff` le plus haut) porte
    un indicateur visuel SANS avoir à cliquer dessus (retour utilisateur
    2026-07-28). wr=0.55 uniforme sur ce fixture (`_insert_pentad`) : sans
    signal discriminant, "Meilleur winrate" retombe sur le même classement
    que "Meilleure synergie" (même raisonnement que côté synergy_pg) — 2
    cartes affichent donc ce comportement, pas 1."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    _insert_pentad(pg_sync, 1, seed_synergy=0.30, games_eff=100.0)
    _insert_pentad(pg_sync, 6, seed_synergy=0.20, games_eff=100.0)
    _insert_pentad(pg_sync, 11, seed_synergy=0.10, games_eff=5000.0)
    resp = client.get("/draft", params={"suggest": "1"})
    assert resp.status_code == 200
    assert resp.text.count('class="draft-variant-tab active"') == 2
    assert resp.text.count("draft-variant-tab") >= 3
    assert resp.text.count('data-variant-index="0"') >= 1
    assert resp.text.count('data-variant-index="1" hidden') >= 1
    assert resp.text.count('data-variant-index="2" hidden') >= 1
    assert 'data-selection="reliable" title="La plus fiable"' in resp.text


def test_draft_page_custom_weights_shows_own_section(pg_sync, client):
    """ "Personnalise tes poids" (retour utilisateur 2026-07-28, "que
    l'utilisateur décide lui-même des poids afin d'avoir un archétype
    custom") : poids valides (somme = 100) → une composition apparaît dans
    sa PROPRE section (`draft-custom-result`), jamais mélangée à la grille
    des 4 archétypes fixes."""
    _seed_suggest_scenario(pg_sync)
    resp = client.get(
        "/draft",
        params={
            "w_synergy": "100",
            "w_scaling": "0",
            "w_cc": "0",
            "w_gold": "0",
            "w_drakes": "0",
            "w_soul": "0",
        },
    )
    assert resp.status_code == 200
    assert "Personnalisé" in resp.text
    assert '<div class="draft-custom-result">' in resp.text
    for name in ("Vi", "Lee Sin", "Ahri", "Orianna", "Thresh"):
        assert name in resp.text


def test_draft_page_custom_weights_requires_sum_to_100(pg_sync, client):
    """Somme ≠ 100 : message d'erreur explicite, jamais une correction
    silencieuse (retour utilisateur : préciser que la somme doit faire
    100 %)."""
    _seed_suggest_scenario(pg_sync)
    resp = client.get(
        "/draft",
        params={"w_synergy": "50", "w_scaling": "20", "w_cc": "0", "w_gold": "0"},
    )
    assert resp.status_code == 200
    assert "La somme des poids doit faire 100" in resp.text
    assert "70" in resp.text  # rappelle la somme actuelle
    assert '<div class="draft-custom-result">' not in resp.text


def test_draft_page_custom_weights_rejects_negative(pg_sync, client):
    """Poids négatif : message d'erreur, jamais silencieusement clampé à 0."""
    _seed_suggest_scenario(pg_sync)
    resp = client.get(
        "/draft",
        params={"w_synergy": "-10", "w_scaling": "110", "w_cc": "0", "w_gold": "0"},
    )
    assert resp.status_code == 200
    assert "ne peuvent pas être négatifs" in resp.text


def test_draft_page_no_custom_weights_shows_neither_card_nor_error(pg_sync, client):
    """Page fraîche (aucun champ de poids rempli) : ni carte, ni message
    d'erreur — le formulaire "Personnalise tes poids" n'a jamais été
    soumis, ce n'est pas un cas d'échec."""
    _seed_suggest_scenario(pg_sync)
    resp = client.get("/draft")
    assert resp.status_code == 200
    assert '<div class="draft-custom-result">' not in resp.text
    assert "La somme des poids doit faire 100" not in resp.text


def test_draft_page_compose_with_custom_weights(pg_sync, client):
    """ "Compose à partir de tes champions" avec archétype "custom" (retour
    utilisateur 2026-07-28, "on puisse aussi personnaliser les poids") :
    les poids soumis via les champs `cw_<axe>`, PROPRES à ce formulaire —
    distincts de `w_<axe>` ("Personnalise tes poids", retour utilisateur
    "pourquoi il devrait partager les mêmes poids personnalisés ?" — 2
    formulaires, 2 états indépendants) — s'appliquent à la complétion des
    champions choisis à la main. Seuls `cw_*` sont soumis ici (jamais
    `w_*`) : si le test passait à cause du 5e archétype auto-suggéré de
    "Personnalise tes poids" plutôt que de `manual_results`, ce serait un
    faux positif — n'arrive plus avec des champs séparés."""
    _seed_suggest_scenario(pg_sync)
    resp = client.get(
        "/draft",
        params={
            "seed_jgl": "Lee Sin",
            "seed_mid": "Ahri",
            "archetype": "custom",
            "cw_synergy": "100",
            "cw_scaling": "0",
            "cw_cc": "0",
            "cw_gold": "0",
            "cw_drakes": "0",
            "cw_soul": "0",
        },
    )
    assert resp.status_code == 200
    assert '<h3 class="draft-suggest-label">Personnalisé</h3>' in resp.text
    for name in ("Vi", "Lee Sin", "Ahri", "Orianna", "Thresh"):
        assert name in resp.text
    # Indépendance des 2 formulaires (retour utilisateur 2026-07-28) : seuls
    # `cw_*` ont été soumis, jamais `w_*` — "Personnalise tes poids" (5e
    # archétype auto-suggéré) ne doit ni afficher de carte, ni d'erreur.
    assert '<div class="draft-custom-result">' not in resp.text
    assert "La somme des poids doit faire 100" not in resp.text


def test_draft_page_compose_custom_archetype_without_weights_shows_error(pg_sync, client):
    """ "Personnalisé" choisi sans avoir rempli les poids : message explicite
    plutôt qu'un plantage ou une complétion silencieuse avec des poids
    vides (retour utilisateur 2026-07-28)."""
    _seed_suggest_scenario(pg_sync)
    resp = client.get(
        "/draft", params={"seed_jgl": "Lee Sin", "seed_mid": "Ahri", "archetype": "custom"}
    )
    assert resp.status_code == 200
    assert "Renseigne des poids qui totalisent 100 % ci-dessus." in resp.text


def test_draft_page_suggest_shows_counters(pg_sync, client):
    """Contres 1v1 (retour utilisateur 2026-07-25) : le rôle le plus
    exploitable de la composition affiche ses contres — toujours du 1v1 par
    rôle (`score_matchup`), jamais un contre de la draft entière (Phase 4,
    abandonné le 2026-07-19, cf. CLAUDE.md)."""
    _seed_suggest_scenario(
        pg_sync
    )  # complète en jgl=Lee Sin(1) mid=Ahri(2) sup=Thresh(3) top=Vi(4) bot=Orianna(5)
    pg_sync.execute(
        "INSERT INTO score_matchup (window_label, platform, role, champ_a, champ_b, games,"
        " games_eff, wr, delta_raw, delta, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 'JUNGLE', 6, 1, 100, 100.0, 0.60, 0.10, 0.10, 0.05, 0.15,"
        " 'eleve')"
    )
    resp = client.get("/draft", params={"suggest": "1"})
    assert resp.status_code == 200
    # Nomme le champion PRÉCIS de la composition (Lee Sin en jungle), pas
    # juste "le rôle jungle" dans l'abstrait, et dit explicitement que c'est
    # un point FAIBLE (retour utilisateur 2026-07-26 : la 1ère formulation
    # "contre Lee Sin" ne disait pas si la draft était forte ou faible).
    assert "Lee Sin puni(e) par" in resp.text
    assert "Leona" in resp.text
    assert "+10.0 %" in resp.text


def test_draft_page_suggest_shows_strengths_and_weights(pg_sync, client):
    """Points FORTS (retour utilisateur 2026-07-26, "contre qui cette
    composition est forte ?") : symétrique des points faibles, matchup
    inverse (`champ_a` = notre champion). Poids de l'archétype affichés sur
    la carte (même retour) — "Meilleure synergie" pèse 100 % sur la
    synergie."""
    _seed_suggest_scenario(pg_sync)  # jgl=Lee Sin(1) mid=Ahri(2) ...
    pg_sync.execute(
        "INSERT INTO score_matchup (window_label, platform, role, champ_a, champ_b, games,"
        " games_eff, wr, delta_raw, delta, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 'JUNGLE', 1, 7, 100, 100.0, 0.65, 0.12, 0.12, 0.07, 0.17,"
        " 'eleve')"
    )
    resp = client.get("/draft", params={"suggest": "1"})
    assert resp.status_code == 200
    assert "Points forts" in resp.text
    assert "Lee Sin domine" in resp.text
    assert "Zed" in resp.text
    assert "+12.0 %" in resp.text
    assert "Synergie 100 %" in resp.text


def test_draft_page_suggest_no_counters_shows_message(pg_sync, client):
    """Aucune ligne `score_matchup` du tout : pas de contre notable nulle
    part, message explicite plutôt qu'une section vide/plantée."""
    _seed_suggest_scenario(pg_sync)
    resp = client.get("/draft", params={"suggest": "1"})
    assert resp.status_code == 200
    assert "Aucun contre 1v1 notable." in resp.text


def test_draft_page_compose_from_champions_completes_and_shows_reliability(pg_sync, client):
    """ "Compose à partir de tes champions" (retour utilisateur 2026-07-25) :
    part de 1-2 champions choisis à la main (pas d'un duo auto-suggéré),
    complète avec le même algorithme, et affiche la fiabilité de la paire
    de départ tout comme les compositions auto-suggérées."""
    _seed_suggest_scenario(pg_sync)
    resp = client.get(
        "/draft", params={"seed_jgl": "Lee Sin", "seed_mid": "Ahri", "archetype": "synergy"}
    )
    assert resp.status_code == 200
    assert "Meilleure synergie" in resp.text
    for name in ("Vi", "Lee Sin", "Ahri", "Orianna", "Thresh"):
        assert name in resp.text
    assert "+51.0 %" in resp.text
    assert "Lee Sin + Ahri" in resp.text
    assert "+30.0 %, eleve (500)" in resp.text


def test_draft_page_compose_without_archetype_proposes_one_per_archetype(pg_sync, client):
    """Archétype non précisé (retour utilisateur 2026-07-26, "il faudrait
    aussi pouvoir ne pas préciser l'archétype et que ça donne une
    proposition par archétype") : une composition par archétype réussi,
    comme "Compositions suggérées" — pas obligé de choisir à l'avance."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    # Toutes les paires (index : 1=jgl, 2=mid, 3=sup, 4=top, 5=bot) portent
    # les mêmes scaling/cc/gold/drakes/âme/portée/winrate : les 6 archétypes
    # doivent pouvoir compléter (seule la synergie discrimine encore leur
    # classement interne).
    rows = (
        ("jgl_mid", 1, 2, 0.30),
        ("jgl_sup", 1, 3, 0.05),
        ("mid_sup", 2, 3, 0.04),
        ("top_jgl", 4, 1, 0.02),
        ("top_mid", 4, 2, 0.02),
        ("top_sup", 4, 3, 0.03),
        ("jgl_bot", 1, 5, 0.01),
        ("mid_bot", 2, 5, 0.01),
        ("bot_sup", 5, 3, 0.01),
        ("top_bot", 4, 5, 0.02),
    )
    for roles, champ_a, champ_b, synergy in rows:
        pg_sync.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct,"
            " gold_diff_15, drakes, soul_rate, range_theoretical_pct)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, 500, 500.0, 0.55, %s, 0.0, %s, 'eleve',"
            " 0.05, 20.0, 100.0, 0.02, 0.10, 40.0)",
            (roles, champ_a, champ_b, synergy, synergy),
        )
    resp = client.get("/draft", params={"seed_jgl": "Lee Sin", "seed_mid": "Ahri"})
    assert resp.status_code == 200
    assert "Meilleure synergie" in resp.text
    assert "Meilleur winrate" in resp.text
    assert "Scaling / fin de partie" in resp.text
    assert "Avantage early / lane" in resp.text
    assert "Contrôle des objectifs" in resp.text
    assert "Poke / zone" in resp.text
    assert resp.text.count("draft-suggest-card") == 6


def test_draft_page_compose_shows_no_data_for_unplayed_pair(pg_sync, client):
    """Une paire choisie à la main jamais jouée ensemble (pas de ligne
    `score_duo`) : jamais bloquant (retour utilisateur 2026-07-25), sa
    fiabilité s'affiche honnêtement comme "aucune donnée", le reste de la
    draft se complète quand même à partir des autres paires disponibles."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    # Toutes les paires structurelles SAUF top_sup (Vi + Thresh, le duo de
    # départ choisi à la main) : leur paire n'a donc aucune ligne score_duo.
    rows = (
        ("jgl_mid", 1, 2, 0.30),
        ("jgl_sup", 1, 3, 0.05),
        ("mid_sup", 2, 3, 0.04),
        ("top_jgl", 4, 1, 0.02),
        ("top_mid", 4, 2, 0.02),
        ("jgl_bot", 1, 5, 0.01),
        ("mid_bot", 2, 5, 0.01),
        ("bot_sup", 5, 3, 0.01),
        ("top_bot", 4, 5, 0.02),
    )
    for roles, champ_a, champ_b, synergy in rows:
        pg_sync.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier)"
            " VALUES ('16.13', 'euw1', %s, %s, %s, 500, 500.0, 0.55, %s, 0.0, %s, 'eleve')",
            (roles, champ_a, champ_b, synergy, synergy),
        )
    resp = client.get(
        "/draft", params={"seed_top": "Vi", "seed_sup": "Thresh", "archetype": "synergy"}
    )
    assert resp.status_code == 200
    assert "Vi + Thresh" in resp.text
    assert "aucune donnée" in resp.text
    for name in ("Vi", "Lee Sin", "Ahri", "Orianna", "Thresh"):
        assert name in resp.text


def test_draft_page_compose_requires_at_least_one_champion(pg_sync, client):
    """Un archétype choisi sans aucun champion : message explicite plutôt
    que de tenter de compléter un draft sans aucune donnée d'ancrage
    (`_sum_synergy` ne peut rien couvrir sans au moins 1 champion posé)."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    resp = client.get("/draft", params={"archetype": "synergy"})
    assert resp.status_code == 200
    assert "Choisis au moins 1 champion" in resp.text


def test_draft_page_compose_rejects_unknown_archetype(pg_sync, client):
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    resp = client.get("/draft", params={"seed_top": "Vi", "archetype": "not-a-real-archetype"})
    assert resp.status_code == 404


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
    # herald_taken/soul_taken/first_tower retirés le 2026-07-24 (résultats de
    # fin de partie, pas bornés à 15 min) : team_vision_per_min à la place.
    rows = (
        ("all", "team_vision_per_min", 2.1, 8.2),
        ("all", "team_gold_diff_15", 0.96, 2.6),
        ("all", "jgl_cs_diff_15", 0.05, 1.05),
        ("all", "intercept", -0.9, 0.4),
        ("behind_gold15", "team_vision_per_min", 2.3, 10.0),
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
    assert "équipe / min" in resp.text  # "Vision d'équipe / min", apostrophe échappée en HTML
    # team_gold_diff_15 doit apparaître avant team_vision_per_min : l'ordre
    # suit FEATURES, pas la valeur du coefficient.
    assert resp.text.index("Avantage gold") < resp.text.index("équipe / min")
    # Conversion en probabilité absolue (retour utilisateur 2026-07-19),
    # seul format affiché depuis le retrait du ×N (retour utilisateur
    # 2026-07-24, jugé peu lisible) : sigmoid(intercept) →
    # sigmoid(intercept + coef) pour 'all'/team_vision_per_min
    # (intercept=-0.9, coef=2.1).
    assert "29 % → 77 %" in resp.text
    # jgl_cs_diff_15 n'a une valeur QUE pour 'all' : la ligne existe quand
    # même (alignement garanti, pas de ligne manquante), valeur affichée
    # (intercept=-0.9, coef=0.05).
    assert "29 % → 30 %" in resp.text


def test_insights_page_shows_win_factors_holdout_auc(pg_sync, client):
    """AUC hors-échantillon (`_auc_test`, retour utilisateur 2026-07-24) :
    affichée sur la page, jamais dans le tableau de coefficients (feature
    spéciale, exclue de WIN_FACTOR_FEATURES comme _r2_draft_only/_r2_full
    pour gold_factors)."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    rows = (
        ("all", "intercept", -0.9, 0.4, 1000),
        ("all", "team_gold_diff_15", 0.96, 2.6, 1000),
        ("all", "_auc_test", 0.821, 0.821, 200),
        ("behind_gold15", "intercept", -0.5, 0.6, 500),
        ("behind_gold15", "_auc_test", 0.734, 0.734, 100),
    )
    for population, feature, coef, odds, n in rows:
        pg_sync.execute(
            "INSERT INTO score_win_factors (window_label, population, feature, coef,"
            " odds_ratio, n) VALUES ('16.13', %s, %s, %s, %s, %s)",
            (population, feature, coef, odds, n),
        )
    resp = client.get("/insights")
    assert resp.status_code == 200
    assert "Fiabilité du modèle" in resp.text
    assert "0.821" in resp.text
    assert "0.734" in resp.text
    # jamais dans le tableau de coefficients (pas une vraie feature).
    assert "_auc_test" not in resp.text
    # n affiché pour 'all' doit être celui de l'ajustement complet (1000),
    # pas celui du jeu de test de l'AUC (200) — rows[0] n'est pas fiable
    # (pas d'ORDER BY, et le diagnostic a un n différent depuis 2026-07-24).
    assert "1000" in resp.text


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
    # (écart large) — sous le seuil de fiabilité (games < 30), pour vérifier
    # qu'il est exclu plutôt que grisé (retour utilisateur 2026-07-20).
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
    # n'apparaît plus du tout dans la page (exclu, pas juste grisé).
    assert "Vi" not in resp.text

    # Facteur inconnu / rôle inconnu : 404, pas un crash silencieux.
    assert client.get("/resilience", params={"factor": "inconnu"}).status_code == 404
    assert (
        client.get(
            "/resilience", params={"factor": "team_gold_diff_15", "role": "INVALID"}
        ).status_code
        == 404
    )

    # role="" (pas absent) : c'est ce qu'envoie <select name="role"> quand
    # "tous" est sélectionné et que le formulaire est soumis — régression
    # constatée en prod le 20/07/2026, "0 champions" dès le 1er clic sur
    # Filtrer (avant même de toucher un filtre), causé par un `AND role = ''`
    # ajouté silencieusement côté SQL.
    resp = client.get("/resilience", params={"factor": "team_gold_diff_15", "role": ""})
    assert "Lee Sin" in resp.text


def test_resilience_page_min_max_filters(pg_sync, client):
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    # Lee Sin (champ 1) : résilient, écart faible (70 % vs 60 %, gap 10 %).
    # Vi (champ 4) : dépendant de l'avance, écart large (90 % vs 20 %, gap 70 %).
    pg_sync.execute(
        "INSERT INTO score_champion_resilience (window_label, role, champion_id, factor,"
        " games_ahead, wins_ahead, games_behind, wins_behind)"
        " VALUES ('16.13', 'JUNGLE', 1, 'team_gold_diff_15', 100, 70, 100, 60),"
        "        ('16.13', 'JUNGLE', 4, 'team_gold_diff_15', 100, 90, 100, 20)"
    )
    # max_gap=30 : ne garde que Lee Sin (gap 10 %), exclut Vi (gap 70 %).
    resp = client.get("/resilience", params={"factor": "team_gold_diff_15", "max_gap": "30"})
    assert resp.status_code == 200
    assert "Lee Sin" in resp.text
    assert "Vi" not in resp.text

    # min_wr_behind=50 : ne garde que Lee Sin (WR en retard 60 %), exclut Vi (20 %).
    resp = client.get("/resilience", params={"factor": "team_gold_diff_15", "min_wr_behind": "50"})
    assert "Lee Sin" in resp.text
    assert "Vi" not in resp.text

    # min_games=201 : dépasse le total des 2 (200 games chacun) — page vide,
    # mais message "filtres trop stricts" (pas "pas encore calculé", retour
    # utilisateur 2026-07-20 : les 2 champions SONT fiables, juste hors de la
    # plage demandée — pointer vers la commande de matérialisation serait
    # trompeur puisque la donnée existe déjà).
    resp = client.get("/resilience", params={"factor": "team_gold_diff_15", "min_games": "201"})
    assert "Lee Sin" not in resp.text
    assert "Vi" not in resp.text
    assert "Aucun champion ne correspond à ces filtres" in resp.text
    assert "Rien à afficher" not in resp.text


def test_resilience_page_shows_materialization_hint_only_without_any_reliable_data(pg_sync, client):
    """Sans aucune ligne fiable du tout (rien de matérialisé, ou pas assez de
    games des 2 côtés partout) : le message pointe vers la commande de
    matérialisation — distinct du cas « filtres trop stricts » ci-dessus.
    Fenêtre matérialisée (score_trio) mais AUCUNE ligne de résilience semée."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    resp = client.get("/resilience", params={"factor": "team_gold_diff_15"})
    assert "Rien à afficher" in resp.text
    assert "Aucun champion ne correspond à ces filtres" not in resp.text


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
    # ratio = 5200/4800 ≈ 1.08 → écart signé +8 % (retour utilisateur
    # 2026-07-20 : ×1.08 remplacé par un écart % coloré, plus parlant).
    assert "+8 %" in resp.text
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


def test_flex_page_wr_column_and_sortable_headers(pg_sync, client):
    """WR du rôle secondaire + colonnes triables (retour utilisateur
    2026-07-20). Lee Sin (1, Top principal), Vi (4, Jungle principal) et
    Thresh (3, Jungle principal) ont tous Support en secondaire — Thresh en
    dessous de la moyenne (dev négative), les 2 autres au-dessus, pour
    vérifier que le tri respecte le SIGNE (pas juste la magnitude). Ahri (2,
    Support principal) sert seulement d'ancre de baseline (exclue des
    picks, son propre rôle secondaire n'existe pas ici)."""
    pg_sync.execute(
        "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
        " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
        " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', 1, 2, 3, 1, 1.0, 1.0, 0.0, 0.0,"
        " 0.0, 0.0, 1.0, 'faible')"
    )
    pg_sync.execute(
        "INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins) VALUES"
        " ('16.13', 'euw1', 'TOP', 1, 300, 150),"
        " ('16.13', 'euw1', 'UTILITY', 1, 150, 70),"  # Lee Sin support : WR 46.7 %
        " ('16.13', 'euw1', 'JUNGLE', 4, 300, 150),"
        " ('16.13', 'euw1', 'UTILITY', 4, 150, 140),"  # Vi support : WR 93.3 %
        " ('16.13', 'euw1', 'JUNGLE', 3, 300, 150),"
        " ('16.13', 'euw1', 'UTILITY', 3, 150, 60),"  # Thresh support : WR 40 %
        " ('16.13', 'euw1', 'UTILITY', 2, 500, 250)"  # Ahri : ancre baseline, exclue des picks
    )
    # gold_15/dmg_per_gold en Support, baseline = (6000+5500+3800+4000)/4 =
    # 4825 : Lee Sin dev +24 %, Vi dev +14 % (tous deux AU-DESSUS), Thresh
    # dev -21 % (EN DESSOUS) — un tri par magnitude (bug initial) classerait
    # Thresh avant Vi en décroissant ; un tri par signe (attendu) le classe
    # dernier.
    for champ_id, gold_15, dmg_per_gold, count in (
        (1, 6000, 2.0, 40),
        (4, 5500, 1.0, 40),
        (3, 3800, 1.5, 40),
        (2, 4000, 1.5, 40),
    ):
        for i in range(count):
            match_id = f"SORT_{champ_id}_{i}"
            pg_sync.execute(
                "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
                " game_creation, game_duration_s, winning_team)"
                " VALUES (%s, 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)",
                (match_id,),
            )
            pg_sync.execute(
                "INSERT INTO match_role_stats (match_id, team_id, role, champion_id, win,"
                " gold_15, dmg_per_gold) VALUES (%s, 100, 'UTILITY', %s, true, %s, %s)",
                (match_id, champ_id, gold_15, dmg_per_gold),
            )
    resp = client.get("/flex")
    assert resp.status_code == 200
    # WR rôle secondaire affiché (47 % Lee Sin, 93 % Vi, arrondis) avec
    # l'écart signé vs la moyenne du rôle.
    assert "47 %" in resp.text
    assert "93 %" in resp.text
    # Dégâts/gold vs moyenne : Lee Sin au-dessus (+33 %), Vi en dessous (-33 %).
    assert "+33 %" in resp.text
    assert "-33 %" in resp.text
    # Tri par défaut (deviation desc, SIGNÉ) : Lee Sin (+24 %) > Vi (+14 %)
    # > Thresh (-21 %) — Thresh DERNIER malgré une magnitude plus grande que
    # Vi (21 % > 14 %), ce qu'un tri par abs() aurait inversé.
    idx_lee, idx_vi, idx_thresh = (resp.text.index(n) for n in ("Lee Sin", "Vi", "Thresh"))
    assert idx_lee < idx_vi < idx_thresh
    # Tri croissant : Thresh (le plus négatif) en premier, Lee Sin en dernier.
    resp_asc = client.get("/flex", params={"sort": "deviation", "dir": "asc"})
    idx_lee, idx_vi, idx_thresh = (resp_asc.text.index(n) for n in ("Lee Sin", "Vi", "Thresh"))
    assert idx_thresh < idx_vi < idx_lee
    # Trier par WR croissant : Lee Sin (WR plus faible) doit passer avant Vi.
    resp_wr_asc = client.get("/flex", params={"sort": "wr_secondary", "dir": "asc"})
    assert resp_wr_asc.text.index("Lee Sin") < resp_wr_asc.text.index("Vi")
    # Trier par WR décroissant : Vi (WR plus haut) doit passer avant Lee Sin.
    resp_wr_desc = client.get("/flex", params={"sort": "wr_secondary", "dir": "desc"})
    assert resp_wr_desc.text.index("Vi") < resp_wr_desc.text.index("Lee Sin")
