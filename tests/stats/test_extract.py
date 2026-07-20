"""Tests unitaires de l'extraction (timelines synthétiques, valeurs vérifiables à la main).

Rappel builder : trio équipe 100 = pids (2, 3, 5) / champions (2, 3, 5) ;
trio équipe 200 = pids (7, 8, 10) / champions (12, 13, 15) ;
totalGold(minute, pid) = minute × (100 + pid).
"""

from __future__ import annotations

import pytest

from trio_lab.collector.parsing import ParseError
from trio_lab.stats import extract

from ..collector._builders import build_detail
from ._builders import build_timeline, first_blood, kill, monster, tower, ward_kill, ward_placed

# --- trios_of ---


def test_trios_of_maps_roles_to_pids_and_champions():
    trios = extract.trios_of(build_detail())
    assert trios[100].pids == (2, 3, 5)
    assert trios[100].champion_ids == (2, 3, 5)
    assert trios[200].pids == (7, 8, 10)
    assert trios[200].champion_ids == (12, 13, 15)


def test_trios_of_missing_role_raises():
    detail = build_detail()
    detail["info"]["participants"][4]["teamPosition"] = ""  # UTILITY équipe 100
    with pytest.raises(ParseError, match="UTILITY"):
        extract.trios_of(detail)


def test_trios_of_duplicate_role_raises():
    detail = build_detail()
    detail["info"]["participants"][0]["teamPosition"] = "JUNGLE"
    with pytest.raises(ParseError, match="dupliqué"):
        extract.trios_of(detail)


# --- objective_events : attribution et ordre ---


def test_objective_events_attribution_and_order():
    timeline = build_timeline(
        events=[
            tower(owner_team=200, ts_s=800),  # prise par l'équipe 100 (opposé du propriétaire)
            monster("DRAGON", 200, 500, subtype="WATER_DRAGON"),
            monster("HORDE", 100, 540),
            first_blood(killer=9, ts_s=61),  # équipe du tueur (pid 9 → 200)
            monster("DRAGON", 100, 1900, subtype="ELDER_DRAGON"),
            monster("BARON_NASHOR", 200, 1600),
        ]
    )
    events = extract.objective_events(timeline)

    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5, 6]
    assert [(e["event_type"], e["team_id"], e["ts_s"]) for e in events] == [
        ("FIRST_BLOOD", 200, 61),
        ("DRAGON", 200, 500),
        ("VOID_GRUB", 100, 540),
        ("TOWER", 100, 800),
        ("BARON", 200, 1600),
        ("ELDER_DRAGON", 100, 1900),
    ]
    dragon = events[1]
    assert dragon["subtype"] == "ocean"  # WATER_DRAGON → nom interne macro-lab
    assert (dragon["pos_x"], dragon["pos_y"]) == (1, 2)
    assert events[3]["subtype"] == "OUTER_TURRET"


def test_objective_events_ignores_unknown_and_unattributed():
    timeline = build_timeline(
        events=[
            monster("TOTALLY_NEW_MONSTER", 100, 100),
            {**monster("DRAGON", 100, 200, subtype="FIRE_DRAGON"), "killerTeamId": 0},
            {
                "type": "BUILDING_KILL",
                "buildingType": "INHIBITOR_BUILDING",
                "teamId": 100,
                "timestamp": 300_000,
            },
        ]
    )
    assert extract.objective_events(timeline) == []


# --- team_objectives ---


def _obj_events(*specs):
    """(event_type, team, ts_s) → lignes triées comme en sortie d'objective_events."""
    rows = [
        {"event_type": etype, "team_id": team, "ts_s": ts, "subtype": None}
        for etype, team, ts in specs
    ]
    return sorted(rows, key=lambda r: r["ts_s"])


def test_soul_requires_four_drakes():
    three = extract.team_objectives(_obj_events(*[("DRAGON", 100, t) for t in (300, 600, 900)]))
    assert three[100]["drakes_taken"] == 3
    assert three[100]["soul_taken"] is False

    four = extract.team_objectives(
        _obj_events(*[("DRAGON", 100, t) for t in (300, 600, 900, 1200)], ("DRAGON", 200, 450))
    )
    assert four[100]["soul_taken"] is True
    assert four[200] == {**four[200], "drakes_taken": 1, "soul_taken": False}


def test_first_nashor_and_first_tower_are_exclusive():
    stats = extract.team_objectives(
        _obj_events(
            ("BARON", 200, 1500),
            ("BARON", 100, 1900),
            ("TOWER", 100, 800),
            ("TOWER", 200, 900),
            ("TOWER", 200, 950),
        )
    )
    assert stats[200]["nashor_first"] is True
    assert stats[200]["nashor_first_s"] == 1500
    assert stats[100]["nashor_first"] is False
    assert stats[100]["nashor_first_s"] is None
    assert stats[100]["first_tower"] is True
    assert stats[200]["first_tower"] is False
    assert stats[100]["towers_destroyed"] == 1
    assert stats[200]["towers_destroyed"] == 2


