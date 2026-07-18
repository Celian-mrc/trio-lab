"""Découverte des joueurs Emerald+ : league-v4 → lignes pour la table `players`.

Trois sources, appelées séparément (cadences différentes côté `collect.py`) :
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
- `player_row_from_entries` : construit une ligne à partir d'une réponse
  league-v4-par-PUUID — utilisé par la récolte des participants d'un match
  déjà téléchargé (`collect.py`, session du 18/07/2026), gratuite en appels
  API pour les PUUIDs (déjà dans le détail du match), un seul appel de
  vérification de rang par candidat inconnu. Uniquement des endpoints
  officiels Riot (CLAUDE.md règle 4) — pas de liste de match IDs tierce.

La persistance (upsert `players`) relève de `storage`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from trio_lab.collector.client import APEX_TIERS, DIVISIONS, regional_for

logger = logging.getLogger(__name__)

# Tiers non-apex du scope trio-lab (Emerald+, cf. PROJECT.md § Collecte).
SUB_APEX_TIERS: tuple[str, ...] = ("EMERALD", "DIAMOND")
# Tiers éligibles au scope Emerald+ (apex en majuscules + sub-apex), utilisé
# par `player_row_from_entries` pour filtrer une réponse league-v4-par-PUUID.
_ELIGIBLE_TIERS: frozenset[str] = frozenset(t.upper() for t in APEX_TIERS) | frozenset(
    SUB_APEX_TIERS
)
_SOLOQ_QUEUE_TYPE = "RANKED_SOLO_5x5"
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

    async def get_league_entries_by_puuid(self, puuid: str, *, platform: str) -> list[dict]: ...


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


def player_row_from_entries(
    entries: list[dict[str, Any]], *, puuid: str, platform: str
) -> PlayerRow | None:
    """Ligne candidate depuis une réponse league-v4-par-PUUID, `None` si hors scope.

    Filtre sur l'entrée RANKED_SOLO_5x5 uniquement (un compte a aussi des
    entrées flex/autres queues) et sur le tier Emerald+ — un participant
    d'un match ramené par la récolte peut très bien être en dessous
    (matchmaking, ancien compte) et ne doit pas entrer dans le pool.
    """
    for entry in entries:
        if entry.get("queueType") != _SOLOQ_QUEUE_TYPE:
            continue
        tier = entry.get("tier", "").upper()
        if tier not in _ELIGIBLE_TIERS:
            return None
        division = entry.get("rank") if tier in SUB_APEX_TIERS else None
        return PlayerRow(puuid, platform, regional_for(platform), tier, division)
    return None
