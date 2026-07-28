"""Synchronise `champion_range_theoretical` depuis Data Dragon.

Usage : `python -m trio_lab.rangeref.sync`

À relancer seulement à la sortie d'un champion ou un rework de portée —
jamais à chaque cycle du service (même raisonnement que
`ccref.sync_theoretical`) : `synergy/compute.py` lit cette table (SELECT
rapide, pas d'appel réseau) pour matérialiser le score de portée
théorique de chaque duo/trio.
"""

from __future__ import annotations

import argparse
import logging

import psycopg

from trio_lab import config, db
from trio_lab.rangeref import score

logger = logging.getLogger(__name__)


def sync(*, dsn: str | None = None) -> int:
    """Recalcule `champion_range_theoretical` en entier. Retourne le nombre de lignes."""
    scores = score.fetch_champion_scores()
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        conn.execute("DELETE FROM champion_range_theoretical")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO champion_range_theoretical (champion_id, score) VALUES (%s, %s)",
                list(scores.items()),
            )
    logger.info("champion_range_theoretical synchronisée : %d champions", len(scores))
    return len(scores)


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.rangeref.sync", description=__doc__)
    parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    sync()


if __name__ == "__main__":
    main()