def test_grubs_herald_counters():
    stats = extract.team_objectives(
        _obj_events(
            ("VOID_GRUB", 100, 500),
            ("VOID_GRUB", 100, 510),
            ("VOID_GRUB", 200, 520),
            ("RIFT_HERALD", 200, 960),
        )
    )
    assert stats[100]["grubs_taken"] == 2
    assert stats[200]["grubs_taken"] == 1
    assert stats[200]["herald_taken"] is True
    assert stats[100]["herald_taken"] is False


# --- pre15_stats (migration 030) ---


def test_pre15_stats_ignores_objectives_after_cutoff():
    # Héraut équipe 200 à 12:00 (avant coupure) ; drakes équipe 100 à 3:00 et
    # 9:00 (avant) puis 16:00 (après, exclu) ; équipe 200 sans dragon.
    events = _obj_events(
        ("RIFT_HERALD", 200, 720),
        ("DRAGON", 100, 180),
        ("DRAGON", 100, 540),
        ("DRAGON", 100, 960),  # 16:00, après la coupure à 15 min (900s)
    )
    timeline = build_timeline(events=[])
    stats = extract.pre15_stats(timeline, events)

    assert stats[200]["herald_taken_pre15"] is True
    assert stats[100]["herald_taken_pre15"] is False
    assert stats[100]["dragons_taken_pre15"] == 2
    assert stats[200]["dragons_taken_pre15"] == 0


def test_pre15_stats_counts_wards_before_cutoff_only():
    # pid 2 (équipe 100) pose 2 wards avant 15 min, 1 après (exclue) ; pid 8
    # (équipe 200) détruit 1 ward avant 15 min.
    timeline = build_timeline(
        events=[
            ward_placed(2, 300),
            ward_placed(2, 600),
            ward_placed(2, 960),  # 16:00, après la coupure
            ward_kill(8, 700),
        ]
    )
    stats = extract.pre15_stats(timeline, events=[])

    assert stats[100]["wards_pre15"] == 2
    assert stats[200]["wards_pre15"] == 1


# --- gold_diffs ---


def test_gold_diffs_values_and_truncation():
    # trio 100 = pids (2,3,5) → Σ(100+pid) = 310 ; trio 200 = (7,8,10) → 325.
    # diff équipe 100 à la minute m = m×310 − m×325 = −15m.
    timeline = build_timeline(minutes=20)
    trios = extract.trios_of(build_detail())
    diffs = extract.gold_diffs(timeline, trios)

    assert diffs[100]["gold_diff_5"] == -75
    assert diffs[100]["gold_diff_10"] == -150
    assert diffs[100]["gold_diff_20"] == -300
    assert diffs[200]["gold_diff_20"] == 300  # symétrique
    for minute in (25, 30, 35):  # partie finie à 20 min
        assert diffs[100][f"gold_diff_{minute}"] is None
        assert diffs[200][f"gold_diff_{minute}"] is None


# --- combat_stats ---


def test_kill_participation_pre15_window_and_attribution():
    timeline = build_timeline(
        events=[
            kill(killer=2, victim=7, ts_s=100),  # jgl 100 : kill trio
            kill(killer=1, victim=6, ts_s=200),  # top 100 sans assist : hors trio
            kill(killer=0, victim=3, ts_s=300),  # exécution d'un membre 100 → kill équipe 200
            kill(killer=9, victim=4, ts_s=400, assists=[10]),  # assist sup 200 : trio
            kill(killer=6, victim=1, ts_s=900),  # à 15:00 pile → hors fenêtre
        ]
    )
    stats = extract.combat_stats(build_detail(), timeline, extract.trios_of(build_detail()))
    assert stats[100]["kill_participation_pre15"] == pytest.approx(0.5)  # 1 / 2
    assert stats[200]["kill_participation_pre15"] == pytest.approx(0.5)  # 1 / 2


def test_kill_participation_none_without_kills():
    stats = extract.combat_stats(build_detail(), build_timeline(), extract.trios_of(build_detail()))
    assert stats[100]["kill_participation_pre15"] is None
    assert stats[200]["kill_participation_pre15"] is None


def test_endgame_stats_sums_and_shares():
    detail = build_detail()
    detail["info"]["participants"][4]["firstBloodAssist"] = True  # pid 5 = sup 100 (trio)
    detail["info"]["participants"][5]["firstBloodKill"] = True  # pid 6 = top 200 (hors trio)
    stats = extract.combat_stats(detail, build_timeline(), extract.trios_of(detail))

    # Builder : visionScore=pid, cc=2×pid, dégâts=1000×pid, 1 plaque/joueur.
    assert stats[100]["vision_score"] == 2 + 3 + 5
    assert stats[200]["vision_score"] == 7 + 8 + 10
    assert stats[100]["cc_time_s"] == 2 * (2 + 3 + 5)
    # Ventilation par membre (migration 020) : jgl=pid2, mid=pid3, sup=pid5.
    assert (
        stats[100]["jgl_cc_time_s"],
        stats[100]["mid_cc_time_s"],
        stats[100]["sup_cc_time_s"],
    ) == (
        2 * 2,
        2 * 3,
        2 * 5,
    )
    assert stats[100]["damage_share"] == pytest.approx(10 / 15)
    assert stats[200]["damage_share"] == pytest.approx(25 / 40)
    assert stats[100]["plates_taken"] == 5
    assert stats[100]["first_blood_trio"] is True  # assist du support
    assert stats[200]["first_blood_trio"] is False  # FB par le top, hors trio


