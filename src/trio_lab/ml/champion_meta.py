"""Métadonnées intrinsèques de champion (tags de rôle, profil dégâts/tankiness)
— Data Dragon `champion.json`, même source que `web/champions.py` mais des
champs différents (`tags`/`info`, pas nom/icône) : module séparé plutôt que
d'alourdir `web/champions.py` avec des champs que l'interface ne consomme pas.

Statique (propriété du champion, pas dérivée de résultats de matchs) : même
raisonnement que `champion_range_theoretical`/`champion_cc_theoretical` —
aucun risque de fuite, pas besoin de leave-one-patch-out.
"""

from __future__ import annotations

from dataclasses import dataclass

from trio_lab.web import champions as web_champions


@dataclass(frozen=True)
class ChampionMeta:
    id: int
    tags: tuple[str, ...]
    attack: int  # info.attack Data Dragon, 0-10 — proxy dégâts physiques
    defense: int  # info.defense, 0-10 — proxy tankiness
    magic: int  # info.magic, 0-10 — proxy dégâts magiques


def fetch_meta() -> dict[int, ChampionMeta]:
    """`{championId: ChampionMeta}` depuis la dernière version Data Dragon."""
    version = web_champions.get_json(web_champions.VERSIONS_URL)[0]
    data = web_champions.get_json(web_champions.CHAMPIONS_URL.format(version=version))["data"]
    return {
        int(c["key"]): ChampionMeta(
            id=int(c["key"]),
            tags=tuple(c["tags"]),
            attack=c["info"]["attack"],
            defense=c["info"]["defense"],
            magic=c["info"]["magic"],
        )
        for c in data.values()
    }
