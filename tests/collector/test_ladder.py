"""Tests de la découverte Emerald+ : apex + pagination entries, dédup, plafond."""

from __future__ import annotations

from trio_lab import config
from trio_lab.collector import ladder


class _FakeClient:
    """Faux client league-v4 : apex fixes + pages programmables par (tier, division)."""

    def __init__(self, pages: dict[tuple[str, str], list[list[dict]]] | None = None):
        self.pages = pages or {}
        self.entry_calls: list[tuple[str, str, int]] = []

    async def get_apex_league(self, tier, *, platform):
        entries = {
            "challenger": [{"puuid": "chall-1"}],
            "grandmaster": [{"puuid": "gm-1"}, {"puuid": "chall-1"}],  # doublon cross-tier
            "master": [{"puuid": "master-1"}, {}],  # entrée sans puuid ignorée
        }[tier]
        return {"tier": tier.upper(), "entries": entries}

    async def get_league_entries(self, tier, division, *, platform, page):
        self.entry_calls.append((tier, division, page))
        per_division = self.pages.get((tier, division), [])
        return per_division[page - 1] if page <= len(per_division) else []


async def test_discover_apex_dedups_across_tiers():
    client = _FakeClient()
    rows = await ladder.discover_apex(client, platform="euw1")
    by_puuid = {r.puuid: r for r in rows}

    assert set(by_puuid) == {"chall-1", "gm-1", "master-1"}
    # Premier tier vu conservé : chall-1 reste CHALLENGER malgré sa présence en grandmaster.
    assert by_puuid["chall-1"].tier == "CHALLENGER"
    assert by_puuid["chall-1"].division is None
    # routing dérivé de la plateforme (budget de rate-limit).
    assert all(r.platform == "euw1" and r.routing == "europe" for r in rows)


async def test_discover_entries_returns_all_divisions():
    client = _FakeClient(
        pages={
            ("EMERALD", "I"): [[{"puuid": "em-1"}]],
            ("DIAMOND", "IV"): [[{"puuid": "dia-1"}]],
        }
    )
    rows = await ladder.discover_entries(client, platform="euw1", max_pages=3)
    by_puuid = {r.puuid: r for r in rows}

    assert set(by_puuid) == {"em-1", "dia-1"}
    assert by_puuid["em-1"].tier == "EMERALD"
    assert by_puuid["em-1"].division == "I"
    assert all(r.platform == "euw1" and r.routing == "europe" for r in rows)


async def test_pagination_stops_on_empty_page():
    client = _FakeClient(
        pages={("EMERALD", "I"): [[{"puuid": "a"}], [{"puuid": "b"}]]}  # 2 pages puis vide
    )
    rows = await ladder.discover_entries(client, platform="kr", max_pages=10)
    # Page 3 (vide) demandée pour EMERALD I, puis arrêt — pas de page 4.
    emerald_i_pages = [p for (t, d, p) in client.entry_calls if (t, d) == ("EMERALD", "I")]
    assert emerald_i_pages == [1, 2, 3]
    assert {r.puuid for r in rows if r.tier == "EMERALD"} == {"a", "b"}


async def test_pagination_is_capped_at_max_pages():
    endless = [[{"puuid": f"em-{i}"}] for i in range(100)]  # jamais de page vide
    client = _FakeClient(pages={("EMERALD", "II"): endless})
    await ladder.discover_entries(client, platform="na1", max_pages=2)
    emerald_ii_pages = [p for (t, d, p) in client.entry_calls if (t, d) == ("EMERALD", "II")]
    assert emerald_ii_pages == [1, 2]


async def test_scope_is_emerald_plus():
    """La découverte paginée n'interroge que EMERALD et DIAMOND, jamais en dessous."""
    client = _FakeClient()
    await ladder.discover_entries(client, platform="euw1", max_pages=1)
    tiers_called = {t for (t, _d, _p) in client.entry_calls}
    assert tiers_called == set(ladder.SUB_APEX_TIERS)


def test_default_scope_matches_project():
    assert ladder.SUB_APEX_TIERS == ("EMERALD", "DIAMOND")
    assert config  # évite l'import inutilisé si le scope change
