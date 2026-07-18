"""Tests du résumé statistique détaillé d'un trio (module pur)."""

from __future__ import annotations

import pytest

from trio_lab.web import summary


def _row(patch="16.13", win=True, duration=1800, **overrides):
    row = {
        "patch": patch,
        "game_duration_s": duration,
        "win": win,
        "gold_diff_5": None,
        "gold_diff_10": None,
        "grubs_taken": None,
        "herald_taken": None,
        "drakes_taken": None,
        "soul_taken": None,
        "nashor_first": None,
        "nashor_first_s": None,
        "first_tower": None,
        "towers_destroyed": None,
        "plates_taken": None,
        "first_blood_trio": None,
        "kill_participation_pre15": None,
        "damage_share": None,
        "vision_score": None,
        "cc_time_s": None,
    }
    row.update(overrides)
    return row


def test_weighted_mean_ignores_nulls_and_zero_weight_patches():
    rows = [
        _row(gold_diff_10=1000),
        _row(gold_diff_10=None),  # partie sans la stat : ignorée
        _row(patch="16.12", gold_diff_10=-500),  # poids 0.6
        _row(patch="16.11", gold_diff_10=99999),  # hors fenêtre : ignorée
    ]
    stats = summary.summarize(rows, {"16.13": 1.0, "16.12": 0.6})
    # (1000×1.0 + (−500)×0.6) / 1.6 = 437.5
    assert stats["gold_diff"][10] == pytest.approx(437.5)
    assert stats["gold_diff"][5] is None  # jamais renseignée
    assert stats["games"] == 3  # la ligne 16.11 ne compte pas


def test_rates_and_wr():
    rows = [
        _row(win=True, herald_taken=True, soul_taken=True),
        _row(win=False, herald_taken=False, soul_taken=False),
        _row(win=False, herald_taken=False, soul_taken=False),
        _row(win=True, herald_taken=True, soul_taken=False),
    ]
    stats = summary.summarize(rows, {"16.13": 1.0})
    assert stats["wr"] == pytest.approx(0.5)
    assert stats["herald_taken"] == pytest.approx(0.5)
    assert stats["soul_taken"] == pytest.approx(0.25)
    # WR sans l'âme : 1 win sur les 3 parties où soul_taken est False.
    assert stats["wr_without_soul"] == pytest.approx(1 / 3)
    # WR avec l'âme : 1 win sur l'unique partie où soul_taken est True.
    assert stats["wr_with_soul"] == pytest.approx(1.0)


def test_tempo_split_wins_losses():
    rows = [
        _row(win=True, duration=1500),
        _row(win=True, duration=1700),
        _row(win=False, duration=2100),
    ]
    stats = summary.summarize(rows, {"16.13": 1.0})
    assert stats["avg_duration_win_s"] == pytest.approx(1600.0)
    assert stats["avg_duration_loss_s"] == pytest.approx(2100.0)


def test_vision_score_is_per_minute_not_cumulative():
    rows = [
        _row(duration=1500, vision_score=90),  # 90 / 25 min = 3.6 / min
        _row(duration=2100, vision_score=70),  # 70 / 35 min = 2.0 / min
    ]
    stats = summary.summarize(rows, {"16.13": 1.0})
    assert stats["vision_score"] == pytest.approx(2.8)  # (3.6 + 2.0) / 2


def test_drakes_and_cc_time_s_are_per_minute_not_cumulative():
    rows = [
        _row(duration=1500, drakes_taken=2, cc_time_s=75),  # 25 min : 0.08/min, 3.0 s/min
        _row(duration=2100, drakes_taken=3, cc_time_s=70),  # 35 min : 0.0857.../min, 2.0 s/min
    ]
    stats = summary.summarize(rows, {"16.13": 1.0})
    assert stats["drakes_taken"] == pytest.approx((2 / 25 + 3 / 35) / 2)
    assert stats["cc_time_s"] == pytest.approx((3.0 + 2.0) / 2)


def test_empty_rows_yield_none_everywhere():
    stats = summary.summarize([], {"16.13": 1.0})
    assert stats["games"] == 0
    assert stats["wr"] is None
    assert stats["avg_duration_win_s"] is None
    assert all(v is None for v in stats["gold_diff"].values())
    assert all(n == 0 for n in stats["gold_diff_n"].values())


def test_gold_diff_n_counts_games_reaching_each_checkpoint():
    rows = [
        _row(gold_diff_5=100, gold_diff_10=200),  # partie longue : les 2 checkpoints atteints
        _row(gold_diff_5=50, gold_diff_10=None),  # finie avant 10 min
        _row(patch="16.12", gold_diff_5=10, gold_diff_10=20),  # poids 0.6, compte quand même
        _row(patch="16.11", gold_diff_5=999, gold_diff_10=999),  # hors fenêtre : ignorée
    ]
    stats = summary.summarize(rows, {"16.13": 1.0, "16.12": 0.6})
    # brut, PAS pondéré : 3 games dans la fenêtre ont gold_diff_5, 2 ont gold_diff_10.
    assert stats["gold_diff_n"][5] == 3
    assert stats["gold_diff_n"][10] == 2
    assert stats["gold_diff_n"][15] == 0  # jamais renseignée
