import numpy as np

from trio_lab.ml.champion_meta import ChampionMeta
from trio_lab.ml.features import (
    _TRIO_STAT_COLUMNS,
    COMPOSITION_FEATURE_NAMES,
    EMBEDDING_FEATURE_NAMES,
    FEATURE_NAMES,
    FIVE_ROLE_FEATURE_NAMES,
    _avg,
    _avg_range,
    _champion_wr,
    _duo_synergy,
    _matchup_delta,
    _row_features,
    _row_features_5roles,
    _row_features_composition,
    _row_features_embedding,
    _scaling_slope_excluding,
    _sum_excluding,
    _team_composition_features,
    _team_embedding,
    _trio_stat,
    _trio_wr,
    _wr,
)

PATCHES = ["16.17", "16.16", "16.15"]


def test_wr_none_when_no_games():
    assert _wr(0, 0) is None


def test_wr_basic():
    assert _wr(100, 60) == 0.6


def test_avg_none_when_no_n():
    assert _avg(100.0, 0) is None


def test_avg_basic():
    assert _avg(3000.0, 60) == 50.0


def test_sum_excluding_skips_only_the_given_patch():
    table = {
        ("16.17", "X"): (10, 5),
        ("16.16", "X"): (20, 10),
        ("16.15", "X"): (30, 20),
    }
    assert _sum_excluding(table, ("X",), PATCHES, "16.17") == (50, 30)
    assert _sum_excluding(table, ("X",), PATCHES, "16.16") == (40, 25)


def test_champion_wr_leaves_out_the_match_own_patch():
    # Le champion 1 (JUNGLE) a 100% WR sur son propre patch (16.17) mais 50%
    # sur les 2 autres — la feature d'un match du 16.17 ne doit PAS voir le
    # 100% (fuite), seulement le 50% des 2 autres patchs.
    agg_champion = {
        ("16.17", "JUNGLE", 1): (100, 100),
        ("16.16", "JUNGLE", 1): (100, 50),
        ("16.15", "JUNGLE", 1): (100, 50),
    }
    assert _champion_wr(agg_champion, PATCHES, "16.17", "jgl", 1) == 0.5


def test_champion_wr_none_when_only_seen_on_the_excluded_patch():
    agg_champion = {("16.17", "JUNGLE", 1): (100, 100)}
    assert _champion_wr(agg_champion, PATCHES, "16.17", "jgl", 1) is None


def test_duo_synergy_computation():
    agg_champion = {
        ("16.16", "JUNGLE", 1): (100, 50),  # wr 0.5
        ("16.15", "JUNGLE", 1): (100, 50),
        ("16.16", "MIDDLE", 2): (100, 60),  # wr 0.6
        ("16.15", "MIDDLE", 2): (100, 60),
    }
    agg_duo = {
        ("16.16", "jgl_mid", 1, 2): (100, 66),  # wr 0.66
        ("16.15", "jgl_mid", 1, 2): (100, 66),
    }
    synergy = _duo_synergy(agg_duo, agg_champion, PATCHES, "16.17", "jgl_mid", "jgl", "mid", 1, 2)
    # duo_wr (0.66) - avg(champ_a wr 0.5, champ_b wr 0.6) = 0.66 - 0.55 = 0.11
    assert synergy is not None
    assert round(synergy, 2) == 0.11


def test_trio_wr_basic():
    agg_trio = {("16.16", 1, 2, 3): (50, 25), ("16.15", 1, 2, 3): (50, 25)}
    assert _trio_wr(agg_trio, PATCHES, "16.17", 1, 2, 3) == 0.5


def test_matchup_delta_positive_means_counters_enemy():
    agg_matchup = {
        ("16.16", "JUNGLE", 1, 9): (100, 80),  # champ 1 vs champ 9 : wr 0.8
        ("16.15", "JUNGLE", 1, 9): (100, 80),
    }
    agg_champion = {
        ("16.16", "JUNGLE", 1): (100, 50),  # baseline wr 0.5
        ("16.15", "JUNGLE", 1): (100, 50),
    }
    delta = _matchup_delta(agg_matchup, agg_champion, PATCHES, "16.17", "jgl", 1, 9)
    assert delta is not None
    assert round(delta, 2) == 0.3  # 0.8 - 0.5


