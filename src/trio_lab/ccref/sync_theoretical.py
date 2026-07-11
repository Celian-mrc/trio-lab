"""Synchronise `champion_cc_theoretical` (base) depuis le fichier gelé.

Usage : `python -m trio_lab.ccref.sync_theoretical`

À relancer seulement quand `data/external/cc_reference.csv` change (rework
relu et regelé) ou qu'un nouveau champion sort — jamais à chaque cycle du
service : `synergy/compute.py` lit cette table (SELECT rapide, pas d'appel
réseau Data Dragon) pour matérialiser les scores CC théorique/mélangé.
"""

from __future__ import annotations

import argparse
import logging

import psycopg

from trio_lab import config, db
from trio_lab.ccref import champions, score

logger = logging.getLogger(__name__)


def sync(*, dsn: str | None = None) -> int:
    """Recalcule `champion_cc_theoretical` en entier. Retourne le nombre de lignes."""
    name_to_id = champions.fetch_name_to_id()
    rows = [
        (champ_id, value)
        for name, value in score.champion_scores().items()
        if (champ_id := champions.resolve(name, name_to_id)) is not None
    ]
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        conn.execute("DELETE FROM champion_cc_theoretical")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO champion_cc_theoretical (champion_id, score) VALUES (%s, %s)",
                rows,
            )
    logger.info("champion_cc_theoretical synchronisée : %d champions", len(rows))
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.ccref.sync_theoretical", description=__doc__)
    parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    sync()


if __name__ == "__main__":
    main()
