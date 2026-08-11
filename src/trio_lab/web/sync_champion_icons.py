"""Télécharge les icônes de champion manquantes depuis Data Dragon vers
`static/champions/` (servies localement par l'interface, cf. `champions.py`).

Usage : `python -m trio_lab.web.sync_champion_icons`

Incrémental (retour utilisateur 2026-08-11 : "elles changent très rarement
faudra juste ajouter les nouveaux champions") : ne télécharge que les
fichiers ABSENTS du dossier local, jamais un icône déjà présent — un
reskin visuel du même champion (même nom de fichier) ne serait donc pas
repris automatiquement, à re-synchroniser à la main (suppression du
fichier local) si ça arrive un jour. À relancer seulement à la sortie d'un
nouveau champion, jamais à chaque cycle du service (même raisonnement que
`ccref.sync_theoretical`/`rangeref.sync`).
"""

from __future__ import annotations

import argparse
import logging
import urllib.request

from trio_lab import config
from trio_lab.web.champions import CHAMPIONS_URL, ICON_URL, USER_AGENT, VERSIONS_URL, get_json

logger = logging.getLogger(__name__)

ICON_DIR = config.PROJECT_ROOT / "src" / "trio_lab" / "web" / "static" / "champions"


def sync() -> list[str]:
    """Télécharge les icônes manquantes. Retourne les fichiers ajoutés."""
    version = get_json(VERSIONS_URL)[0]
    data = get_json(CHAMPIONS_URL.format(version=version))["data"]
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    added: list[str] = []
    for champ in data.values():
        image = champ["image"]["full"]
        dest = ICON_DIR / image
        if dest.exists():
            continue
        request = urllib.request.Request(
            ICON_URL.format(version=version, image=image), headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            dest.write_bytes(response.read())
        added.append(image)
    logger.info(
        "icônes champion Data Dragon %s : %d ajoutées, %d déjà présentes",
        version,
        len(added),
        len(data) - len(added),
    )
    return added


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.web.sync_champion_icons", description=__doc__)
    parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    sync()


if __name__ == "__main__":
    main()
