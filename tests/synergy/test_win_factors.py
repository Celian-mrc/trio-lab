"""Tests unitaires (sans DB) de synergy/win_factors.py — AUC hors-échantillon.

Ajoutés le 2026-07-24 (retour utilisateur + audit) : aucun AUC n'était mesuré
nulle part avant, pour aucun module de ce projet.
"""

from __future__ import annotations

from trio_lab.synergy import win_factors


def test_auc_perfect_separation_is_one():
    y_true = [0.0, 0.0, 1.0, 1.0]
    y_score = [0.1, 0.2, 0.8, 0.9]
    assert win_factors._auc(y_true, y_score) == 1.0


def test_auc_perfect_inversion_is_zero():
    y_true = [0.0, 0.0, 1.0, 1.0]
    y_score = [0.9, 0.8, 0.2, 0.1]
    assert win_factors._auc(y_true, y_score) == 0.0


def test_auc_random_scores_is_around_half():
    # Score identique pour tout le monde : ex-aequo total, chaque paire
    # positive/négative a exactement 50 % de chances d'être bien classée
    # (rang moyen) — pas de biais dans la gestion des ex-aequo.
    y_true = [0.0, 1.0, 0.0, 1.0]
    y_score = [0.5, 0.5, 0.5, 0.5]
    assert win_factors._auc(y_true, y_score) == 0.5


def test_auc_none_when_single_class():
    # Un jeu de test qui ne contient que des victoires (ou que des défaites)
    # rend l'AUC sans objet (pas de paire positive/négative à comparer) —
    # jamais une division par zéro, `None` explicite.
    assert win_factors._auc([1.0, 1.0, 1.0], [0.2, 0.6, 0.9]) is None
    assert win_factors._auc([0.0, 0.0], [0.2, 0.6]) is None


def test_auc_handles_ties_with_average_rank():
    # 2 négatifs à 0.3 (ex-aequo), 1 positif à 0.3 (même score que les 2
    # négatifs), 1 positif à 0.9 (nettement au-dessus) — le positif à 0.9
    # est toujours mieux classé (2/2 paires), celui à 0.3 est à égalité avec
    # les 2 négatifs (rang moyen, ni gagnant ni perdant) → AUC = (2 + 1) / 4.
    y_true = [0.0, 0.0, 1.0, 1.0]
    y_score = [0.3, 0.3, 0.3, 0.9]
    assert win_factors._auc(y_true, y_score) == 0.75


def test_predict_matches_manual_sigmoid():
    import math

    beta = [0.0, 1.0]  # intercept nul, coefficient 1 sur la seule feature
    assert win_factors._predict(beta, [1.0, 2.0]) == 1.0 / (1.0 + math.exp(-2.0))


def test_is_test_match_is_deterministic_and_stable():
    # Même match_id → même résultat à chaque appel (contrairement à
    # `hash()`, randomisé par process via PYTHONHASHSEED) — condition pour
    # que les 2 lignes d'un même match (une par équipe) tombent toujours du
    # même côté du split train/test.
    assert win_factors._is_test_match("EUW1_1234") == win_factors._is_test_match("EUW1_1234")


def test_is_test_match_splits_roughly_one_fifth():
    match_ids = [f"EUW1_{i}" for i in range(10_000)]
    test_count = sum(win_factors._is_test_match(m) for m in match_ids)
    # ~20 % attendu (crc32 % 5 == 0) — tolérance large, pas un test de
    # distribution statistique rigoureux, juste une garde contre une
    # implémentation qui renverrait toujours True/False ou un ratio absurde.
    assert 1_500 < test_count < 2_500