def test_dmg_per_gold_and_wards_per_member():
    detail = build_detail()
    stats = extract.combat_stats(detail, build_timeline(), extract.trios_of(detail))

    # Builder : dégâts=1000×pid, gold=1000+200×pid ; jgl=pid2, mid=pid3, sup=pid5 (équipe 100).
    assert stats[100]["jgl_dmg_per_gold"] == pytest.approx(2000 / 1400)
    assert stats[100]["mid_dmg_per_gold"] == pytest.approx(3000 / 1600)
    assert stats[100]["sup_dmg_per_gold"] == pytest.approx(5000 / 2000)

    # wardsPlaced=pid+10, wardsKilled=pid ; trio 100 = pids (2,3,5).
    assert stats[100]["wards_placed"] == (2 + 10) + (3 + 10) + (5 + 10)
    assert stats[100]["wards_killed"] == 2 + 3 + 5
    assert stats[200]["wards_placed"] == (7 + 10) + (8 + 10) + (10 + 10)
    assert stats[200]["wards_killed"] == 7 + 8 + 10


def test_dmg_per_gold_none_when_gold_earned_is_zero():
    detail = build_detail()
    detail["info"]["participants"][1]["goldEarned"] = 0  # pid 2 = jgl équipe 100
    stats = extract.combat_stats(detail, build_timeline(), extract.trios_of(detail))
    assert stats[100]["jgl_dmg_per_gold"] is None


# --- jungle_cs_diff ---


def test_jungle_cs_diff_at_15_minutes():
    # jungleMinionsKilled(minute, pid) = minute × pid ; jgl 100 = pid 2, jgl 200 = pid 7.
    timeline = build_timeline(minutes=20)
    trios = extract.trios_of(build_detail())
    diff = extract.jungle_cs_diff(timeline, trios)
    assert diff[100] == 15 * 2 - 15 * 7
    assert diff[200] == 15 * 7 - 15 * 2


def test_jungle_cs_diff_none_before_15_minutes():
    timeline = build_timeline(minutes=10)
    trios = extract.trios_of(build_detail())
    diff = extract.jungle_cs_diff(timeline, trios)
    assert diff[100] is None
    assert diff[200] is None


def test_cc_time_s_applies_per_champion_reliability():
    """championId == participantId côté équipe 100 (builder) : jgl=2, mid=3, sup=5."""
    detail = build_detail()
    stats = extract.combat_stats(
        detail, build_timeline(), extract.trios_of(detail), cc_reliability={2: 0.5}
    )
    # jgl (champ 2, cc brut 4) atténué à 2 ; mid (6) et sup (10) inchangés (défaut 1.0).
    assert stats[100]["cc_time_s"] == 2 + 6 + 10
    # La ventilation par membre reflète la même atténuation (pas juste le total).
    assert stats[100]["jgl_cc_time_s"] == 2
    assert (stats[100]["mid_cc_time_s"], stats[100]["sup_cc_time_s"]) == (6, 10)
    # Sans reliability fournie : comportement identique à avant (aucune atténuation).
    baseline = extract.combat_stats(detail, build_timeline(), extract.trios_of(detail))
    assert baseline[100]["cc_time_s"] == 2 * (2 + 3 + 5)


# --- extract_match ---


def test_extract_match_rejects_mismatched_ids():
    with pytest.raises(ParseError, match="timeline"):
        extract.extract_match(build_detail("EUW1_A"), build_timeline("EUW1_B"))


def test_extract_match_assembles_full_rows():
    detail = build_detail("EUW1_42", winning_team=200)
    timeline = build_timeline(
        "EUW1_42",
        events=[monster("DRAGON", 200, 500, subtype="FIRE_DRAGON"), tower(100, 800)],
    )
    trio_rows, event_rows = extract.extract_match(detail, timeline)

    assert [r["team_id"] for r in trio_rows] == [100, 200]
    row_200 = trio_rows[1]
    assert row_200["match_id"] == "EUW1_42"
    assert (row_200["jgl_champion"], row_200["mid_champion"], row_200["sup_champion"]) == (
        12,
        13,
        15,
    )
    assert row_200["win"] is True
    assert trio_rows[0]["win"] is False
    assert row_200["drakes_taken"] == 1
    assert row_200["towers_destroyed"] == 1  # tour du propriétaire 100 prise par 200
    assert row_200["gold_diff_10"] == 150

    assert [(e["event_type"], e["seq"]) for e in event_rows] == [("DRAGON", 1), ("TOWER", 2)]
    assert all(e["match_id"] == "EUW1_42" for e in event_rows)
