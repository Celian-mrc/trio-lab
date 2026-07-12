"""Synchronise `champion_cc_reliability` (base) — ratio CC/immobilisation par champion.

Contrairement à `sync_theoretical` (fichier gelé, figé une fois pour toutes),
ce calcul dépend de la donnée de match courante (patch, méta) : à relancer
périodiquement, pas figé.

Usage :
    python -m trio_lab.ccref.sync_reliability
    python -m trio_lab.ccref.sync_reliability --backfill [--patch 16.13]
        (recalcule aussi match_trio_stats.cc_time_s pour les matchs encore
        couverts par match_participants — cf. reliability.backfill_trio_cc)
"""

from __future__ import annotations

import argparse
import logging

import psycopg

from trio_lab import config, db
from trio_lab.ccref import reliability

logger = logging.getLogger(__name__)


def sync(
    *, dsn: str | None = None, min_games: int = reliability.MIN_GAMES, backfill: bool = False
) -> int:
    """Recalcule `champion_cc_reliability` en entier. Retourne le nombre de champions."""
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        computed = reliability.compute_reliability(conn, min_games=min_games)
        conn.execute("DELETE FROM champion_cc_reliability")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO champion_cc_reliability"
                " (champion_id, reliability, sec_per_immo, games) VALUES (%s, %s, %s, %s)",
                [
                    (champ_id, v["reliability"], v["sec_per_immo"], v["games"])
                    for champ_id, v in computed.items()
                ],
            )
        if backfill:
            reliability.backfill_trio_cc(conn)
    logger.info("champion_cc_reliability synchronisée : %d champions", len(computed))
    return len(computed)


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.ccref.sync_reliability", description=__doc__)
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="recalcule aussi match_trio_stats.cc_time_s (matchs encore couverts par"
        " match_participants) — puis relancer stats.aggregate et synergy.compute",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    sync(backfill=args.backfill)


if __name__ == "__main__":
    main()
