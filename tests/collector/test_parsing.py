"""Tests d'extraction des lignes `matches` / `match_participants`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trio_lab.collector import parsing
from trio_lab.collector.parsing import ParseError

from ._builders import build_detail

# --- match_row ---


def test_match_row_extracts_expected_columns():
    detail = build_detail("EUW1_42", patch="16.13", duration_s=1815, winning_team=200)
    row = parsing.match_row(detail, platform="euw1")
    assert row == {
        "match_id": "EUW1_42",
        "platform": "euw1",
        "patch": "16.13",
        "game_version": "16.13.673.9817",
        "queue_id": 420,
        "game_creation": datetime.fromtimestamp(1_780_000_000, tz=UTC),
        "game_duration_s": 1815,
        "winning_team": 200,
    }


def test_match_row_handles_ms_duration_without_end_timestamp():
    """Piège s/ms : sans gameEndTimestamp, gameDuration est en millisecondes."""
    detail = build_detail(duration_s=0)
    detail["info"]["gameDuration"] = 1_815_000
    del detail["info"]["gameEndTimestamp"]
    assert parsing.match_row(detail, platform="euw1")["game_duration_s"] == 1815


def test_match_row_no_winner_raises():
    detail = build_detail()
    for team in detail["info"]["teams"]:
        team["win"] = False
    with pytest.raises(ParseError, match="gagnante"):
        parsing.match_row(detail, platform="euw1")


def test_match_row_two_winners_raises():
    detail = build_detail()
    for team in detail["info"]["teams"]:
        team["win"] = True
    with pytest.raises(ParseError, match="gagnante"):
        parsing.match_row(detail, platform="euw1")


# --- participant_rows ---


def test_participant_rows_full_teams():
    rows = parsing.participant_rows(build_detail("EUW1_42", winning_team=100))
    assert len(rows) == 10
    assert all(r["match_id"] == "EUW1_42" for r in rows)
    # 5 rôles distincts par équipe, win cohérent avec l'équipe gagnante.
    by_team = {100: set(), 200: set()}
    for r in rows:
        by_team[r["team_id"]].add(r["role"])
        assert r["win"] is (r["team_id"] == 100)
        assert isinstance(r["champion_id"], int)
        # CC empirique par champion (builder : cc = 2×pid, immobilizations = pid).
        assert r["cc_time_s"] == 2 * r["immobilizations"]
    assert by_team[100] == by_team[200] == set(parsing.ROLES)


def test_participant_rows_empty_role_raises():
    """teamPosition vide (AFK précoce, données dégradées) → ParseError → exclusion."""
    detail = build_detail()
    detail["info"]["participants"][0]["teamPosition"] = ""
    with pytest.raises(ParseError, match="incohérents"):
        parsing.participant_rows(detail)


def test_participant_rows_duplicate_role_raises():
    detail = build_detail()
    detail["info"]["participants"][0]["teamPosition"] = "JUNGLE"  # 2 JUNGLE team 100
    with pytest.raises(ParseError):
        parsing.participant_rows(detail)


def test_participant_rows_missing_participant_raises():
    detail = build_detail()
    detail["info"]["participants"].pop()
    with pytest.raises(ParseError):
        parsing.participant_rows(detail)
