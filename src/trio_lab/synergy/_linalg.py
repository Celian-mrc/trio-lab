"""Algèbre linéaire pure Python partagée par les modèles de régression
(`win_factors.py` — logistique, `gold_factors.py` — OLS) : élimination de
Gauss, diagnostic de colinéarité (VIF). Pas de numpy/scipy, cohérent avec
la philosophie du projet pour des ajustements à ~10 variables sur quelques
dizaines de milliers de lignes.
"""

from __future__ import annotations

import math

# Seuil d'alerte VIF (variance inflation factor) standard en régression :
# > 5 signale une colinéarité qui gonfle les erreurs-types des coefficients
# concernés (pas leur valeur ponctuelle).
VIF_ALERT_THRESHOLD = 5.0
DEFAULT_RIDGE = 1e-6  # stabilisation numérique pure, pas une vraie régularisation
VIF_RIDGE = 1.0  # régularisation réelle, appliquée seulement si un VIF dépasse le seuil


def solve(matrix: list[list[float]], b: list[float]) -> list[float]:
    """Résolution par élimination de Gauss avec pivot partiel — matrice p×p,
    p ≈ 10 : trivial en pure Python à cette taille."""
    n = len(b)
    aug = [row[:] + [b[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= pv
        for r in range(n):
            if r != col and aug[r][col] != 0.0:
                factor = aug[r][col]
                for j in range(col, n + 1):
                    aug[r][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]


def compute_vif(x_rows: list[list[float]], target_cols: list[int]) -> dict[int, float]:
    """VIF (variance inflation factor) par régression auxiliaire OLS non
    pondérée : VIF_j = 1 / (1 − R²_j), où R²_j vient de la régression de la
    colonne j sur toutes les autres colonnes du design matrix (intercept
    inclus, colonne 0). Diagnostic seulement, jamais bloquant — l'appelant
    décide comment réagir (`VIF_ALERT_THRESHOLD`, renforce le ridge)."""
    n = len(x_rows)
    p = len(x_rows[0])
    vifs: dict[int, float] = {}
    for j in target_cols:
        other_cols = [c for c in range(p) if c != j]
        xtx = [[0.0] * len(other_cols) for _ in other_cols]
        xty = [0.0] * len(other_cols)
        target_vals = [x_rows[i][j] for i in range(n)]
        target_mean = sum(target_vals) / n
        ss_tot = sum((v - target_mean) ** 2 for v in target_vals)
        for i in range(n):
            xi = [x_rows[i][c] for c in other_cols]
            yj = x_rows[i][j]
            for a in range(len(other_cols)):
                xty[a] += xi[a] * yj
                for b_ in range(len(other_cols)):
                    xtx[a][b_] += xi[a] * xi[b_]
        # Stabilisation numérique (même esprit que `DEFAULT_RIDGE`, pas une
        # vraie régularisation) : sans elle, une feature CONSTANTE parmi
        # `other_cols` rend la matrice normale singulière (pivot nul).
        for a in range(len(other_cols)):
            xtx[a][a] += DEFAULT_RIDGE
        beta = solve(xtx, xty)
        ss_res = 0.0
        for i in range(n):
            xi = [x_rows[i][c] for c in other_cols]
            pred = sum(b * x for b, x in zip(beta, xi, strict=True))
            ss_res += (x_rows[i][j] - pred) ** 2
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        vifs[j] = 1.0 / (1.0 - r2) if r2 < 0.999 else math.inf
    return vifs


def fit_weighted_ols(
    x_rows: list[list[float]], y: list[float], row_weights: list[float], *, ridge: float
) -> list[float]:
    """Moindres carrés pondérés, forme fermée (équations normales
    (XᵀWX)β = XᵀWy, résolues par `solve`) — pas d'itération, contrairement à
    l'IRLS logistique : la cible est déjà continue."""
    n, p = len(x_rows), len(x_rows[0])
    xtwx = [[0.0] * p for _ in range(p)]
    xtwy = [0.0] * p
    for i in range(n):
        xi = x_rows[i]
        w = row_weights[i]
        for a in range(p):
            wxa = w * xi[a]
            xtwy[a] += wxa * y[i]
            for b_ in range(p):
                xtwx[a][b_] += wxa * xi[b_]
    for a in range(p):
        xtwx[a][a] += ridge
    return solve(xtwx, xtwy)


def weighted_r_squared(
    x_rows: list[list[float]], y: list[float], beta: list[float], row_weights: list[float]
) -> float:
    """R² pondéré = 1 − SCR_pondérée / SCT_pondérée, moyenne pondérée comme
    référence (pas la moyenne simple) — cohérent avec un ajustement pondéré
    par poids de patch."""
    w_sum = sum(row_weights)
    y_mean = sum(w * yi for w, yi in zip(row_weights, y, strict=True)) / w_sum
    ss_res = 0.0
    ss_tot = 0.0
    for i, xi in enumerate(x_rows):
        pred = sum(b * x for b, x in zip(beta, xi, strict=True))
        ss_res += row_weights[i] * (y[i] - pred) ** 2
        ss_tot += row_weights[i] * (y[i] - y_mean) ** 2
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
