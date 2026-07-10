"""Tests d'extraction sur les deux matchs réels archivés (patch 16.13, euw1).

Les valeurs attendues sont la **vérité-terrain calculée indépendamment** depuis
les JSON bruts (script de contrôle, session Phase 2) — pas la sortie de
l'extracteur lui-même. Les fixtures sont anonymisées (PII retirée).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from trio_lab.stats import extract

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "riot"


def _load(match_id: str, kind: str) -> dict:
    with gzip.open(FIXTURES / f"{match_id}.{kind}.json.gz", "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _rows(match_id: str):
    trio_rows, event_rows = extract.extract_match(
        _load(match_id, "detail"), _load(match_id, "timeline")
    )
    return {r["team_id"]: r for r in trio_rows}, event_rows


# --- EUW1_7913858220 : 22 min, victoire 200, ni Nashor ni âme ---


def test_game1_trios_and_outcome():
    rows, _ = _rows("EUW1_7913858220")
    r100, r200 = rows[100], rows[200]
    assert (r100["jgl_champion"], r100["mid_champion"], r100["sup_champion"]) == (876, 126, 555)
    assert (r200["jgl_champion"], r200["mid_champion"], r200["sup_champion"]) == (246, 18, 60)
    assert r100["win"] is False and r200["win"] is True


def test_game1_objectives():
    rows, _ = _rows("EUW1_7913858220")
    r100, r200 = rows[100], rows[200]
    assert (r100["grubs_taken"], r200["grubs_taken"]) == (3, 0)
    assert r100["herald_taken"] is True and r200["herald_taken"] is False
    assert (r100["drakes_taken"], r200["drakes_taken"]) == (0, 3)
    assert r100["soul_taken"] is False and r200["soul_taken"] is False  # 3 drakes ≠ âme
    assert r100["atakhan_taken"] is False and r200["atakhan_taken"] is False
    assert r100["nashor_first"] is False and r200["nashor_first"] is False
    assert r100["nashor_first_s"] is None and r200["nashor_first_s"] is None
    assert r100["first_tower"] is True  # première tour (owner 200) à 819 s
    assert (r100["towers_destroyed"], r200["towers_destroyed"]) == (4, 4)


def test_game1_gold_and_combat():
    rows, _ = _rows("EUW1_7913858220")
    r100, r200 = rows[100], rows[200]
    assert r100["gold_diff_10"] == -392
    assert r200["gold_diff_10"] == 392
    assert r100["gold_diff_20"] is not None
    # Dernière frame complète = minute 22 → rien au-delà.
    for minute in (25, 30, 35):
        assert r100[f"gold_diff_{minute}"] is None

    assert r100["kill_participation_pre15"] == pytest.approx(8 / 9)
    assert r200["kill_participation_pre15"] == pytest.approx(14 / 19)
    # FB : kill du pid 9 (BOTTOM) avec assist du pid 10 (UTILITY → trio 200).
    assert r100["first_blood_trio"] is False and r200["first_blood_trio"] is True
    assert (r100["vision_score"], r200["vision_score"]) == (127, 115)
    assert (r100["cc_time_s"], r200["cc_time_s"]) == (37, 36)
    assert (r100["plates_taken"], r200["plates_taken"]) == (34, 25)
    assert r100["damage_share"] == pytest.approx(0.6306, abs=1e-4)
    assert r200["damage_share"] == pytest.approx(0.4748, abs=1e-4)


def test_game1_event_log():
    _, events = _rows("EUW1_7913858220")
    # 1 FB + 3 drakes + 3 grubs + 1 héraut + 8 tours = 16 events, seq continu.
    assert len(events) == 16
    assert [e["seq"] for e in events] == list(range(1, 17))
    assert events[0]["event_type"] == "FIRST_BLOOD" and events[0]["ts_s"] == 61
    drakes = [e for e in events if e["event_type"] == "DRAGON"]
    assert [d["subtype"] for d in drakes] == ["ocean", "mountain", "hextech"]
    assert all(d["team_id"] == 200 for d in drakes)


# --- EUW1_7913889450 : 34 min, victoire 200, 2 Nashors ---


def test_game2_objectives_and_nashor():
    rows, _ = _rows("EUW1_7913889450")
    r100, r200 = rows[100], rows[200]
    assert (r100["jgl_champion"], r100["mid_champion"], r100["sup_champion"]) == (254, 103, 24)
    assert (r200["jgl_champion"], r200["mid_champion"], r200["sup_champion"]) == (64, 910, 111)
    assert (r100["grubs_taken"], r200["grubs_taken"]) == (1, 2)
    assert (r100["drakes_taken"], r200["drakes_taken"]) == (3, 1)
    assert r100["soul_taken"] is False and r200["soul_taken"] is False
    assert r200["nashor_first"] is True and r200["nashor_first_s"] == 1484
    assert r100["nashor_first"] is False and r100["nashor_first_s"] is None
    assert r100["first_tower"] is True
    assert (r100["towers_destroyed"], r200["towers_destroyed"]) == (6, 5)


def test_game2_gold_and_combat():
    rows, _ = _rows("EUW1_7913889450")
    r100, r200 = rows[100], rows[200]
    assert r100["gold_diff_10"] == -1645
    assert r100["gold_diff_30"] is not None
    assert r100["gold_diff_35"] is None  # dernière frame complète = minute 34

    assert r100["kill_participation_pre15"] == pytest.approx(5 / 6)
    assert r200["kill_participation_pre15"] == pytest.approx(1.0)
    assert r100["first_blood_trio"] is True  # assist du pid 5 (UTILITY 100)
    assert (r100["vision_score"], r200["vision_score"]) == (199, 201)
    assert (r100["cc_time_s"], r200["cc_time_s"]) == (89, 146)
    assert (r100["plates_taken"], r200["plates_taken"]) == (51, 32)
    assert r100["damage_share"] == pytest.approx(0.4233, abs=1e-4)
    assert r200["damage_share"] == pytest.approx(0.5713, abs=1e-4)
