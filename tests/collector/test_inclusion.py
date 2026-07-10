"""Tests des critères d'inclusion : queue, durée (piège s/ms), FF, patch."""

from __future__ import annotations

from trio_lab.collector import inclusion

PATCH = "16.13"


def _info(**overrides):
    base = {
        "queueId": 420,
        "gameEndedInEarlySurrender": False,
        "gameDuration": 1800,  # secondes (gameEndTimestamp présent)
        "gameEndTimestamp": 1_780_001_800_000,
        "gameVersion": f"{PATCH}.673.9817",
    }
    base.update(overrides)
    return {"info": base}


def test_patch_of():
    assert inclusion.patch_of("16.13.673.9817") == "16.13"


def test_game_duration_seconds_when_end_timestamp_present():
    assert inclusion.game_duration_ms(_info()["info"]) == 1_800_000


def test_game_duration_already_ms_without_end_timestamp():
    info = _info(gameDuration=1_800_000)["info"]
    del info["gameEndTimestamp"]
    assert inclusion.game_duration_ms(info) == 1_800_000


def test_valid_match_is_included():
    assert inclusion.is_included(_info(), PATCH) == (True, None)


def test_wrong_queue_excluded():
    assert inclusion.is_included(_info(queueId=440), PATCH) == (False, "queue")


def test_early_surrender_excluded():
    included, reason = inclusion.is_included(_info(gameEndedInEarlySurrender=True), PATCH)
    assert (included, reason) == (False, "early_surrender")


def test_too_short_excluded():
    assert inclusion.is_included(_info(gameDuration=299), PATCH) == (False, "duration")


def test_wrong_patch_excluded():
    detail = _info(gameVersion="16.12.100.1")
    assert inclusion.is_included(detail, PATCH) == (False, "wrong_patch")
