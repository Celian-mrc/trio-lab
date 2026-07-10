"""Client API Riot centralisé, adapté du collector de macro-lab (Phase 1).

Encapsule `pulsefire` : throttling et back-off **natifs** (middlewares
`RiotAPIRateLimiter` + `http_error_middleware`), budgets séparés par région de
routage, réponses typées. C'est l'**unique** point d'entrée réseau vers l'API
Riot (règle CLAUDE.md #2) : aucun import `pulsefire` ni appel HTTP direct ne
doit exister hors de ce module.

Distinction de routing :
- routing **platform** (`euw1`, `kr`…) pour `league-v4` ;
- routing **regional** (`europe`, `asia`…) pour `match-v5`.
Les méthodes publiques prennent toujours un `platform` ; le mapping vers le
routing regional est fait en interne par `regional_for`.

Différence avec macro-lab : ajout de `get_league_entries` (endpoint paginé
`/lol/league/v4/entries/{queue}/{tier}/{division}`) pour la découverte des
joueurs Emerald+ — macro-lab ne seedait que les tiers apex.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from pulsefire.clients import RiotAPIClient
from pulsefire.middlewares import (
    http_error_middleware,
    json_response_middleware,
    rate_limiter_middleware,
)
from pulsefire.ratelimiters import RiotAPIRateLimiter

from trio_lab import config

logger = logging.getLogger(__name__)


def _parse_rate_pairs(header: str) -> dict[int, int]:
    """Parse un header Riot "value:window,value:window" → `{window_seconds: value}`."""
    pairs: dict[int, int] = {}
    for chunk in header.split(","):
        value, _, window = chunk.partition(":")
        try:
            pairs[int(window)] = int(value)
        except ValueError:
            continue
    return pairs


@dataclass
class RateLimitSnapshot:
    """Dernier état connu du rate-limit *app*, mis à jour par le middleware observateur.

    `remaining` = minimum, sur toutes les fenêtres, de `(limite - compteur courant)`.
    Reste `None` tant qu'aucune réponse portant les headers n'a été observée.
    """

    remaining: int | None = None
    limit_header: str | None = None
    count_header: str | None = None
    updated_at: str | None = None

    def update(self, limit_header: str, count_header: str) -> None:
        limits = _parse_rate_pairs(limit_header)
        counts = _parse_rate_pairs(count_header)
        remainings = [limits[w] - counts.get(w, 0) for w in limits]
        self.remaining = min(remainings) if remainings else None
        self.limit_header = limit_header
        self.count_header = count_header
        self.updated_at = datetime.now(UTC).isoformat()


def _rate_observer_middleware(snapshot: RateLimitSnapshot):
    """Middleware d'observabilité : lit les headers de rate-limit app de chaque réponse.

    Positionné sous `json_response_middleware` et au-dessus de
    `http_error_middleware` : il voit la réponse aiohttp finale (post-retry) et
    lit les headers sans consommer le body.
    """

    def constructor(next):
        async def middleware(invocation):
            response = await next(invocation)
            limit = response.headers.get("X-App-Rate-Limit")
            count = response.headers.get("X-App-Rate-Limit-Count")
            if limit and count:
                snapshot.update(limit, count)
            return response

        return middleware

    return constructor


# Routing platform → routing regional. Scope trio-lab : na1/euw1/kr, mais on
# garde le mapping complet (la plateforme est une donnée, pas une branche).
PLATFORM_TO_REGIONAL: dict[str, str] = {
    "euw1": "europe",
    "eun1": "europe",
    "tr1": "europe",
    "ru": "europe",
    "na1": "americas",
    "br1": "americas",
    "la1": "americas",
    "la2": "americas",
    "kr": "asia",
    "jp1": "asia",
    "oc1": "sea",
    "ph2": "sea",
    "sg2": "sea",
    "th2": "sea",
    "tw2": "sea",
    "vn2": "sea",
}

# Tiers apex de LEAGUE-V4 (chacun a sa méthode pulsefire dédiée).
APEX_TIERS: tuple[str, ...] = ("challenger", "grandmaster", "master")
# Tiers servis par l'endpoint paginé /entries/{queue}/{tier}/{division}.
ENTRY_TIERS: tuple[str, ...] = (
    "IRON",
    "BRONZE",
    "SILVER",
    "GOLD",
    "PLATINUM",
    "EMERALD",
    "DIAMOND",
)
DIVISIONS: tuple[str, ...] = ("I", "II", "III", "IV")

# SoloQ ranked.
DEFAULT_SOLOQ_QUEUE = 420
# Nombre de retries du back-off exponentiel sur 429/5xx (middleware pulsefire).
DEFAULT_MAX_RETRIES = 3


def regional_for(platform: str) -> str:
    """Retourne le routing regional (europe/americas/asia/sea) d'un routing platform.

    Lève `ValueError` si la plateforme est inconnue — pas de valeur par défaut
    silencieuse, qui masquerait une typo de région et taperait le mauvais bucket.
    """
    try:
        return PLATFORM_TO_REGIONAL[platform]
    except KeyError:
        raise ValueError(
            f"Plateforme inconnue : {platform!r}. Valeurs valides : {sorted(PLATFORM_TO_REGIONAL)}"
        ) from None


class RiotClient:
    """Wrapper async centralisé autour de pulsefire.

    Utilisation comme gestionnaire de contexte asynchrone :

        async with RiotClient() as client:
            league = await client.get_apex_league("master", platform="kr")

    La clé API est lue dans `trio_lab.config` (donc `.env`) si non fournie. Le
    throttling par région et le back-off 429/5xx sont assurés par les
    middlewares pulsefire — pas de token bucket maison.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        key = api_key if api_key is not None else config.RIOT_API_KEY
        if not key:
            raise RuntimeError(
                "RIOT_API_KEY absente : renseigne-la dans le fichier .env "
                "(voir .env.example). Jamais en dur ni en argument CLI."
            )
        # État de rate-limit observable, alimenté par le middleware observateur.
        self.rate = RateLimitSnapshot()
        self._client = RiotAPIClient(
            default_headers={"X-Riot-Token": key},
            middlewares=[
                json_response_middleware(),
                _rate_observer_middleware(self.rate),
                http_error_middleware(max_retries=max_retries),
                rate_limiter_middleware(RiotAPIRateLimiter()),
            ],
        )

    async def __aenter__(self) -> RiotClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    # --- league-v4 (routing platform) ---

    async def get_apex_league(
        self, tier: str, *, platform: str, queue: str = "RANKED_SOLO_5x5"
    ) -> dict[str, Any]:
        """Ladder apex (challenger / grandmaster / master) pour une plateforme."""
        tier_l = tier.lower()
        if tier_l not in APEX_TIERS:
            raise ValueError(f"Tier apex invalide : {tier!r}. Valeurs : {APEX_TIERS}")
        method = getattr(self._client, f"get_lol_league_v4_{tier_l}_league_by_queue")
        logger.debug("league-v4 %s %s (%s)", tier_l, queue, platform)
        return await method(region=platform, queue=queue)

    async def get_league_entries(
        self,
        tier: str,
        division: str,
        *,
        platform: str,
        queue: str = "RANKED_SOLO_5x5",
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Page d'entrées league-v4 d'un tier/division non-apex (page 1-indexée).

        L'API renvoie ~205 entrées par page et une liste vide au-delà de la
        dernière page — c'est le signal de fin de pagination pour `ladder`.
        """
        tier_u = tier.upper()
        if tier_u not in ENTRY_TIERS:
            raise ValueError(f"Tier invalide : {tier!r}. Valeurs : {ENTRY_TIERS}")
        if division not in DIVISIONS:
            raise ValueError(f"Division invalide : {division!r}. Valeurs : {DIVISIONS}")
        logger.debug("league-v4 entries %s %s p%d (%s)", tier_u, division, page, platform)
        return await self._client.get_lol_league_v4_entries_by_division(
            region=platform, queue=queue, tier=tier_u, division=division, queries={"page": page}
        )

    # --- match-v5 (routing regional) ---

    async def get_match_ids_by_puuid(
        self,
        puuid: str,
        *,
        platform: str,
        queue: int = DEFAULT_SOLOQ_QUEUE,
        start: int = 0,
        count: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[str]:
        """Liste les match IDs d'un PUUID (SoloQ par défaut, `queue=420`).

        `start_time`/`end_time` sont des timestamps **secondes** (convention Riot
        pour cet endpoint) permettant de borner sur le patch collecté.
        """
        region = regional_for(platform)
        queries: dict[str, Any] = {"start": start, "count": count, "queue": queue}
        if start_time is not None:
            queries["startTime"] = start_time
        if end_time is not None:
            queries["endTime"] = end_time
        logger.debug("match-v5 ids by-puuid %s (%s) queue=%s", puuid, region, queue)
        return await self._client.get_lol_match_v5_match_ids_by_puuid(
            region=region, puuid=puuid, queries=queries
        )

    async def get_match(self, match_id: str, *, platform: str) -> dict[str, Any]:
        """Détail d'un match (`match-v5`)."""
        region = regional_for(platform)
        logger.debug("match-v5 detail %s (%s)", match_id, region)
        return await self._client.get_lol_match_v5_match(region=region, id=match_id)

    async def get_match_timeline(self, match_id: str, *, platform: str) -> dict[str, Any]:
        """Timeline d'un match (`match-v5`) — frames par-minute + events."""
        region = regional_for(platform)
        logger.debug("match-v5 timeline %s (%s)", match_id, region)
        return await self._client.get_lol_match_v5_match_timeline(region=region, id=match_id)
