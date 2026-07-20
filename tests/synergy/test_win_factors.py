"""Tests unitaires (sans DB) de synergy/win_factors.py — diagnostic VIF.

Retour utilisateur 2026-07-19 (audit méthodologique sourcé) : le VIF par
régression auxiliaire (VIF_j = 1 / (1 − R²_j)) signale la colinéarité entre
features continues sans la « corriger » par orthogonalisation manuelle
(déconseillée par la littérature citée dans docs/ROADMAP.md) — seulement un
diagnostic loggé, qui renforce le ridge de l'IRLS au-delà du seuil d'alerte.
"""

from __future__ import annotations

from trio_lab.synergy import win_factors


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

    vifs = win_factors._compute_vif(rows, [1, 2, 3])

    assert vifs[1] > win_factors.VIF_ALERT_THRESHOLD
    assert vifs[2] > win_factors.VIF_ALERT_THRESHOLD
    assert vifs[3] < win_factors.VIF_ALERT_THRESHOLD
