"""Correspondance nom de champion (wiki) → championId (Riot), via Data Dragon.

Data Dragon est le CDN de données STATIQUES officiel de Riot (pas des données
de match) : un fetch léger à la demande, pas de clé API requise. Le nom
d'affichage en_US de Data Dragon coïncide avec les noms du wiki, aux alias
près (`NAME_ALIASES`).
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPIONS_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
USER_AGENT = "trio-lab/0.1 (mapping statique champion name vers id)"

# Nom wiki → nom d'affichage Data Dragon quand ils divergent.
NAME_ALIASES: dict[str, str] = {"Nunu": "Nunu & Willump"}


def _get_json(url: str) -> dict | list:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_name_to_id() -> dict[str, int]:
    """`{nom d'affichage: championId}` depuis la dernière version Data Dragon."""
    version = _get_json(VERSIONS_URL)[0]
    data = _get_json(CHAMPIONS_URL.format(version=version))["data"]
    mapping = {champ["name"]: int(champ["key"]) for champ in data.values()}
    logger.info("Data Dragon %s : %d champions", version, len(mapping))
    return mapping


def resolve(name: str, name_to_id: dict[str, int]) -> int | None:
    """championId d'un nom wiki (alias appliqués), None si introuvable."""
    return name_to_id.get(NAME_ALIASES.get(name, name))
