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
    """Deux matchs du trio (1,2,3) : une win courte gold +1000@10, une loss longue −400@10."""
    for match_id, duration, win, gold_10, vision in (
        ("EUW1_W1", 1500, True, 1000, 90),
        ("EUW1_L1", 2100, False, -400, 70),
    ):
        conn.execute(
            "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
            " game_creation, game_duration_s, winning_team)"
            " VALUES (%s, 'euw1', '16.13', '16.13.1', 420, now(), %s, 100)",
            (match_id, duration),
        )
        conn.execute(
            "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
            " sup_champion, win, gold_diff_10, herald_taken, soul_taken, vision_score)"
            " VALUES (%s, 100, 1, 2, 3, %s, %s, %s, false, %s)",
            (match_id, win, gold_10, win, vision),
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


def test_html_pages_render(pg_sync, client):
    _seed_scores(pg_sync)
    _seed_matches(pg_sync)
    home = client.get("/")
    assert home.status_code == 200
    assert "Lee Sin" in home.text
    detail = client.get("/trio/1/2/3")
    assert detail.status_code == 200
    assert "Nocturne" in detail.text
    duos = client.get("/duos")
    assert duos.status_code == 200
    assert "Ahri" in duos.text


def test_unknown_window_and_trio_are_404(pg_sync, client):
    _seed_scores(pg_sync)
    assert client.get("/api/trios", params={"window": "15.01"}).status_code == 404
    assert client.get("/api/trios/9/9/9").status_code == 404


def test_no_scores_yields_503(pg_sync, client):
    assert client.get("/api/trios").status_code == 503
