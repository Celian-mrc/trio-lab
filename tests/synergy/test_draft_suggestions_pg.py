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
    bat le champion 7 — point FORT, retour utilisateur 2026-07-26). wr/scaling/
    cc/gold identiques sur les 10 paires (retour utilisateur 2026-07-27,
    winrate + IC) pour que la moyenne du draft complet reste ces valeurs."""
    conn.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct, gold_diff_15)"
        " VALUES ('16.13', 'all', 'jgl_mid', 1, 2, 60, 60.0, 0.55, 0.30, 0.0, 0.30, 'eleve',"
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
        conn.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct, gold_diff_15)"
            " VALUES ('16.13', 'all', %s, %s, %s, 60, 60.0, 0.55, %s, 0.0, %s, 'eleve',"
            " 0.08, 70.0, 800.0)",
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
    seule archétype possible : `drakes` n'est pas renseigné, les 3 profils
    pondérés ne peuvent pas compléter) et son contre 1v1 dans les 2 tables —
    mêmes champions/synergie que le calcul en direct (`propose_drafts`), cf.
    tests web équivalents. Vérifie aussi le winrate + IC moyennés (retour
    utilisateur 2026-07-27) : wr=0.55 partout -> 0.55 ; ci_low=0.0 partout
    -> 0.0 ; ci_high = synergie de chaque paire, moyenne = 0.051."""
    _seed_scenario(pg_sync)
    window = make_window(["16.13"])
    n = draft_suggestions.refresh(window, "all", dsn=TEST_DSN)
    assert n == 1

    with pg_sync.cursor() as cur:
        cur.execute(
            "SELECT archetype, label, top_champion, jgl_champion, mid_champion, bot_champion,"
            " sup_champion, total_synergy, seed_roles, seed_champ_a, seed_champ_b, seed_tier,"
            " wr, wr_ci_low, wr_ci_high"
            " FROM draft_suggestion WHERE window_label = '16.13' AND platform = 'all'"
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        (
            archetype,
            label,
            top,
            jgl,
            mid,
            bot,
            sup,
            total,
            seed_roles,
            seed_a,
            seed_b,
            seed_tier,
            wr,
            wr_ci_low,
            wr_ci_high,
        ) = rows[0]
        assert archetype == "synergy"
        assert label == "Meilleure synergie"
        assert (top, jgl, mid, bot, sup) == (4, 1, 2, 5, 3)
        assert total == pytest.approx(0.51, abs=1e-6)
        assert seed_roles == "jgl_mid"
        assert (seed_a, seed_b) == (1, 2)
        assert seed_tier == "eleve"
        assert wr == pytest.approx(0.55, abs=1e-6)
        assert wr_ci_low == pytest.approx(0.0, abs=1e-6)
        assert wr_ci_high == pytest.approx(0.051, abs=1e-6)

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


def test_propose_drafts_uses_different_seed_duo_per_archetype(pg_sync):
    """Poids différents (retour utilisateur 2026-07-25) : la synergie brute
    dominante et le scaling dominant doivent justifier 2 duos de départ
    DIFFÉRENTS ("Meilleure synergie" part de jgl/mid, "Scaling" part de
    top/bot), même si le draft complet peut retomber sur le même univers
    fermé à 5 champions. `seed_pairs` n'est plus affiché sur "Compositions
    suggérées" (retour utilisateur 2026-07-27 : détail interne sans intérêt
    pour l'utilisateur une fois le total de synergie affiché, cf. web/app.py
    `_build_draft_result(include_seed_pairs=False)`) — vérifié ici au niveau
    de la fonction plutôt que par scraping HTML."""
    # Duo A (jgl_mid, champ 1/2) : synergie dominante (+30 %) mais scaling
    # négatif — gagne "Meilleure synergie", perd "Scaling".
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct, gold_diff_15,"
        " drakes, soul_rate)"
        " VALUES ('16.13', 'all', 'jgl_mid', 1, 2, 60, 60.0, 0.55, 0.30, 0.0, 0.30, 'eleve',"
        " -0.10, 20.0, 100.0, 0.02, 0.10)"
    )
    # Duo B (top_bot, champ 4/5) : synergie bien plus faible (+5 %) mais
    # scaling fortement positif — perd "Meilleure synergie", gagne "Scaling"
    # (poids `soul` désormais, retour utilisateur 2026-07-27 — cf.
    # ARCHETYPES["scaling"] — d'où soul_rate renseigné ici comme les autres
    # colonnes archétype, pour que ce duo reste éligible).
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier, scaling, cc_blended_pct, gold_diff_15,"
        " drakes, soul_rate)"
        " VALUES ('16.13', 'all', 'top_bot', 4, 5, 60, 60.0, 0.55, 0.05, 0.0, 0.05, 'eleve',"
        " 0.10, 20.0, 100.0, 0.02, 0.10)"
    )
    # Paires structurelles restantes : stats archétype neutres partout, pour
    # ne pas disqualifier un candidat faute de donnée.
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
            " VALUES ('16.13', 'all', %s, %s, %s, 60, 60.0, 0.55, %s, 0.0, %s, 'eleve',"
            " 0.0, 20.0, 100.0, 0.02, 0.10)",
            (roles, champ_a, champ_b, synergy, synergy),
        )
    pool, zstats = draft_suggestions.pool_and_zstats(pg_sync, "16.13", "all")
    results = draft_suggestions.propose_drafts(pg_sync, "16.13", "all", pool, zstats)
    by_archetype = {r["archetype"]: r for r in results}
    assert "synergy" in by_archetype
    assert "scaling" in by_archetype
    synergy_seed = by_archetype["synergy"]["seed_pairs"][0]
    scaling_seed = by_archetype["scaling"]["seed_pairs"][0]
    assert {synergy_seed["champ_a"], synergy_seed["champ_b"]} == {1, 2}
    assert {scaling_seed["champ_a"], scaling_seed["champ_b"]} == {4, 5}


