"""Tests des mathématiques de score (WR pondéré, Wilson, lissage bayésien)."""

from __future__ import annotations

import math

import pytest

from trio_lab.synergy import scores

# --- weighted_wr ---


def test_weighted_wr_single_patch():
    result = scores.weighted_wr([("16.13", 100, 60)], {"16.13": 1.0})
    assert result == scores.WeightedWR(wr=0.6, games=100, games_eff=100.0)


def test_weighted_wr_combines_patches_with_weights():
    result = scores.weighted_wr(
        [("16.13", 40, 24), ("16.12", 100, 40)], {"16.13": 1.0, "16.12": 0.6}
    )
    # games_eff = 40 + 60 ; wins_eff = 24 + 24 → wr = 48/100.
    assert result.games == 140
    assert result.games_eff == pytest.approx(100.0)
    assert result.wr == pytest.approx(0.48)


def test_weighted_wr_ignores_zero_weight_and_unknown_patches():
    result = scores.weighted_wr(
        [("16.13", 50, 30), ("16.12", 100, 10), ("16.11", 100, 100)],
        {"16.13": 1.0, "16.12": 0.0},  # 16.12 coupé (rework), 16.11 hors fenêtre
    )
    assert result.games == 50
    assert result.wr == pytest.approx(0.6)


def test_weighted_wr_none_when_no_effective_games():
    assert scores.weighted_wr([("16.12", 100, 50)], {"16.12": 0.0}) is None
    assert scores.weighted_wr([], {"16.13": 1.0}) is None


# --- Wilson ---


def test_wilson_interval_known_value():
    # p=0.6, n=100, z=1.96 → ≈ [0.502, 0.691] (valeurs de référence classiques).
    low, high = scores.wilson_interval(0.6, 100)
    assert low == pytest.approx(0.502, abs=2e-3)
    assert high == pytest.approx(0.691, abs=2e-3)


def test_wilson_interval_widens_for_small_n():
    low_small, high_small = scores.wilson_interval(0.6, 10)
    low_big, high_big = scores.wilson_interval(0.6, 1000)
    assert high_small - low_small > high_big - low_big
    assert 0.0 <= low_small < high_small <= 1.0


def test_wilson_interval_degenerate_n():
    assert scores.wilson_interval(0.5, 0) == (0.0, 1.0)


def test_normal_interval_centers_on_p():
    lo, hi = scores.normal_interval(0.5, se=0.02)
    assert lo == pytest.approx(0.5 - 1.96 * 0.02, abs=1e-3)
    assert hi == pytest.approx(0.5 + 1.96 * 0.02, abs=1e-3)


def test_normal_interval_clamped_to_0_1():
    lo, hi = scores.normal_interval(0.99, se=1.0)
    assert lo == 0.0
    assert hi == 1.0


def test_newcombe_interval_zero_when_identical_intervals():
    # p1 == p2 avec les mêmes bornes : la différence est 0, l'IC est symétrique
    # (combinaison en racine de la somme des carrés des 2 marges, pas une
    # simple addition — cf. formule de Newcombe).
    lo, hi = scores.newcombe_interval(0.5, 0.4, 0.6, 0.5, 0.4, 0.6)
    assert lo == pytest.approx(-math.sqrt(0.02))
    assert hi == pytest.approx(math.sqrt(0.02))


def test_newcombe_interval_excludes_zero_for_clear_gap():
    # p1 nettement au-dessus de p2, IC étroits des deux côtés : la différence
    # (~0.30) doit rester positive même à la borne basse de l'IC combiné.
    lo, hi = scores.newcombe_interval(0.60, 0.58, 0.62, 0.30, 0.28, 0.32)
    assert lo > 0.0


# --- tiers / synergie / lissage ---


def test_reliability_tiers():
    assert scores.reliability_tier(10) == "faible"
    assert scores.reliability_tier(50) == "moyen"
    assert scores.reliability_tier(400) == "eleve"


def test_synergy_is_difference_to_member_mean():
    assert scores.synergy(0.55, (0.5, 0.5, 0.5)) == pytest.approx(0.05)
    assert scores.synergy(0.45, (0.4, 0.5)) == pytest.approx(0.0)


def test_trio_prediction_mean_of_available_duos():
    assert scores.trio_prediction([0.03, 0.0, 0.03]) == pytest.approx(0.02)
    assert scores.trio_prediction([0.04]) == pytest.approx(0.04)
    assert scores.trio_prediction([]) == 0.0  # prior neutre sans duo observé


