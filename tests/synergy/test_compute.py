"""Tests purs (sans base) de `compute._cc_pct_fields`."""

from __future__ import annotations

import pytest

from trio_lab.synergy import compute


def test_cc_pct_fields_empty_reference_returns_none():
    """Table `champion_cc_theoretical` pas encore synchronisée : pas de crash."""
    fields = compute._cc_pct_fields((1, 2, 3), {}, empirical_cc_time_s=100.0, games_eff=50.0, k=200)
    assert fields == {"cc_theoretical_pct": None, "cc_empirical_pct": None, "cc_blended_pct": None}


def test_cc_pct_fields_trio_computes_all_three():
    cc_scores = {1: 6.0, 2: 1.0, 3: 2.0, 99: 7.0}  # 99 = max global (hors trio)
    fields = compute._cc_pct_fields(
        (1, 2, 3), cc_scores, empirical_cc_time_s=120.0, games_eff=0.0, k=200
    )
    # théorique : (6+1+2) / (3×7) × 100 = 42.857 % ; empirique : 120/240×100 = 50 %.
    assert fields["cc_theoretical_pct"] == pytest.approx(900 / 21)
    assert fields["cc_empirical_pct"] == pytest.approx(50.0)
    # games_eff=0 : mélangé = théorique pur.
    assert fields["cc_blended_pct"] == pytest.approx(fields["cc_theoretical_pct"])


def test_cc_pct_fields_duo_uses_2x_ceiling():
    cc_scores = {1: 6.0, 2: 1.0, 99: 7.0}
    fields = compute._cc_pct_fields(
        (1, 2), cc_scores, empirical_cc_time_s=None, games_eff=500.0, k=200
    )
    # théorique duo : (6+1) / (2×7) × 100 = 50 %.
    assert fields["cc_theoretical_pct"] == pytest.approx(50.0)
    assert fields["cc_empirical_pct"] is None
    # Pas d'empirique : le mélange retombe sur le théorique, quel que soit games_eff.
    assert fields["cc_blended_pct"] == pytest.approx(50.0)


def test_cc_pct_fields_missing_member_score_counts_as_zero():
    cc_scores = {1: 6.0, 99: 7.0}  # champion 2 absent (non résolu côté Data Dragon)
    fields = compute._cc_pct_fields(
        (1, 2), cc_scores, empirical_cc_time_s=0.0, games_eff=0.0, k=200
    )
    # (6 + 0) / (2×7) × 100 = 42.857 %.
    assert fields["cc_theoretical_pct"] == pytest.approx(600 / 14)
