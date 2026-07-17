"""Découverte des joueurs Emerald+ : league-v4 → lignes pour la table `players`.

Deux sources, appelées séparément (cadences différentes côté `collect.py`) :
- `discover_apex` : ladders challenger/grandmaster/master, non paginés, peu
  coûteux (3 appels/plateforme) — peut rester rafraîchi souvent ;
- `discover_entries` : endpoint paginé `/entries` pour **EMERALD et DIAMOND**
  (4 divisions chacun) — chaque division dépasse largement le plafond de
  pagination observé en pratique (`kr EMERALD IV` encore pleine à la page 200,
  soit 40 000+ joueurs, cf. session du 17/07/2026) : l'exhaustivité n'est pas
  visée, `max_pages` cherche juste un vivier nettement plus large que
  l'ancien plafond de 5 pages (~1 000 joueurs/division, jugé trop restrictif —
  il expliquait une bonne partie du plafonnement du volume quotidien collecté).
  Coûteux (`max_pages` × 8 divisions appels/plateforme) → cadence journalière,
  pas horaire.

La persistance (upsert `players`) relève de `storage`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from trio_lab.collector.client import APEX_TIERS, DIVISIONS, regional_for

logger = logging.getLogger(__name__)

# Tiers non-apex du scope trio-lab (Emerald+, cf. PROJECT.md § Collecte).
SUB_APEX_TIERS: tuple[str, ...] = ("EMERALD", "DIAMOND")
# Relevé le 17/07/2026 (5 -> 100) : l'ancien plafond ne captait qu'une
# fraction marginale de chaque division (vérifié en direct sur l'API,
# kr EMERALD IV encore pleine à la page 200) et plafonnait mécaniquement le
# volume quotidien de matchs collectés. 100 pages reste loin de
# l'exhaustivité mais élargit le vivier x20 pour un coût encore négligeable
# (~800 appels/plateforme, cf. discover_entries) une fois par jour.
DEFAULT_MAX_PAGES = 100


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


async def discover_apex(client: _LeagueClient, *, platform: str) -> list[PlayerRow]:
    """PUUIDs challenger/grandmaster/master d'une plateforme (premier tier vu conservé)."""
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

    return list(rows.values())


async def discover_entries(
    client: _LeagueClient,
    *,
    platform: str,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[PlayerRow]:
    """PUUIDs Emerald+Diamond d'une plateforme (paginé, plafonné à `max_pages`/division)."""
    routing = regional_for(platform)
    rows: dict[str, PlayerRow] = {}

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
