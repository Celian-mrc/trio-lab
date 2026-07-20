"""Tests unitaires (sans DB) de synergy/_linalg.py — algèbre partagée par
win_factors.py (IRLS logistique) et gold_factors.py (OLS pondéré).

Retour utilisateur 2026-07-19 (audit méthodologique sourcé) : le VIF par
régression auxiliaire (VIF_j = 1 / (1 − R²_j)) signale la colinéarité entre
features continues sans la « corriger » par orthogonalisation manuelle
(déconseillée par la littérature citée dans docs/ROADMAP.md) — seulement un
diagnostic loggé, qui renforce le ridge au-delà du seuil d'alerte.
"""

from __future__ import annotations

import pytest

from trio_lab.synergy import _linalg


def test_compute_vif_flags_correlated_columns_not_independent_ones():
    """Design matrix à 3 colonnes (intercept + 2 candidats) : la colonne 2
    est une quasi-copie bruitée de la colonne 1 (VIF très élevé attendu) ;
    la colonne 3 varie de façon non linéaire par rapport aux 2 autres (VIF
    proche de 1, sous le seuil d'alerte `VIF_ALERT_THRESHOLD`)."""
    rows = []
    for i in range(50):
        x1 = float(i)
        x2 = x1 + (0.01 if i % 2 == 0 else -0.01)  # quasi-copie de x1
        x3 = float((i * 37) % 13)  # relation non linéaire, peu colinéaire
        rows.append([1.0, x1, x2, x3])

    vifs = _linalg.compute_vif(rows, [1, 2, 3])

    assert vifs[1] > _linalg.VIF_ALERT_THRESHOLD
    assert vifs[2] > _linalg.VIF_ALERT_THRESHOLD
    assert vifs[3] < _linalg.VIF_ALERT_THRESHOLD


def test_fit_weighted_ols_recovers_known_linear_relation():
    """y = 2 + 3·x1 (bruit nul, relation exacte) : l'ajustement doit
    retrouver intercept=2, coef=3 à la précision flottante près."""
    x_rows = [[1.0, float(i)] for i in range(20)]
    y = [2.0 + 3.0 * float(i) for i in range(20)]
    weights = [1.0] * 20

    beta = _linalg.fit_weighted_ols(x_rows, y, weights, ridge=1e-9)

    assert beta[0] == pytest.approx(2.0, abs=1e-6)
    assert beta[1] == pytest.approx(3.0, abs=1e-6)

    r2 = _linalg.weighted_r_squared(x_rows, y, beta, weights)
    assert r2 == pytest.approx(1.0, abs=1e-6)


def test_weighted_r_squared_drops_with_noise():
    """Même relation, mais y bruité alternativement au-dessus/en-dessous :
    R² doit rester net (signal fort) mais strictement sous 1."""
    x_rows = [[1.0, float(i)] for i in range(20)]
    y = [2.0 + 3.0 * float(i) + (5.0 if i % 2 == 0 else -5.0) for i in range(20)]
    weights = [1.0] * 20

    beta = _linalg.fit_weighted_ols(x_rows, y, weights, ridge=1e-9)
    r2 = _linalg.weighted_r_squared(x_rows, y, beta, weights)

    assert 0.5 < r2 < 1.0