def test_trio_stat_leaves_out_the_match_own_patch():
    agg_trio_stats = {
        ("16.17", 1, 2, 3): {"gold15": (100000, 100)},  # patch du match : jamais lu
        ("16.16", 1, 2, 3): {"gold15": (2500, 50)},
        ("16.15", 1, 2, 3): {"gold15": (2500, 50)},
    }
    avg = _trio_stat(agg_trio_stats, PATCHES, "16.17", 1, 2, 3, "gold15")
    assert avg == 50.0  # (2500 + 2500) / (50 + 50), jamais le patch 16.17


def test_trio_stat_none_when_missing():
    assert _trio_stat({}, PATCHES, "16.17", 1, 2, 3, "gold15") is None


def test_avg_range_full():
    range_theoretical = {1: 60.0, 2: 30.0, 3: 30.0}
    assert _avg_range(range_theoretical, 1, 2, 3) == 40.0


def test_avg_range_none_when_a_champion_is_missing():
    range_theoretical = {1: 60.0, 2: 30.0}
    assert _avg_range(range_theoretical, 1, 2, 3) is None


def test_scaling_slope_excluding_none_when_no_data():
    assert _scaling_slope_excluding(None, PATCHES, "16.17") is None
    assert _scaling_slope_excluding({}, PATCHES, "16.17") is None


def test_scaling_slope_excluding_computes_a_slope_with_enough_buckets():
    by_bucket = {
        15: [("16.17", 100, 90), ("16.16", 5, 3), ("16.15", 5, 2)],
        20: [("16.16", 5, 4), ("16.15", 5, 3)],
        25: [("16.16", 5, 2), ("16.15", 5, 1)],
    }
    slope = _scaling_slope_excluding(by_bucket, PATCHES, "16.17")
    # Le patch 16.17 (90% WR) est exclu : si la fuite existait, la pente
    # serait dominée par ce point à 100 games — on vérifie juste qu'un
    # résultat est bien produit à partir des 2 seuls autres patchs. Seuil
    # indépendant de MIN_GAMES (cf. SCALING_MIN_BUCKET_GAMES/SCALING_MIN_BUCKETS).
    assert slope is not None
    assert isinstance(slope, float)


def test_scaling_slope_excluding_none_below_min_buckets():
    # Un seul bucket exploitable une fois 16.17 exclu -> sous SCALING_MIN_BUCKETS.
    by_bucket = {15: [("16.17", 100, 90), ("16.16", 5, 3)]}
    assert _scaling_slope_excluding(by_bucket, PATCHES, "16.17") is None


def test_row_features_none_when_any_required_axis_incomplete():
    # Aucune donnée fournie nulle part -> toutes les baselines requises manquent.
    row = {
        "patch": "16.17",
        "us_jgl": 1,
        "us_mid": 2,
        "us_sup": 3,
        "them_jgl": 4,
        "them_mid": 5,
        "them_sup": 6,
    }
    assert _row_features(row, {}, {}, {}, {}, {}, {}, {}, PATCHES) is None


