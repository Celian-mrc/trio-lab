import numpy as np

from trio_lab.ml.embeddings import (
    N_COMPONENTS,
    _sum_pair_stats_excluding,
    fit_embeddings,
    pair_table_from_agg_duo,
)

PATCHES = ["16.17", "16.16", "16.15"]


def test_pair_table_from_agg_duo_sums_across_pair_types_and_normalizes_order():
    agg_duo = {
        ("16.16", "jgl_mid", 1, 2): (100, 60),
        ("16.15", "jgl_sup", 2, 1): (50, 20),  # ordre champ_a/champ_b inversé
        ("16.16", "not_a_duo_pair", 1, 2): (999, 999),  # ignoré (pas jgl_mid/jgl_sup/mid_sup)
    }
    result = pair_table_from_agg_duo(agg_duo)
    assert result == {("16.16", 1, 2): (100, 60), ("16.15", 1, 2): (50, 20)}


def test_sum_pair_stats_excluding_skips_only_the_given_patch():
    pair_table = {
        ("16.17", 1, 2): (1000, 1000),
        ("16.16", 1, 2): (100, 60),
        ("16.15", 1, 2): (50, 20),
    }
    result = _sum_pair_stats_excluding(pair_table, PATCHES, "16.17")
    assert result == {(1, 2): (150, 80)}


def test_fit_embeddings_returns_a_vector_per_champion():
    pair_table = {
        ("16.16", 1, 2): (100, 70),
        ("16.16", 1, 3): (100, 30),
        ("16.16", 2, 3): (100, 50),
        ("16.15", 1, 2): (100, 70),
        ("16.15", 1, 3): (100, 30),
        ("16.15", 2, 3): (100, 50),
    }
    vectors = fit_embeddings(pair_table, PATCHES, "16.17", [1, 2, 3])
    assert set(vectors) == {1, 2, 3}
    for v in vectors.values():
        assert isinstance(v, np.ndarray)
        assert v.shape == (N_COMPONENTS,)


def test_fit_embeddings_deterministic():
    pair_table = {("16.16", 1, 2): (100, 70), ("16.15", 1, 2): (100, 70)}
    a = fit_embeddings(pair_table, PATCHES, "16.17", [1, 2])
    b = fit_embeddings(pair_table, PATCHES, "16.17", [1, 2])
    assert np.allclose(a[1], b[1])
    assert np.allclose(a[2], b[2])


def test_fit_embeddings_excludes_own_patch_data():
    # Le patch 16.17 a une valeur extrême (100% WR) sur une paire, neutre
    # (50%) sur une autre — s'il fuitait, exclure 16.17 ou 16.16 donnerait
    # le même résultat (les 2 autres patchs sont identiques par ailleurs).
    # 4 champions (pas 2) pour éviter le cas dégénéré numériquement d'une
    # matrice 2×2 en SVD, non représentatif du cas réel (~170 champions).
    pair_table = {
        ("16.17", 1, 2): (1000, 1000),
        ("16.16", 1, 2): (100, 50),
        ("16.15", 1, 2): (100, 50),
        ("16.17", 3, 4): (100, 50),
        ("16.16", 3, 4): (100, 50),
        ("16.15", 3, 4): (100, 50),
    }
    excluding_16_17 = fit_embeddings(pair_table, PATCHES, "16.17", [1, 2, 3, 4])
    excluding_16_16 = fit_embeddings(pair_table, PATCHES, "16.16", [1, 2, 3, 4])
    assert not np.allclose(excluding_16_17[1], excluding_16_16[1])
