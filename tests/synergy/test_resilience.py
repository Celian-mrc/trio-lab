"""Tests unitaires (sans DB) de synergy/resilience.py — agrégation avance/retard."""

from __future__ import annotations

from trio_lab.synergy import resilience


def test_is_ahead_handles_booleans_and_continuous_diffs():
    assert resilience._is_ahead(True) is True
    assert resilience._is_ahead(False) is False
    assert resilience._is_ahead(0) is True  # égalité comptée comme "en avance"
    assert resilience._is_ahead(150) is True
    assert resilience._is_ahead(-1) is False


def test_aggregate_splits_games_and_wins_per_factor_and_champion():
    rows = [
        # Nasus (75) JUNGLE : 2 games en retard au gold (1 win, 1 loss, écart
        # franc au-delà de la zone neutre ±1000), 1 en avance (1 win) —
        # reproduit le pattern "tolère le retard".
        {
            "role": "JUNGLE",
            "champion_id": 75,
            "win": True,
            "team_gold_diff_15": -1500,
            "jgl_cs_diff_15": 3,
            "first_blood_team": True,
        },
        {
            "role": "JUNGLE",
            "champion_id": 75,
            "win": False,
            "team_gold_diff_15": -1300,
            "jgl_cs_diff_15": -2,
            "first_blood_team": False,
        },
        {
            "role": "JUNGLE",
            "champion_id": 75,
            "win": True,
            "team_gold_diff_15": 1800,
            "jgl_cs_diff_15": 5,
            "first_blood_team": True,
        },
    ]
    aggregated = resilience._aggregate(rows)

    by_factor = {a["factor"]: a for a in aggregated if a["champion_id"] == 75}
    assert set(by_factor) == set(resilience.FACTORS)

    gold = by_factor["team_gold_diff_15"]
    assert (gold["games_behind"], gold["wins_behind"]) == (2, 1)
    assert (gold["games_ahead"], gold["wins_ahead"]) == (1, 1)

    fb = by_factor["first_blood_team"]
    assert (fb["games_ahead"], fb["wins_ahead"]) == (2, 2)  # 2 games avec 1er sang, 2 wins
    assert (fb["games_behind"], fb["wins_behind"]) == (1, 0)


def test_aggregate_excludes_gold_within_neutral_zone():
    """Zone neutre ±1000 gold (retour utilisateur 2026-07-20) : un écart de
    -50 ou +200 est trop faible pour compter comme avance ou retard — ignoré
    pour team_gold_diff_15 (mais PAS pour jgl_cs_diff_15/first_blood_team,
    pas de zone neutre définie pour eux)."""
    rows = [
        {
            "role": "JUNGLE",
            "champion_id": 75,
            "win": True,
            "team_gold_diff_15": -50,  # dans la zone neutre : ignoré pour ce facteur
            "jgl_cs_diff_15": 3,  # pas de zone neutre : compté normalement
            "first_blood_team": True,
        },
        {
            "role": "JUNGLE",
            "champion_id": 75,
            "win": False,
            "team_gold_diff_15": 999,  # tout juste dans la zone neutre (< 1000)
            "jgl_cs_diff_15": -2,
            "first_blood_team": False,
        },
    ]
    aggregated = resilience._aggregate(rows)
    by_factor = {a["factor"]: a for a in aggregated if a["champion_id"] == 75}

    # Les 2 games sont dans la zone neutre pour team_gold_diff_15 : aucun
    # bucket n'est même créé pour ce facteur (pas de division par 0 en aval).
    assert "team_gold_diff_15" not in by_factor

    cs = by_factor["jgl_cs_diff_15"]
    assert (cs["games_ahead"], cs["games_behind"]) == (1, 1)
