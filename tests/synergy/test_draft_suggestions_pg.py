"""Tests d'intégration de synergy/draft_suggestions.py (compositions
suggérées + contres, matérialisation)."""

from __future__ import annotations

import psycopg
import pytest

from trio_lab import db
from trio_lab.synergy import draft_suggestions
from trio_lab.synergy.windows import make_window

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


@pytest.fixture
def pg_sync():
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


def _seed_scenario(conn) -> None:
    """Duo de départ jgl_mid (champ 1/2, synergie +30 %) étendu vers un
    draft complet à 5 (index : 1=jgl, 2=mid, 3=sup, 4=top, 5=bot) — même
    scénario déterministe que `_seed_suggest_scenario` côté web, plus un
    contre 1v1 notable (champion 6 contre le jungler, champion 1 — point
    FAIBLE) et un matchup notable dans l'autre sens (le jungler, champion 1,
    bat le champion 7 — point FORT, retour utilisateur 2026-07-26)."""
    conn.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'all', 'jgl_mid', 1, 2, 60, 60.0, 0.55, 0.30, 0.0, 0.30, 'eleve')"
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
            " VALUES ('16.13', 'all', %s, %s, %s, 60, 60.0, 0.55, %s, 0.0, %s, 'eleve')",
            (roles, champ_a, champ_b, synergy, synergy),
        )
    conn.execute(
        "INSERT INTO score_matchup (window_label, platform, role, champ_a, champ_b, games,"
        " games_eff, wr, delta_raw, delta, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'all', 'JUNGLE', 6, 1, 100, 100.0, 0.60, 0.10, 0.10, 0.05, 0.15,"
        " 'eleve'),"
        "        ('16.13', 'all', 'JUNGLE', 1, 7, 100, 100.0, 0.65, 0.12, 0.12, 0.07, 0.17,"
        " 'eleve')"
    )


def test_refresh_materializes_composition_and_counter(pg_sync):
    """`refresh` écrit la composition gagnante (ici "Meilleure synergie",
    seule archétype possible sans scaling/cc/gold/drakes renseignés) et son
    contre 1v1 dans les 2 tables — mêmes champions/synergie que le calcul en
    direct (`propose_drafts`), cf. tests web équivalents."""
    _seed_scenario(pg_sync)
    window = make_window(["16.13"])
    n = draft_suggestions.refresh(window, "all", dsn=TEST_DSN)
    assert n == 1

    with pg_sync.cursor() as cur:
        cur.execute(
            "SELECT archetype, label, top_champion, jgl_champion, mid_champion, bot_champion,"
            " sup_champion, total_synergy, seed_roles, seed_champ_a, seed_champ_b, seed_tier"
            " FROM draft_suggestion WHERE window_label = '16.13' AND platform = 'all'"
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        archetype, label, top, jgl, mid, bot, sup, total, seed_roles, seed_a, seed_b, seed_tier = (
            rows[0]
        )
        assert archetype == "synergy"
        assert label == "Meilleure synergie"
        assert (top, jgl, mid, bot, sup) == (4, 1, 2, 5, 3)
        assert total == pytest.approx(0.51, abs=1e-6)
        assert seed_roles == "jgl_mid"
        assert (seed_a, seed_b) == (1, 2)
        assert seed_tier == "eleve"

        cur.execute(
            "SELECT direction, kind, rank, role, against_champion, champion_id, delta"
            " FROM draft_suggestion_counter WHERE window_label = '16.13' AND platform = 'all'"
            " ORDER BY direction"
        )
        counters = cur.fetchall()
        assert counters == [
            ("strength", "primary", 0, "jgl", 1, 7, pytest.approx(0.12, abs=1e-6)),
            ("weakness", "primary", 0, "jgl", 1, 6, pytest.approx(0.10, abs=1e-6)),
        ]


def test_refresh_overwrites_stale_rows(pg_sync):
    """Un 2e `refresh` sur la même fenêtre/plateforme ne duplique rien (DELETE
    + INSERT, même raisonnement que resilience/win_factors/gold_factors)."""
    _seed_scenario(pg_sync)
    window = make_window(["16.13"])
    draft_suggestions.refresh(window, "all", dsn=TEST_DSN)
    draft_suggestions.refresh(window, "all", dsn=TEST_DSN)
    with pg_sync.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM draft_suggestion"
            " WHERE window_label = '16.13' AND platform = 'all'"
        )
        assert cur.fetchone()[0] == 1


def test_refresh_writes_nothing_when_no_archetype_completes(pg_sync):
    """Fenêtre sans aucun duo fiable : `refresh` n'écrit rien (0 archétype),
    pas d'erreur — même esprit que `resilience.refresh` sous le seuil."""
    window = make_window(["16.13"])
    n = draft_suggestions.refresh(window, "all", dsn=TEST_DSN)
    assert n == 0
    with pg_sync.cursor() as cur:
        cur.execute("SELECT count(*) FROM draft_suggestion")
        assert cur.fetchone()[0] == 0
