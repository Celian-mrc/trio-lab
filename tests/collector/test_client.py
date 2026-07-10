"""Tests du client API Riot — transport aiohttp de pulsefire mocké via aioresponses.

Aucun appel réseau réel. Le back-off (429/5xx) est prouvé de façon déterministe
en neutralisant `asyncio.sleep` (fixture `no_sleep`) et en enchaînant des
réponses mockées erreur → succès.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from aioresponses import aioresponses

from trio_lab.collector import client as cl
from trio_lab.collector.client import RiotClient, regional_for

API_KEY = "RGAPI-test-key"

# URL de référence (league-v4 apex, routing platform) réutilisée par plusieurs tests.
APEX_URL = "https://kr.api.riotgames.com/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5"


@pytest.fixture
def no_sleep(mocker):
    """Neutralise le back-off exponentiel (asyncio.sleep) pour des tests rapides.

    Retourne le mock pour vérifier que le back-off a bien été déclenché.
    """

    async def _noop(*_args, **_kwargs):
        return None

    return mocker.patch("pulsefire.middlewares.asyncio.sleep", side_effect=_noop)


# --- mapping platform → regional ---


def test_regional_for_known_platforms():
    assert regional_for("euw1") == "europe"
    assert regional_for("kr") == "asia"
    assert regional_for("na1") == "americas"


def test_regional_for_unknown_raises():
    with pytest.raises(ValueError, match="oce42"):
        regional_for("oce42")


# --- clé API ---


def test_missing_api_key_raises(monkeypatch):
    """Sans clé (ni argument ni .env), l'instanciation échoue explicitement."""
    monkeypatch.setattr(cl.config, "RIOT_API_KEY", None)
    with pytest.raises(RuntimeError, match="RIOT_API_KEY"):
        RiotClient()


def test_explicit_api_key_overrides_config(monkeypatch):
    monkeypatch.setattr(cl.config, "RIOT_API_KEY", None)
    # Ne doit pas lever : la clé est fournie en argument.
    RiotClient(api_key=API_KEY)


# --- league-v4 apex : routing platform ---


async def test_apex_league_uses_platform_routing():
    with aioresponses() as m:
        m.get(APEX_URL, payload={"tier": "CHALLENGER", "queue": "RANKED_SOLO_5x5", "entries": []})
        async with RiotClient(api_key=API_KEY) as client:
            league = await client.get_apex_league("challenger", platform="kr")
    assert league["tier"] == "CHALLENGER"


async def test_apex_league_invalid_tier_raises():
    async with RiotClient(api_key=API_KEY) as client:
        with pytest.raises(ValueError, match="Tier apex invalide"):
            await client.get_apex_league("diamond", platform="euw1")


# --- league-v4 entries : pagination Emerald+ ---


async def test_league_entries_url_and_page():
    pattern = re.compile(
        r"^https://euw1\.api\.riotgames\.com/lol/league/v4/entries/RANKED_SOLO_5x5/EMERALD/I"
    )
    with aioresponses() as m:
        m.get(pattern, payload=[{"puuid": "e1"}, {"puuid": "e2"}])
        async with RiotClient(api_key=API_KEY) as client:
            entries = await client.get_league_entries("emerald", "I", platform="euw1", page=2)
    assert [e["puuid"] for e in entries] == ["e1", "e2"]
    # Le routing platform (euw1) et la page demandée ont bien été envoyés.
    sent_urls = [str(url) for (_method, url) in m.requests]
    assert any("page=2" in u and "euw1.api.riotgames.com" in u for u in sent_urls)


async def test_league_entries_invalid_tier_and_division_raise():
    async with RiotClient(api_key=API_KEY) as client:
        with pytest.raises(ValueError, match="Tier invalide"):
            await client.get_league_entries("master", "I", platform="euw1")
        with pytest.raises(ValueError, match="Division invalide"):
            await client.get_league_entries("EMERALD", "V", platform="euw1")


# --- match-v5 : queue 420 + routing regional ---


async def test_match_ids_passes_queue_420_and_regional_routing():
    pattern = re.compile(
        r"^https://europe\.api\.riotgames\.com/lol/match/v5/matches/by-puuid/PUUID/ids"
    )
    with aioresponses() as m:
        m.get(pattern, payload=["EUW1_1", "EUW1_2"])
        async with RiotClient(api_key=API_KEY) as client:
            ids = await client.get_match_ids_by_puuid(
                "PUUID", platform="euw1", start_time=1, end_time=2
            )
    assert ids == ["EUW1_1", "EUW1_2"]
    sent_urls = [str(url) for (_method, url) in m.requests]
    assert any(
        "queue=420" in u and "startTime=1" in u and "europe.api.riotgames.com" in u
        for u in sent_urls
    )


async def test_get_match_timeline_returns_payload():
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "riot" / "timeline_min.json"
    timeline = json.loads(fixture.read_text(encoding="utf-8"))
    url = "https://europe.api.riotgames.com/lol/match/v5/matches/EUW1_TEST0001/timeline"
    with aioresponses() as m:
        m.get(url, payload=timeline)
        async with RiotClient(api_key=API_KEY) as client:
            result = await client.get_match_timeline("EUW1_TEST0001", platform="euw1")
    assert result["metadata"]["matchId"] == "EUW1_TEST0001"
    assert result["info"]["frameInterval"] == 60000


# --- back-off (preuve du throttling/retry natif pulsefire) ---


async def test_backoff_retries_on_429_then_succeeds(no_sleep):
    with aioresponses() as m:
        m.get(APEX_URL, status=429, headers={"Retry-After": "0"})
        m.get(APEX_URL, payload={"tier": "CHALLENGER", "entries": []})
        async with RiotClient(api_key=API_KEY) as client:
            league = await client.get_apex_league("challenger", platform="kr")
    assert league["tier"] == "CHALLENGER"
    assert no_sleep.called  # un back-off a bien eu lieu entre les deux tentatives


async def test_backoff_retries_on_503_then_succeeds(no_sleep):
    with aioresponses() as m:
        m.get(APEX_URL, status=503)
        m.get(APEX_URL, payload={"tier": "CHALLENGER", "entries": []})
        async with RiotClient(api_key=API_KEY) as client:
            league = await client.get_apex_league("challenger", platform="kr")
    assert league["tier"] == "CHALLENGER"
    assert no_sleep.called


async def test_404_is_not_retried(no_sleep):
    """Une 4xx non-429 doit remonter immédiatement, sans retry ni back-off."""
    from aiohttp import ClientResponseError

    with aioresponses() as m:
        m.get(APEX_URL, status=404)
        async with RiotClient(api_key=API_KEY) as client:
            with pytest.raises(ClientResponseError):
                await client.get_apex_league("challenger", platform="kr")
    assert not no_sleep.called
