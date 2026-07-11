"""Index champion_id → nom/icône via Data Dragon (CDN statique Riot, sans clé).

Complète `ccref.champions` (qui ne mappe que nom → id pour la validation CC) :
l'interface a besoin du sens inverse et des icônes. Un fetch au premier usage,
gardé en mémoire pour la vie du process — les champions ne changent qu'au
patch, un redémarrage suffit.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPIONS_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
ICON_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{image}"
USER_AGENT = "trio-lab/0.1 (index champion pour l'interface)"


@dataclass(frozen=True)
class Champion:
    id: int
    name: str
    icon_url: str


def _get_json(url: str) -> dict | list:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_index() -> dict[int, Champion]:
    """`{championId: Champion}` depuis la dernière version Data Dragon."""
    version = _get_json(VERSIONS_URL)[0]
    data = _get_json(CHAMPIONS_URL.format(version=version))["data"]
    index = {
        int(champ["key"]): Champion(
            id=int(champ["key"]),
            name=champ["name"],
            icon_url=ICON_URL.format(version=version, image=champ["image"]["full"]),
        )
        for champ in data.values()
    }
    logger.info("Data Dragon %s : %d champions indexés", version, len(index))
    return index


def name_lookup(index: dict[int, Champion]) -> dict[str, int]:
    """`{nom normalisé (minuscules): championId}` pour la recherche."""
    return {champ.name.casefold(): champ.id for champ in index.values()}
