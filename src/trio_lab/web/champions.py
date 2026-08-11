"""Index champion_id → nom/icône via Data Dragon (CDN statique Riot, sans clé).

Complète `ccref.champions` (qui ne mappe que nom → id pour la validation CC) :
l'interface a besoin du sens inverse et des icônes. Un fetch au premier usage,
gardé en mémoire pour la vie du process — les champions ne changent qu'au
patch, un redémarrage suffit.

Icônes servies LOCALEMENT (`/static/champions/`, synchronisées via
`python -m trio_lab.web.sync_champion_icons`), pas hotlinkées vers le CDN
Riot (retour utilisateur 2026-08-11 : une page de tier list déclenchait
~90 requêtes tierces vers `ddragon.leagueoflegends.com`, sans
`Cache-Control` côté Riot — cause réelle d'une lenteur perçue à la
navigation, la page elle-même répondant vite). `ICON_URL` (source Data
Dragon) reste utilisée par le script de synchronisation, pas par
`fetch_index`.
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
LOCAL_ICON_PATH = "/static/champions/{image}"
USER_AGENT = "trio-lab/0.1 (index champion pour l'interface)"


@dataclass(frozen=True)
class Champion:
    id: int
    name: str
    icon_url: str


def get_json(url: str) -> dict | list:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_index() -> dict[int, Champion]:
    """`{championId: Champion}` depuis la dernière version Data Dragon."""
    version = get_json(VERSIONS_URL)[0]
    data = get_json(CHAMPIONS_URL.format(version=version))["data"]
    index = {
        int(champ["key"]): Champion(
            id=int(champ["key"]),
            name=champ["name"],
            icon_url=LOCAL_ICON_PATH.format(image=champ["image"]["full"]),
        )
        for champ in data.values()
    }
    logger.info("Data Dragon %s : %d champions indexés", version, len(index))
    return index


def name_lookup(index: dict[int, Champion]) -> dict[str, int]:
    """`{nom normalisé (minuscules): championId}` pour la recherche."""
    return {champ.name.casefold(): champ.id for champ in index.values()}
