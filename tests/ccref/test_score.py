"""Tests du score CC théorique (poids, coefs, règle du max) et des corrélations."""

from __future__ import annotations

import pytest

from trio_lab.ccref import score, validate


def _row(
    type_cc,
    duree="1.0",
    pct="",
    zone="mono",
    fiab="skillshot",
    dispo="base",
    repos="0",
    cond="0",
    champion="X",
    sort="Q",
):
    return {
        "champion": champion,
        "sort": sort,
        "type_cc": type_cc,
        "duree_s": duree,
        "pct_slow": pct,
        "zone": zone,
        "fiabilite": fiab,
        "disponibilite": dispo,
        "repositionnement": repos,
        "conditionnel": cond,
        "note_relecture": "",
    }


# --- row_contribution : formule et coefficients ---


def test_contribution_base_stun():
    assert score.row_contribution(_row("stun", duree="2.0")) == pytest.approx(1.8)  # 0.9 × 2


def test_contribution_all_coefficients():
    # airborne 1.0 × 1 s × zone 1.5 × point_click 1.2 × ultimate 0.5
    # × repositionnement 1.15 × conditionnel 0.7 = 0.7245
    row = _row("airborne", zone="multi", fiab="point_click", dispo="ultimate", repos="1", cond="1")
    assert score.row_contribution(row) == pytest.approx(1.0 * 1.5 * 1.2 * 0.5 * 1.15 * 0.7)


def test_contribution_slow_scales_with_percentage():
    # 0.3 × 2 s × 60 % = 0.36
    assert score.row_contribution(_row("slow", duree="2.0", pct="60")) == pytest.approx(0.36)


def test_contribution_missing_duration_or_pct_is_zero():
    assert score.row_contribution(_row("stun", duree="")) == 0.0
    assert score.row_contribution(_row("slow", duree="2.0", pct="")) == 0.0


# --- spell_score : CC durs simultanés non cumulés, slows additifs ---


def test_spell_score_hard_cc_max_not_sum():
    # Q d'Alistar : knock-up 1 s (1.0) + stun 1 s (0.9) simultanés → max = 1.0.
    rows = [_row("airborne"), _row("stun")]
    assert score.spell_score(rows) == pytest.approx(1.0)


def test_spell_score_slow_adds_to_hard_cc():
    # stun 1 s (0.9) + slow 2 s à 50 % (0.3) → 1.2.
    rows = [_row("stun"), _row("slow", duree="2.0", pct="50")]
    assert score.spell_score(rows) == pytest.approx(0.9 + 0.3)


# --- champion_scores / trio_score ---


def test_champion_scores_sum_spells_and_group_by_spell():
    rows = [
        _row("airborne", champion="Alistar", sort="Q"),  # 1.0
        _row("stun", champion="Alistar", sort="Q"),  # simultané → absorbé
        _row("stun", duree="0.75", champion="Alistar", sort="W"),  # 0.675
        _row("stun", duree="1.0", champion="Leona", sort="Q"),  # 0.9
    ]
    scores = score.champion_scores(rows)
    assert scores["Alistar"] == pytest.approx(1.0 + 0.675)
    assert scores["Leona"] == pytest.approx(0.9)
    assert score.trio_score(("Alistar", "Leona", "Inconnu"), scores) == pytest.approx(2.575)


def test_frozen_reference_loads_and_scores():
    """Le fichier gelé se charge et produit un score plausible pour tout champion."""
    rows = score.load_reference()
    assert len(rows) > 400
    scores = score.champion_scores(rows)
    assert len(scores) > 150
    assert all(v >= 0 for v in scores.values())
    # Leona (kit très CC) doit dominer un champion quasi sans CC (Zed : 1 slow).
    assert scores["Leona"] > scores["Zed"]


# --- normalisation 0-100 et mélange bayésien (théorique/empirique) ---


def test_theoretical_pct_scales_against_3x_max_champion():
    scores = {"Leona": 6.0, "Zed": 1.0, "Ahri": 2.0}
    # plafond = 3 × 6.0 = 18 ; trio (Leona + Ahri + Zed) = 9.0 → 50 %.
    assert score.theoretical_pct(9.0, scores) == pytest.approx(50.0)


def test_theoretical_pct_zero_ceiling_is_safe():
    assert score.theoretical_pct(0.0, {"X": 0.0}) == 0.0


def test_empirical_pct_scales_and_caps_at_100():
    assert score.empirical_pct(120.0, ceiling=240.0) == pytest.approx(50.0)
    assert score.empirical_pct(482.0, ceiling=240.0) == 100.0  # outlier plafonné
    assert score.empirical_pct(None) is None


def test_blended_pct_leans_theoretical_at_low_volume():
    # games_eff=0 : lissage pur vers le théorique, l'empirique est ignoré.
    assert score.blended_pct(empirical=90.0, theoretical=30.0, games_eff=0.0, k=200) == 30.0


def test_blended_pct_leans_empirical_at_high_volume():
    blended = score.blended_pct(empirical=90.0, theoretical=30.0, games_eff=100_000.0, k=200)
    assert blended == pytest.approx(90.0, abs=0.5)


def test_blended_pct_at_k_is_midpoint():
    # games_eff == k : moitié-moitié (propriété documentée de `smooth`).
    blended = score.blended_pct(empirical=80.0, theoretical=20.0, games_eff=200.0, k=200)
    assert blended == pytest.approx(50.0)


def test_blended_pct_falls_back_to_theoretical_without_empirical_data():
    assert score.blended_pct(empirical=None, theoretical=42.0, games_eff=500.0) == pytest.approx(
        42.0
    )


# --- corrélations (pur Python) ---


def test_pearson_known_values():
    assert validate.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert validate.pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)
    assert validate.pearson([1, 1, 1], [1, 2, 3]) == 0.0  # variance nulle


def test_spearman_is_rank_based():
    # Relation monotone non linéaire : Spearman = 1, Pearson < 1.
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [1.0, 10.0, 100.0, 1000.0]
    assert validate.spearman(xs, ys) == pytest.approx(1.0)
    assert validate.pearson(xs, ys) < 1.0


def test_ranks_handle_ties():
    assert validate._ranks([10.0, 20.0, 20.0, 30.0]) == [1.0, 2.5, 2.5, 4.0]
