"""Découverte des joueurs Emerald+ : league-v4 → lignes pour la table `players`.

Deux sources, fusionnées et dédupliquées par PUUID :
- les ladders **apex** (challenger/grandmaster/master), non paginés ;
- l'endpoint paginé `/entries` pour **EMERALD et DIAMOND** (4 divisions chacun).

La pagination est plafonnée (`max_pages` par division) : on cherche un vivier de
seeds suffisant (~8 divisions × 5 pages × ~205 entrées ≈ 8 000 joueurs par
plateforme), pas l'exhaustivité — chaque joueur ouvre jusqu'à 100 match_ids par
patch au fan-out, largement de quoi saturer le budget API. La persistance
(upsert `players`) relève de `storage`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from trio_lab.collector.client import APEX_TIERS, DIVISIONS, regional_for

logger = logging.getLogger(__name__)

# Tiers non-apex du scope trio-lab (Emerald+, cf. PROJECT.md § Collecte).
SUB_APEX_TIERS: tuple[str, ...] = ("EMERALD", "DIAMOND")
DEFAULT_MAX_PAGES = 5


class _LeagueClient(Protocol):
    """Sous-ensemble de RiotClient utilisé ici (facilite les fakes de test)."""

    async def get_apex_league(self, tier: str, *, platform: str) -> dict: ...

    async def get_league_entries(
        self, tier: str, division: str, *, platform: str, page: int
    ) -> list[dict]: ...


@dataclass(frozen=True)
class PlayerRow:
    """Ligne candidate pour la table `players` (tier = snapshot à la découverte)."""

    puuid: str
    platform: str
    routing: str
    tier: str
    division: str | None


async def discover_players(
    client: _LeagueClient,
    *,
    platform: str,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[PlayerRow]:
    """PUUIDs Emerald+ d'une plateforme, dédupliqués (premier tier vu conservé)."""
    routing = regional_for(platform)
    rows: dict[str, PlayerRow] = {}

    for tier in APEX_TIERS:
        league = await client.get_apex_league(tier, platform=platform)
        entries = league.get("entries", [])
        for entry in entries:
            puuid = entry.get("puuid")
            if puuid and puuid not in rows:
                rows[puuid] = PlayerRow(puuid, platform, routing, tier.upper(), None)
        logger.info("league-v4 %s %s : %d entrées", platform, tier, len(entries))

    for tier in SUB_APEX_TIERS:
        for division in DIVISIONS:
            for page in range(1, max_pages + 1):
                entries = await client.get_league_entries(
                    tier, division, platform=platform, page=page
                )
                if not entries:
                    break  # page vide = fin de la division
                for entry in entries:
                    puuid = entry.get("puuid")
                    if puuid and puuid not in rows:
                        rows[puuid] = PlayerRow(puuid, platform, routing, tier, division)
            logger.info(
                "league-v4 %s %s %s : cumul %d joueurs", platform, tier, division, len(rows)
            )

    return list(rows.values())