def test_row_features_complete_matches_feature_names_length():
    # Garde-fou : si on ajoute/retire une feature dans _row_features sans
    # mettre à jour FEATURE_NAMES (ou l'inverse), ce test casse.
    agg_champion = {
        (p, role, c): (100, 50)
        for p in ("16.16", "16.15")
        for role in ("JUNGLE", "MIDDLE", "UTILITY")
        for c in range(1, 7)
    }
    agg_duo = {
        (p, roles, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for roles, a, b in (
            ("jgl_mid", 1, 2),
            ("jgl_sup", 1, 3),
            ("mid_sup", 2, 3),
            ("jgl_mid", 4, 5),
            ("jgl_sup", 4, 6),
            ("mid_sup", 5, 6),
        )
    }
    agg_trio = {(p, 1, 2, 3): (100, 50) for p in ("16.16", "16.15")} | {
        (p, 4, 5, 6): (100, 50) for p in ("16.16", "16.15")
    }
    agg_matchup = {
        (p, role, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for role, a, b in (("JUNGLE", 1, 4), ("MIDDLE", 2, 5), ("UTILITY", 3, 6))
    }
    agg_trio_stats = {
        (p, *combo): {m: (100, 50) for m in _TRIO_STAT_COLUMNS}
        for p in ("16.16", "16.15")
        for combo in ((1, 2, 3), (4, 5, 6))
    }
    range_theoretical = {1: 50.0, 2: 40.0, 3: 30.0, 4: 20.0, 5: 10.0, 6: 5.0}
    # agg_trio_duration volontairement vide : scaling doit s'imputer à 0.0
    # sans faire tomber la ligne (cf. docstring du module).
    agg_trio_duration: dict = {}
    row = {
        "patch": "16.17",
        "us_jgl": 1,
        "us_mid": 2,
        "us_sup": 3,
        "them_jgl": 4,
        "them_mid": 5,
        "them_sup": 6,
    }
    result = _row_features(
        row,
        agg_champion,
        agg_duo,
        agg_trio,
        agg_matchup,
        agg_trio_stats,
        range_theoretical,
        agg_trio_duration,
        PATCHES,
    )
    assert result is not None
    assert len(result) == len(FEATURE_NAMES)
    # scaling imputé à 0.0 (2 dernières features, cf. FEATURE_NAMES).
    assert result[-2:] == [0.0, 0.0]


def test_row_features_5roles_none_when_top_bot_data_missing():
    # Le socle jgl/mid/sup est complet (mêmes données que le test réduit
    # complet), mais top/bot n'ont aucune baseline -> doit rester None.
    agg_champion = {
        (p, role, c): (100, 50)
        for p in ("16.16", "16.15")
        for role in ("JUNGLE", "MIDDLE", "UTILITY")
        for c in range(1, 7)
    }
    agg_duo = {
        (p, roles, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for roles, a, b in (
            ("jgl_mid", 1, 2),
            ("jgl_sup", 1, 3),
            ("mid_sup", 2, 3),
            ("jgl_mid", 4, 5),
            ("jgl_sup", 4, 6),
            ("mid_sup", 5, 6),
        )
    }
    agg_trio = {(p, 1, 2, 3): (100, 50) for p in ("16.16", "16.15")} | {
        (p, 4, 5, 6): (100, 50) for p in ("16.16", "16.15")
    }
    agg_matchup = {
        (p, role, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for role, a, b in (("JUNGLE", 1, 4), ("MIDDLE", 2, 5), ("UTILITY", 3, 6))
    }
    row = {
        "patch": "16.17",
        "us_jgl": 1,
        "us_mid": 2,
        "us_sup": 3,
        "them_jgl": 4,
        "them_mid": 5,
        "them_sup": 6,
        "us_top": 7,
        "us_bot": 8,
        "them_top": 9,
        "them_bot": 10,
    }
    assert _row_features_5roles(row, agg_champion, agg_duo, agg_trio, agg_matchup, PATCHES) is None


def test_row_features_5roles_complete_matches_feature_names_length():
    agg_champion = {
        (p, role, c): (100, 50)
        for p in ("16.16", "16.15")
        for role in ("JUNGLE", "MIDDLE", "UTILITY", "TOP", "BOTTOM")
        for c in range(1, 11)
    }
    agg_duo = {
        (p, roles, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for roles, a, b in (
            ("jgl_mid", 1, 2),
            ("jgl_sup", 1, 3),
            ("mid_sup", 2, 3),
            ("jgl_mid", 4, 5),
            ("jgl_sup", 4, 6),
            ("mid_sup", 5, 6),
        )
    }
    agg_trio = {(p, 1, 2, 3): (100, 50) for p in ("16.16", "16.15")} | {
        (p, 4, 5, 6): (100, 50) for p in ("16.16", "16.15")
    }
    agg_matchup = {
        (p, role, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for role, a, b in (
            ("JUNGLE", 1, 4),
            ("MIDDLE", 2, 5),
            ("UTILITY", 3, 6),
            ("TOP", 7, 9),
            ("BOTTOM", 8, 10),
        )
    }
    row = {
        "patch": "16.17",
        "us_jgl": 1,
        "us_mid": 2,
        "us_sup": 3,
        "them_jgl": 4,
        "them_mid": 5,
        "them_sup": 6,
        "us_top": 7,
        "us_bot": 8,
        "them_top": 9,
        "them_bot": 10,
    }
    result = _row_features_5roles(row, agg_champion, agg_duo, agg_trio, agg_matchup, PATCHES)
    assert result is not None
    assert len(result) == len(FIVE_ROLE_FEATURE_NAMES)


def test_team_composition_features_basic():
    # 3 champions penchant magie (magic > attack), 2 penchant physique,
    # 2 tanks -> damage_mix = min(3, 2) = 2.
    meta = {
        1: ChampionMeta(id=1, tags=("Tank",), attack=3, defense=8, magic=7),
        2: ChampionMeta(id=2, tags=("Mage",), attack=2, defense=3, magic=9),
        3: ChampionMeta(id=3, tags=("Support",), attack=4, defense=5, magic=6),
        4: ChampionMeta(id=4, tags=("Marksman",), attack=9, defense=2, magic=1),
        5: ChampionMeta(id=5, tags=("Fighter", "Tank"), attack=7, defense=7, magic=2),
    }
    result = _team_composition_features(meta, (1, 2, 3, 4, 5))
    assert result is not None
    magic_avg, attack_avg, defense_avg, damage_mix, n_tanks = result
    assert round(magic_avg, 1) == round((7 + 9 + 6 + 1 + 2) / 5, 1)
    assert round(attack_avg, 1) == round((3 + 2 + 4 + 9 + 7) / 5, 1)
    assert round(defense_avg, 1) == round((8 + 3 + 5 + 2 + 7) / 5, 1)
    assert damage_mix == 2.0  # 3 penchant magie (1,2,3) vs 2 penchant physique (4,5)
    assert n_tanks == 2.0  # champions 1 et 5


def test_team_composition_features_none_when_champion_missing_from_meta():
    meta = {1: ChampionMeta(id=1, tags=("Tank",), attack=3, defense=8, magic=7)}
    assert _team_composition_features(meta, (1, 2, 3, 4, 5)) is None


def test_row_features_composition_none_when_meta_missing():
    agg_champion = {
        (p, role, c): (100, 50)
        for p in ("16.16", "16.15")
        for role in ("JUNGLE", "MIDDLE", "UTILITY", "TOP", "BOTTOM")
        for c in range(1, 11)
    }
    agg_duo = {
        (p, roles, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for roles, a, b in (
            ("jgl_mid", 1, 2),
            ("jgl_sup", 1, 3),
            ("mid_sup", 2, 3),
            ("jgl_mid", 4, 5),
            ("jgl_sup", 4, 6),
            ("mid_sup", 5, 6),
        )
    }
    agg_trio = {(p, 1, 2, 3): (100, 50) for p in ("16.16", "16.15")} | {
        (p, 4, 5, 6): (100, 50) for p in ("16.16", "16.15")
    }
    agg_matchup = {
        (p, role, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for role, a, b in (
            ("JUNGLE", 1, 4),
            ("MIDDLE", 2, 5),
            ("UTILITY", 3, 6),
            ("TOP", 7, 9),
            ("BOTTOM", 8, 10),
        )
    }
    row = {
        "patch": "16.17",
        "us_jgl": 1,
        "us_mid": 2,
        "us_sup": 3,
        "them_jgl": 4,
        "them_mid": 5,
        "them_sup": 6,
        "us_top": 7,
        "us_bot": 8,
        "them_top": 9,
        "them_bot": 10,
    }
    # meta vide -> composition manquante malgré un socle 5 rôles complet.
    result = _row_features_composition(
        row, agg_champion, agg_duo, agg_trio, agg_matchup, {}, PATCHES
    )
    assert result is None


def test_row_features_composition_complete_matches_feature_names_length():
    agg_champion = {
        (p, role, c): (100, 50)
        for p in ("16.16", "16.15")
        for role in ("JUNGLE", "MIDDLE", "UTILITY", "TOP", "BOTTOM")
        for c in range(1, 11)
    }
    agg_duo = {
        (p, roles, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for roles, a, b in (
            ("jgl_mid", 1, 2),
            ("jgl_sup", 1, 3),
            ("mid_sup", 2, 3),
            ("jgl_mid", 4, 5),
            ("jgl_sup", 4, 6),
            ("mid_sup", 5, 6),
        )
    }
    agg_trio = {(p, 1, 2, 3): (100, 50) for p in ("16.16", "16.15")} | {
        (p, 4, 5, 6): (100, 50) for p in ("16.16", "16.15")
    }
    agg_matchup = {
        (p, role, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for role, a, b in (
            ("JUNGLE", 1, 4),
            ("MIDDLE", 2, 5),
            ("UTILITY", 3, 6),
            ("TOP", 7, 9),
            ("BOTTOM", 8, 10),
        )
    }
    meta = {
        c: ChampionMeta(id=c, tags=("Fighter",), attack=5, defense=5, magic=5) for c in range(1, 11)
    }
    row = {
        "patch": "16.17",
        "us_jgl": 1,
        "us_mid": 2,
        "us_sup": 3,
        "them_jgl": 4,
        "them_mid": 5,
        "them_sup": 6,
        "us_top": 7,
        "us_bot": 8,
        "them_top": 9,
        "them_bot": 10,
    }
    result = _row_features_composition(
        row, agg_champion, agg_duo, agg_trio, agg_matchup, meta, PATCHES
    )
    assert result is not None
    assert len(result) == len(COMPOSITION_FEATURE_NAMES)


def test_team_embedding_averages_the_5_vectors():
    vectors = {c: np.array([float(c), float(c) * 2]) for c in range(1, 6)}
    result = _team_embedding(vectors, (1, 2, 3, 4, 5))
    assert result is not None
    assert result == [3.0, 6.0]  # moyenne de 1..5 = 3, moyenne de 2,4..10 = 6


def test_team_embedding_none_when_a_champion_is_missing():
    vectors = {1: np.array([1.0]), 2: np.array([2.0])}
    assert _team_embedding(vectors, (1, 2, 3, 4, 5)) is None


def test_row_features_embedding_none_when_embeddings_missing_for_patch():
    agg_champion = {
        (p, role, c): (100, 50)
        for p in ("16.16", "16.15")
        for role in ("JUNGLE", "MIDDLE", "UTILITY", "TOP", "BOTTOM")
        for c in range(1, 11)
    }
    agg_duo = {
        (p, roles, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for roles, a, b in (
            ("jgl_mid", 1, 2),
            ("jgl_sup", 1, 3),
            ("mid_sup", 2, 3),
            ("jgl_mid", 4, 5),
            ("jgl_sup", 4, 6),
            ("mid_sup", 5, 6),
        )
    }
    agg_trio = {(p, 1, 2, 3): (100, 50) for p in ("16.16", "16.15")} | {
        (p, 4, 5, 6): (100, 50) for p in ("16.16", "16.15")
    }
    agg_matchup = {
        (p, role, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for role, a, b in (
            ("JUNGLE", 1, 4),
            ("MIDDLE", 2, 5),
            ("UTILITY", 3, 6),
            ("TOP", 7, 9),
            ("BOTTOM", 8, 10),
        )
    }
    row = {
        "patch": "16.17",
        "us_jgl": 1,
        "us_mid": 2,
        "us_sup": 3,
        "them_jgl": 4,
        "them_mid": 5,
        "them_sup": 6,
        "us_top": 7,
        "us_bot": 8,
        "them_top": 9,
        "them_bot": 10,
    }
    # embeddings_by_patch vide -> pas de vecteurs pour le patch de la ligne.
    result = _row_features_embedding(row, agg_champion, agg_duo, agg_trio, agg_matchup, {}, PATCHES)
    assert result is None


def test_row_features_embedding_complete_matches_feature_names_length():
    agg_champion = {
        (p, role, c): (100, 50)
        for p in ("16.16", "16.15")
        for role in ("JUNGLE", "MIDDLE", "UTILITY", "TOP", "BOTTOM")
        for c in range(1, 11)
    }
    agg_duo = {
        (p, roles, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for roles, a, b in (
            ("jgl_mid", 1, 2),
            ("jgl_sup", 1, 3),
            ("mid_sup", 2, 3),
            ("jgl_mid", 4, 5),
            ("jgl_sup", 4, 6),
            ("mid_sup", 5, 6),
        )
    }
    agg_trio = {(p, 1, 2, 3): (100, 50) for p in ("16.16", "16.15")} | {
        (p, 4, 5, 6): (100, 50) for p in ("16.16", "16.15")
    }
    agg_matchup = {
        (p, role, a, b): (100, 50)
        for p in ("16.16", "16.15")
        for role, a, b in (
            ("JUNGLE", 1, 4),
            ("MIDDLE", 2, 5),
            ("UTILITY", 3, 6),
            ("TOP", 7, 9),
            ("BOTTOM", 8, 10),
        )
    }
    embeddings_by_patch = {
        "16.17": {c: np.zeros(8) for c in range(1, 11)},
    }
    row = {
        "patch": "16.17",
        "us_jgl": 1,
        "us_mid": 2,
        "us_sup": 3,
        "them_jgl": 4,
        "them_mid": 5,
        "them_sup": 6,
        "us_top": 7,
        "us_bot": 8,
        "them_top": 9,
        "them_bot": 10,
    }
    result = _row_features_embedding(
        row, agg_champion, agg_duo, agg_trio, agg_matchup, embeddings_by_patch, PATCHES
    )
    assert result is not None
    assert len(result) == len(EMBEDDING_FEATURE_NAMES)
