"""Tests du mapping patch → fenêtre temporelle."""

from __future__ import annotations

from datetime import UTC

import pytest

from trio_lab.collector import patches


def test_bounds_are_tz_aware_and_ordered():
    for patch, (start, end) in patches.PATCH_DATES.items():
        assert start.tzinfo == UTC and end.tzinfo == UTC, patch
        assert start < end, patch


def test_epoch_bounds_for_known_patch():
    start_s, end_s = patches.epoch_bounds_for("16.13")
    assert isinstance(start_s, int) and isinstance(end_s, int)
    assert start_s < end_s


def test_unknown_patch_raises_with_hint():
    with pytest.raises(ValueError, match="9.99"):
        patches.bounds_for("9.99")