def test_smooth_interpolates_between_prediction_and_raw():
    # n = 0 → prédiction pure ; n = k → moitié-moitié ; n ≫ k → brut.
    assert scores.smooth(0.2, 0.0, 0.02, k=200) == pytest.approx(0.02)
    assert scores.smooth(0.2, 200.0, 0.02, k=200) == pytest.approx(0.11)
    assert scores.smooth(0.2, 1_000_000.0, 0.02, k=200) == pytest.approx(0.2, abs=1e-4)


def test_smooth_k_zero_returns_raw():
    assert scores.smooth(0.2, 10.0, 0.02, k=0.0) == pytest.approx(0.2)


def test_smooth_negative_k_raises():
    with pytest.raises(ValueError):
        scores.smooth(0.2, 10.0, 0.02, k=-1.0)


# --- add_combined_platform (vue « toutes régions ») ---


def test_add_combined_platform_sums_per_patch():
    mapping = {
        ("euw1", "jgl_mid", 1, 2): [("16.13", 40, 24), ("16.12", 10, 4)],
        ("kr", "jgl_mid", 1, 2): [("16.13", 60, 30)],
        ("kr", "jgl_mid", 9, 9): [("16.13", 5, 5)],
    }
    scores.add_combined_platform(mapping)
    assert sorted(mapping[("all", "jgl_mid", 1, 2)]) == [("16.12", 10, 4), ("16.13", 100, 54)]
    assert mapping[("all", "jgl_mid", 9, 9)] == [("16.13", 5, 5)]
    # Les entrées régionales sont intactes.
    assert mapping[("euw1", "jgl_mid", 1, 2)] == [("16.13", 40, 24), ("16.12", 10, 4)]


def test_add_combined_platform_is_idempotent():
    mapping = {("euw1", 1): [("16.13", 10, 5)]}
    scores.add_combined_platform(mapping)
    scores.add_combined_platform(mapping)  # les entrées 'all' sont ignorées en source
    assert mapping[("all", 1)] == [("16.13", 10, 5)]


# --- weighted_slope_ci ---


def test_weighted_slope_ci_perfect_line():
    # y = 2x + 1, poids égaux, pile sur la droite : pente exacte, aucun résidu
    # donc IC de largeur nulle.
    points = [(0.0, 1.0, 10.0), (1.0, 3.0, 10.0), (2.0, 5.0, 10.0)]
    result = scores.weighted_slope_ci(points)
    assert result.slope == pytest.approx(2.0)
    assert result.ci_low == pytest.approx(2.0)
    assert result.ci_high == pytest.approx(2.0)


def test_weighted_slope_ci_weights_favor_higher_confidence_points():
    # Point (2, 100) quasi ignoré (poids infime) : la pente colle aux 2 premiers.
    points = [(0.0, 0.0, 100.0), (1.0, 1.0, 100.0), (2.0, 100.0, 0.001)]
    assert scores.weighted_slope_ci(points).slope == pytest.approx(1.0, abs=0.01)


def test_weighted_slope_ci_widens_around_noisy_points():
    # Même pente moyenne que le cas parfait, mais un point hors de la droite :
    # l'IC doit s'élargir (résidu non nul) sans forcément changer la pente.
    points = [(0.0, 1.0, 10.0), (1.0, 3.5, 10.0), (2.0, 4.5, 10.0)]
    result = scores.weighted_slope_ci(points)
    assert result.ci_high - result.ci_low > 0.0


def test_weighted_slope_ci_needs_at_least_three_points():
    assert scores.weighted_slope_ci([]) is None
    assert scores.weighted_slope_ci([(0.0, 1.0, 10.0)]) is None
    assert scores.weighted_slope_ci([(0.0, 1.0, 10.0), (1.0, 2.0, 10.0)]) is None


def test_weighted_slope_ci_none_when_all_weights_zero():
    points = [(0.0, 1.0, 0.0), (1.0, 2.0, 0.0), (2.0, 3.0, 0.0)]
    assert scores.weighted_slope_ci(points) is None


def test_weighted_slope_ci_none_when_x_constant():
    # Variance nulle en x : pente indéfinie (dénominateur nul).
    points = [(5.0, 1.0, 10.0), (5.0, 2.0, 10.0), (5.0, 3.0, 10.0)]
    assert scores.weighted_slope_ci(points) is None


def test_weighted_slope_ci_none_beyond_t_table_range():
    # 9 points -> df=7, hors de la table de Student embarquée (jusqu'à df=6,
    # cf. borne des tranches de durée 5-40 min) : None plutôt qu'un crash.
    points = [(float(i), float(i), 10.0) for i in range(9)]
    assert scores.weighted_slope_ci(points) is None
