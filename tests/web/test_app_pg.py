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
from trio_lab.ccref import score as ccref_score
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
    64: Champion(64, "Nocturne", ""),
}


@pytest.fixture
def pg_sync():
    """Connexion sync au Postgres de test, migrations appliquées, tables tronquées."""
    db.apply_migrations(TEST_DSN)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE players, matches, match_fetch_journal,"
            " agg_champion, agg_duo, agg_trio, agg_trio_vs_champion,"
            " score_duo, score_trio, score_trio_vs_champion CASCADE"
        )
        yield conn


@pytest.fixture
def client():
    app = create_app(dsn=TEST_DSN, champion_index=_INDEX)
    with TestClient(app) as test_client:
        yield test_client


def _seed_scores(conn) -> None:
    """Deux trios scorés sur euw1/16.13 : (1,2,3) synergie +.05, (4,5,6) −.02."""
    for jgl, mid, sup, games, wr, syn in ((1, 2, 3, 40, 0.60, 0.05), (4, 5, 6, 80, 0.48, -0.02)):
        conn.execute(
            "INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,"
            " sup_champion, games, games_eff, wr, synergy_raw, synergy_pred, synergy,"
            " ci_low, ci_high, tier) VALUES ('16.13', 'euw1', %s, %s, %s, %s, %s, %s,"
            " %s, 0.0, %s, 0.3, 0.8, 'faible')",
            (jgl, mid, sup, games, float(games), wr, syn, syn),
        )
    conn.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 'jgl_mid', 1, 2, 60, 60.0, 0.58, 0.03, 0.4, 0.7, 'moyen')"
    )
    conn.execute(
        "INSERT INTO score_trio_vs_champion (window_label, platform, jgl_champion,"
        " mid_champion, sup_champion, enemy_role, enemy_champion, games, games_eff, wr,"
        " delta_raw, delta, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'euw1', 1, 2, 3, 'JUNGLE', 64, 12, 12.0, 0.35,"
        " -0.25, -0.014, 0.1, 0.6, 'faible')"
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
    payload = client.get("/api/trios/1/2/3").json()
    assert payload["score"]["wr"] == pytest.approx(0.60)
    stats = payload["stats"]
    assert stats["games"] == 2
    assert stats["wr"] == pytest.approx(0.5)
    assert stats["gold_diff"]["10"] == pytest.approx(300.0)  # (1000 − 400) / 2
    assert stats["herald_taken"] == pytest.approx(0.5)
    assert stats["wr_without_soul"] == pytest.approx(0.5)  # les 2 parties sans âme
    assert stats["vision_score"] == pytest.approx(80.0)
    assert stats["avg_duration_win_s"] == pytest.approx(1500.0)
    assert stats["avg_duration_loss_s"] == pytest.approx(2100.0)
    assert payload["duos"][0]["champ_a_name"] == "Lee Sin"
    worst = payload["counters_worst"][0]
    assert (worst["enemy_champion_name"], worst["enemy_role"]) == ("Nocturne", "JUNGLE")
    assert worst["delta"] == pytest.approx(-0.014)
    # Score CC théorique (Phase 2b, complément de l'empirique) : les 3 noms de
    # la fixture existent dans le fichier gelé, donc résolus + trio = somme.
    cc = payload["cc_theoretical"]
    assert cc["jgl"] is not None and cc["mid"] is not None and cc["sup"] is not None
    assert cc["trio"] == pytest.approx(cc["jgl"] + cc["mid"] + cc["sup"])

    # Scores CC normalisés sur 100 : empirique (100+140)/2 = 120 s, plafond 240 s.
    cc_scores = payload["cc_scores"]
    assert cc_scores["empirical_pct"] == pytest.approx(50.0)
    expected_theo_pct = ccref_score.theoretical_pct(cc["trio"])
    assert cc_scores["theoretical_pct"] == pytest.approx(expected_theo_pct)
    # games_eff=40 (seed), k=200 par défaut : le mélange penche vers le théorique.
    expected_blend = ccref_score.blended_pct(50.0, expected_theo_pct, games_eff=40.0)
    assert cc_scores["blended_pct"] == pytest.approx(expected_blend)
    assert 0.0 <= cc_scores["blended_pct"] <= 100.0


def test_html_pages_render(pg_sync, client):
    _seed_scores(pg_sync)
    _seed_matches(pg_sync)
    home = client.get("/")
    assert home.status_code == 200
    assert "Lee Sin" in home.text
    detail = client.get("/trio/1/2/3")
    assert detail.status_code == 200
    assert "Nocturne" in detail.text
    assert "CC théorique" in detail.text
    assert "Mélangé (lissé par le volume)" in detail.text
    duos = client.get("/duos")
    assert duos.status_code == 200
    assert "Ahri" in duos.text


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