def _insert_pentad(
    conn,
    base_id: int,
    seed_synergy: float,
    seed_games_eff: float,
    other_synergy: float = 0.01,
    other_games_eff: float = 60.0,
) -> None:
    """Insère les 10 paires d'un pentade FERMÉ (jgl/mid/sup/top/bot =
    base_id..base_id+4) — jamais de champion partagé avec un autre pentade
    (sert à tester la diversité : 0 champion en commun entre 2 pentades).
    Le duo jgl_mid (le "seed") a sa PROPRE synergie/`games_eff` ; les 9
    autres paires partagent `other_synergy`/`other_games_eff`."""
    jgl, mid, sup, top, bot = base_id, base_id + 1, base_id + 2, base_id + 3, base_id + 4
    rows = (
        ("jgl_mid", jgl, mid, seed_synergy, seed_games_eff),
        ("jgl_sup", jgl, sup, other_synergy, other_games_eff),
        ("mid_sup", mid, sup, other_synergy, other_games_eff),
        ("top_jgl", top, jgl, other_synergy, other_games_eff),
        ("top_mid", top, mid, other_synergy, other_games_eff),
        ("top_sup", top, sup, other_synergy, other_games_eff),
        ("jgl_bot", jgl, bot, other_synergy, other_games_eff),
        ("mid_bot", mid, bot, other_synergy, other_games_eff),
        ("bot_sup", bot, sup, other_synergy, other_games_eff),
        ("top_bot", top, bot, other_synergy, other_games_eff),
    )
    for roles, champ_a, champ_b, synergy, games_eff in rows:
        conn.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier)"
            " VALUES ('16.13', 'all', %s, %s, %s, 60, %s, 0.55, %s, 0.0, %s, 'eleve')",
            (roles, champ_a, champ_b, games_eff, synergy, synergy),
        )


