"""Tests de la fenêtre multi-patchs (poids décroissants, coupure de rework)."""

from __future__ import annotations

import pytest

from trio_lab.synergy import windows


def test_make_window_label_and_weights():
    window = windows.make_window(["16.14", "16.13", "16.12"])
    assert window.label == "16.14+16.13+16.12"
    assert window.weights == (1.0, 0.6, 0.35)
    single = windows.make_window(["16.13"])
    assert single.label == "16.13"
    assert single.weights == (1.0,)


def test_window_rejects_bad_input():
    with pytest.raises(ValueError, match="1 à 3"):
        windows.make_window([])
    with pytest.raises(ValueError, match="1 à 3"):
        windows.make_window(["16.14", "16.13", "16.12", "16.11"])
    with pytest.raises(ValueError, match="ordonnés"):
        windows.make_window(["16.12", "16.13"])


def test_patch_key_orders_numerically():
    # Tri numérique, pas lexicographique : 16.9 < 16.10.
    assert windows.patch_key("16.9") < windows.patch_key("16.10")


def test_weights_for_without_rework():
    window = windows.make_window(["16.13", "16.12"])
    assert window.weights_for((1, 2, 3)) == {"16.13": 1.0, "16.12": 0.6}


def test_weights_for_cuts_window_before_rework(monkeypatch):
    # Champion 42 retravaillé au 16.13 → le 16.12 est exclu de ses fenêtres.
    monkeypatch.setattr(windows, "REWORKS", {42: "16.13"})
    window = windows.make_window(["16.13", "16.12"])
    assert window.weights_for((1, 42)) == {"16.13": 1.0, "16.12": 0.0}
    # Les combinaisons sans le champion retravaillé gardent la fenêtre entière.
    assert window.weights_for((1, 2)) == {"16.13": 1.0, "16.12": 0.6}
