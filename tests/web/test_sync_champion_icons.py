"""Tests de `web.sync_champion_icons` : réseau simulé, écriture sur disque
réelle (répertoire temporaire)."""

from __future__ import annotations

from trio_lab.web import sync_champion_icons as sync


def _fake_get_json(url: str):
    if "versions.json" in url:
        return ["16.15.1"]
    return {
        "data": {
            "Ahri": {"image": {"full": "Ahri.png"}},
            "Zed": {"image": {"full": "Zed.png"}},
        }
    }


def test_sync_downloads_missing_icons(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "ICON_DIR", tmp_path)
    monkeypatch.setattr(sync, "get_json", _fake_get_json)
    monkeypatch.setattr(
        sync.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(b"fake-png-bytes")
    )
    added = sync.sync()
    assert sorted(added) == ["Ahri.png", "Zed.png"]
    assert (tmp_path / "Ahri.png").read_bytes() == b"fake-png-bytes"
    assert (tmp_path / "Zed.png").read_bytes() == b"fake-png-bytes"


def test_sync_skips_already_present_icons(tmp_path, monkeypatch):
    (tmp_path / "Ahri.png").write_bytes(b"already-there")
    monkeypatch.setattr(sync, "ICON_DIR", tmp_path)
    monkeypatch.setattr(sync, "get_json", _fake_get_json)
    monkeypatch.setattr(
        sync.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(b"fresh-download")
    )
    added = sync.sync()
    assert added == ["Zed.png"]
    # Le fichier déjà présent n'est jamais retéléchargé/écrasé.
    assert (tmp_path / "Ahri.png").read_bytes() == b"already-there"


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._payload
