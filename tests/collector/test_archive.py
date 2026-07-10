"""Tests de l'archive locale des timelines (JSON.gz, jamais en base)."""

from __future__ import annotations

import gzip
import json

from trio_lab.collector import storage


def test_archive_roundtrip(tmp_path):
    timeline = {"metadata": {"matchId": "EUW1_42"}, "info": {"frames": [{"timestamp": 0}]}}
    path = storage.archive_timeline(tmp_path, "euw1", "16.13", "EUW1_42", timeline)

    assert path == tmp_path / "raw" / "euw1" / "16.13" / "EUW1_42.timeline.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert json.load(fh) == timeline


def test_archive_is_noop_when_file_exists(tmp_path):
    first = {"metadata": {"matchId": "EUW1_42"}}
    storage.archive_timeline(tmp_path, "euw1", "16.13", "EUW1_42", first)
    # Second appel avec un contenu différent : le fichier existant n'est pas réécrit.
    path = storage.archive_timeline(tmp_path, "euw1", "16.13", "EUW1_42", {"metadata": {}})
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert json.load(fh) == first
