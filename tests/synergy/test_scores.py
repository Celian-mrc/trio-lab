"""Tests des mathématiques de score (WR pondéré, Wilson, lissage bayésien)."""

from __future__ import annotations

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