def test_propose_drafts_third_variant_uses_reliability_of_the_whole_composition(pg_sync):
    """3e proposition = la composition la plus fiable sur ses 10 VRAIES
    paires, PAS seulement son duo de départ (retour utilisateur 2026-07-28 :
    "pourquoi pas tous les duos plutôt que juste le premier ?"). 4 pentades
    DISJOINTS : A (1-5) et B (6-10) gagnent les rangs 0/1 par score (poids
    de départ élevés, fiabilité uniforme et sans intérêt ici). Entre C
    (11-15) et D (16-20), candidats au rang 2 : C a un duo de départ
    ÉNORME (`games_eff`=5000) mais UNE autre paire (mid_sup) quasi jamais
    jouée (50) — son minimum sur les 10 paires n'est que 50. D a un duo de
    départ plus modeste (300) mais TOUTES ses 9 autres paires solides
    (2000) — son minimum (300) dépasse celui de C. Le rang 2 doit choisir
    D : preuve que la fiabilité est jugée sur la composition ENTIÈRE — avec
    l'ancien critère (seed seul), C aurait gagné (5000 > 300)."""
    _insert_pentad(pg_sync, 1, seed_synergy=0.30, seed_games_eff=1000.0, other_games_eff=1000.0)
    _insert_pentad(pg_sync, 6, seed_synergy=0.20, seed_games_eff=1000.0, other_games_eff=1000.0)
    # C (11-15) : duo de départ énorme, mais 1 paire (mid_sup) quasi jamais jouée.
    jgl, mid, sup, top, bot = 11, 12, 13, 14, 15
    c_rows = (
        ("jgl_mid", jgl, mid, 0.15, 5000.0),
        ("jgl_sup", jgl, sup, 0.01, 1000.0),
        ("mid_sup", mid, sup, 0.01, 50.0),  # le maillon faible de C
        ("top_jgl", top, jgl, 0.01, 1000.0),
        ("top_mid", top, mid, 0.01, 1000.0),
        ("top_sup", top, sup, 0.01, 1000.0),
        ("jgl_bot", jgl, bot, 0.01, 1000.0),
        ("mid_bot", mid, bot, 0.01, 1000.0),
        ("bot_sup", bot, sup, 0.01, 1000.0),
        ("top_bot", top, bot, 0.01, 1000.0),
    )
    for roles, champ_a, champ_b, synergy, games_eff in c_rows:
        pg_sync.execute(
            "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
            " games_eff, wr, synergy, ci_low, ci_high, tier)"
            " VALUES ('16.13', 'all', %s, %s, %s, 60, %s, 0.55, %s, 0.0, %s, 'eleve')",
            (roles, champ_a, champ_b, games_eff, synergy, synergy),
        )
    # D (16-20) : duo de départ modeste, mais TOUTES les autres paires solides.
    _insert_pentad(pg_sync, 16, seed_synergy=0.10, seed_games_eff=300.0, other_games_eff=2000.0)

    pool, zstats = draft_suggestions.pool_and_zstats(pg_sync, "16.13", "all")
    results = draft_suggestions.propose_drafts(pg_sync, "16.13", "all", pool, zstats)
    synergy_results = [r for r in results if r["archetype"] == "synergy"]
    assert len(synergy_results) == 3
    by_rank = {r["suggestion_rank"]: r for r in synergy_results}
    assert by_rank[0]["selection"] == "score"
    assert set(by_rank[0]["members"].values()) == {1, 2, 3, 4, 5}
    assert by_rank[1]["selection"] == "diverse"
    assert set(by_rank[1]["members"].values()) == {6, 7, 8, 9, 10}
    assert by_rank[2]["selection"] == "reliable"
    assert set(by_rank[2]["members"].values()) == {16, 17, 18, 19, 20}


def test_propose_drafts_never_forces_three_variants(pg_sync):
    """Un seul pentade disponible : 1 seule proposition, jamais un doublon
    forcé pour atteindre 3 (retour utilisateur implicite du design — cf.
    docstring `propose_drafts`)."""
    _insert_pentad(pg_sync, 1, seed_synergy=0.30, seed_games_eff=200.0)
    pool, zstats = draft_suggestions.pool_and_zstats(pg_sync, "16.13", "all")
    results = draft_suggestions.propose_drafts(pg_sync, "16.13", "all", pool, zstats)
    synergy_results = [r for r in results if r["archetype"] == "synergy"]
    assert len(synergy_results) == 1
    assert synergy_results[0]["suggestion_rank"] == 0
    assert synergy_results[0]["selection"] == "score"


# --- refine_draft : passage de remplacement post-construction (retour
# utilisateur 2026-07-27, "est-ce que le système essaie de remplacer le duo
# de base par un autre pour voir s'il n'y a pas une meilleure option ?") ---


