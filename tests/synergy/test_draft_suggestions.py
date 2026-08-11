"""Tests purs (sans base) du mode "Contre cette équipe" : exclusion
partielle de l'axe matchup (`_combined_score`, `_matchup_avg_z`)."""

from __future__ import annotations

import pytest

from trio_lab.synergy import draft_suggestions as ds


def test_combined_score_ignores_missing_matchup_axis():
    """Pas de donnée matchup pour ce candidat (rôle non scouté, ou aucun
    score_matchup connu) : l'axe est ignoré, le candidat n'est PAS exclu
    (contrairement aux axes de `weights`, cf. docstring `_combined_score`)."""
    data = {"synergy_sum": 0.10, "stats": {}}
    combined = ds._combined_score(
        data,
        n_anchors=2,
        weights={"synergy": 1.0},
        zstats={"synergy": (0.0, 1.0)},
        matchup_weight=0.40,
        matchup_zstats=(0.0, 1.0),
    )
    assert combined == pytest.approx(0.05)  # synergie seule : (0.10/2 - 0)/1 = 0.05


def test_combined_score_adds_matchup_when_available():
    """Delta matchup connu : ajouté (poids × z-score) au score composé."""
    data = {"synergy_sum": 0.10, "stats": {}, "matchup_delta": 0.30}
    combined = ds._combined_score(
        data,
        n_anchors=2,
        weights={"synergy": 1.0},
        zstats={"synergy": (0.0, 1.0)},
        matchup_weight=0.40,
        matchup_zstats=(0.10, 0.10),
    )
    # synergie : 0.05 ; matchup : 0.40 × (0.30 - 0.10) / 0.10 = 0.80.
    assert combined == pytest.approx(0.05 + 0.80)


def test_combined_score_matchup_weight_zero_is_noop():
    """`matchup_weight=0` (archétypes existants, retour arrière compatible) :
    aucun effet même si `matchup_zstats` est fourni par erreur."""
    data = {"synergy_sum": 0.10, "stats": {}, "matchup_delta": 999.0}
    combined = ds._combined_score(
        data,
        n_anchors=2,
        weights={"synergy": 1.0},
        zstats={"synergy": (0.0, 1.0)},
        matchup_zstats=(0.0, 1.0),
    )
    assert combined == pytest.approx(0.05)


def test_matchup_avg_z_averages_only_scouted_roles_with_data():
    """2 rôles scoutés, un seul avec une donnée pour le champion posé à ce
    rôle : la moyenne ne porte QUE sur ce rôle-là, pas de 0 pour l'autre."""
    matchup_by_role = {
        "jgl": ({1: 0.20}, (0.0, 0.10)),  # champion posé (1) a une donnée
        "bot": ({99: 0.20}, (0.0, 0.10)),  # champion posé (5) n'y est PAS
    }
    placed = {"jgl": 1, "mid": 2, "sup": 3, "top": 4, "bot": 5}
    avg_z = ds._matchup_avg_z(placed, matchup_by_role)
    assert avg_z == pytest.approx(2.0)  # (0.20 - 0.0) / 0.10, seul rôle jgl compte


def test_matchup_avg_z_none_when_no_role_has_data():
    matchup_by_role = {"jgl": ({99: 0.20}, (0.0, 0.10))}
    placed = {"jgl": 1, "mid": 2, "sup": 3, "top": 4, "bot": 5}
    assert ds._matchup_avg_z(placed, matchup_by_role) is None


def test_matchup_avg_z_empty_by_role():
    placed = {"jgl": 1, "mid": 2, "sup": 3, "top": 4, "bot": 5}
    assert ds._matchup_avg_z(placed, {}) is None
