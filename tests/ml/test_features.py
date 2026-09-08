from trio_lab.ml.features import (
    FEATURE_NAMES,
    _champion_wr,
    _duo_synergy,
    _matchup_delta,
    _row_features,
    _sum_excluding,
    _trio_wr,
    _wr,
)

PATCHES = ["16.17", "16.16", "16.15"]


def test_wr_none_when_no_games():
    assert _wr(0, 0) is None


def test_wr_basic():
    assert _wr(100, 60) == 0.6


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
        ("16.17", "JUNGLE", 1): (10, 10),
        ("16.16", "JUNGLE", 1): (10, 5),
        ("16.15", "JUNGLE", 1): (10, 5),
    }
    assert _champion_wr(agg_champion, PATCHES, "16.17", "jgl", 1) == 0.5


def test_champion_wr_none_when_only_seen_on_the_excluded_patch():
    agg_champion = {("16.17", "JUNGLE", 1): (10, 10)}
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
        ("16.16", "JUNGLE", 1, 9): (10, 8),  # champ 1 vs champ 9 : wr 0.8
        ("16.15", "JUNGLE", 1, 9): (10, 8),
    }
    agg_champion = {
        ("16.16", "JUNGLE", 1): (100, 50),  # baseline wr 0.5
        ("16.15", "JUNGLE", 1): (100, 50),
    }
    delta = _matchup_delta(agg_matchup, agg_champion, PATCHES, "16.17", "jgl", 1, 9)
    assert delta is not None
    assert round(delta, 2) == 0.3  # 0.8 - 0.5


def test_row_features_none_when_any_axis_incomplete():
    # Aucune donnée fournie nulle part -> toutes les baselines manquent.
    row = {
        "patch": "16.17",
        "us_jgl": 1,
        "us_mid": 2,
        "us_sup": 3,
        "them_jgl": 4,
        "them_mid": 5,
        "them_sup": 6,
    }
    assert _row_features(row, {}, {}, {}, {}, PATCHES) is None


def test_feature_names_length_matches_row_features_output():
    # Garde-fou : si on ajoute/retire une feature dans _row_features sans
    # mettre à jour FEATURE_NAMES (ou l'inverse), ce test casse.
    agg_champion = {
        (p, role, c): (10, 5)
        for p in ("16.16", "16.15")
        for role in ("JUNGLE", "MIDDLE", "UTILITY")
        for c in range(1, 7)
    }
    agg_duo = {
        (p, roles, a, b): (10, 5)
        for p in ("16.16", "16.15")
        for roles, a, b in (("jgl_mid", 1, 2), ("jgl_sup", 1, 3), ("mid_sup", 2, 3))
    }
    agg_duo.update(
        {
            (p, roles, a, b): (10, 5)
            for p in ("16.16", "16.15")
            for roles, a, b in (("jgl_mid", 4, 5), ("jgl_sup", 4, 6), ("mid_sup", 5, 6))
        }
    )
    agg_trio = {(p, 1, 2, 3): (10, 5) for p in ("16.16", "16.15")} | {
        (p, 4, 5, 6): (10, 5) for p in ("16.16", "16.15")
    }
    agg_matchup = {
        (p, role, a, b): (10, 5)
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
    }
    result = _row_features(row, agg_champion, agg_duo, agg_trio, agg_matchup, PATCHES)
    assert result is not None
    assert len(result) == len(FEATURE_NAMES)