def test_refine_draft_replaces_a_role_with_a_better_candidate(pg_sync):
    """Le champion 6 (alternative BOT) est strictement meilleur que le
    champion 5 (bot actuel) sur les 4 paires qui le concernent — un seul
    passage doit le repérer et l'utiliser à la place, le duo de départ
    (jgl/mid) compris dans les rôles éligibles au remplacement."""
    _seed_scenario(pg_sync)  # jgl=1 mid=2 sup=3 top=4 bot=5 (bot=5 sous-optimal)
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'all', 'jgl_bot', 1, 6, 60, 60.0, 0.55, 0.05, 0.0, 0.05, 'eleve'),"
        "        ('16.13', 'all', 'mid_bot', 2, 6, 60, 60.0, 0.55, 0.05, 0.0, 0.05, 'eleve'),"
        "        ('16.13', 'all', 'bot_sup', 6, 3, 60, 60.0, 0.55, 0.05, 0.0, 0.05, 'eleve'),"
        "        ('16.13', 'all', 'top_bot', 4, 6, 60, 60.0, 0.55, 0.05, 0.0, 0.05, 'eleve')"
    )
    weights = draft_suggestions.ARCHETYPES["synergy"]["weights"]  # {"synergy": 1.0}
    pool, zstats = draft_suggestions.pool_and_zstats(pg_sync, "16.13", "all")
    placed = {"top": 4, "jgl": 1, "mid": 2, "bot": 5, "sup": 3}
    # Vraie Σ synergie des 10 paires initiales (cf. _seed_scenario) : la
    # même valeur que dans test_refresh_materializes_composition_and_counter.
    total = 0.30 + 0.05 + 0.04 + 0.02 + 0.02 + 0.03 + 0.01 + 0.01 + 0.01 + 0.02
    refined_placed, refined_total = draft_suggestions.refine_draft(
        pg_sync, "16.13", "all", placed, total, "eleve", weights, zstats
    )
    assert refined_placed["bot"] == 6
    # top/jgl/mid/sup inchangés : aucun meilleur candidat pour eux ici.
    assert refined_placed["top"] == 4
    assert refined_placed["jgl"] == 1
    assert refined_placed["mid"] == 2
    assert refined_placed["sup"] == 3
    # Total ajusté exactement de la différence des 4 paires touchées :
    # nouvelles (0.05×4 = 0.20) − anciennes (0.01+0.01+0.01+0.02 = 0.05) = +0.15.
    assert refined_total == pytest.approx(total + 0.15, abs=1e-6)


def test_refine_draft_never_replaces_locked_roles(pg_sync):
    """ "Compose à partir de tes champions" verrouille les rôles choisis à la
    main (retour utilisateur : l'utilisateur les a choisis exprès) — même si
    un meilleur candidat existe pour ce rôle, `refine_draft` ne doit jamais
    le toucher quand il est dans `locked_roles`."""
    _seed_scenario(pg_sync)
    pg_sync.execute(
        "INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b, games,"
        " games_eff, wr, synergy, ci_low, ci_high, tier)"
        " VALUES ('16.13', 'all', 'jgl_bot', 1, 6, 60, 60.0, 0.55, 0.05, 0.0, 0.05, 'eleve'),"
        "        ('16.13', 'all', 'mid_bot', 2, 6, 60, 60.0, 0.55, 0.05, 0.0, 0.05, 'eleve'),"
        "        ('16.13', 'all', 'bot_sup', 6, 3, 60, 60.0, 0.55, 0.05, 0.0, 0.05, 'eleve'),"
        "        ('16.13', 'all', 'top_bot', 4, 6, 60, 60.0, 0.55, 0.05, 0.0, 0.05, 'eleve')"
    )
    weights = draft_suggestions.ARCHETYPES["synergy"]["weights"]
    pool, zstats = draft_suggestions.pool_and_zstats(pg_sync, "16.13", "all")
    placed = {"top": 4, "jgl": 1, "mid": 2, "bot": 5, "sup": 3}
    total = 0.30 + 0.05 + 0.04 + 0.02 + 0.02 + 0.03 + 0.01 + 0.01 + 0.01 + 0.02
    refined_placed, refined_total = draft_suggestions.refine_draft(
        pg_sync,
        "16.13",
        "all",
        placed,
        total,
        "eleve",
        weights,
        zstats,
        locked_roles=frozenset({"bot"}),
    )
    assert refined_placed["bot"] == 5  # jamais remplacé, malgré le champion 6 meilleur
    assert refined_placed == placed
    assert refined_total == pytest.approx(total, abs=1e-6)
