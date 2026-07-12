"""Backfill des colonnes `cc_time_s`/`immobilizations` de `match_participants`.

Répare le trou historique du 2026-07-10 : ces colonnes (migration 005)
restent NULL pour les matchs collectés avant leur ajout. Re-télécharge le
detail des matchs concernés via l'API Riot (1 appel/match, aucune timeline
nécessaire) et met à jour les 10 participants. Idempotent (relancer ignore
les matchs déjà complétés) — mais **usage ponctuel** : éviter de lancer
pendant que le collector tourne sur la même clé API (contention 429, cf.
memory railway-deployment), ou accepter un ralentissement temporaire.

Enchaîner ensuite `ccref.reliability.backfill_trio_cc` (recalcule
match_trio_stats.cc_time_s depuis les participants maintenant complétés)
puis `stats.aggregate` + `synergy.compute` pour propager aux tables
matérialisées.

Usage : `python -m trio_lab.stats.backfill_participant_cc [--jgl-champion 56] [--limit N]`
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter

from trio_lab import config, db
from trio_lab.collector.client import RiotClient
from trio_lab.collector.parsing import ParseError, participant_rows

logger = logging.getLogger(__name__)


async def run(
    *, jgl_champion: int | None = None, limit: int | None = None, dsn: str | None = None
) -> dict[str, int]:
    """Backfill des participants sans CC empirique. Retourne les compteurs."""
    counts: Counter[str] = Counter()
    conn = await db.connect(dsn)
    try:
        query = """
            SELECT DISTINCT t.match_id, m.platform
            FROM match_trio_stats t
            JOIN matches m ON m.match_id = t.match_id
            JOIN match_participants p ON p.match_id = t.match_id
            WHERE p.cc_time_s IS NULL
              AND (%(jgl)s::int IS NULL OR t.jgl_champion = %(jgl)s::int)
            ORDER BY t.match_id
        """
        params: dict[str, object] = {"jgl": jgl_champion}
        if limit is not None:
            query += " LIMIT %(limit)s"
            params["limit"] = limit
        cur = await conn.execute(query, params)
        todo = await cur.fetchall()
        if not todo:
            logger.info("aucun match à backfiller")
            return dict(counts)
        logger.info("%d matchs sans détail participant CC", len(todo))

        async with RiotClient() as client:
            for match_id, platform in todo:
                try:
                    detail = await client.get_match(match_id, platform=platform)
                    rows = participant_rows(detail)
                except ParseError as exc:
                    logger.warning("%s : inextractible (%s)", match_id, exc)
                    counts["unextractable"] += 1
                    continue
                except Exception as exc:  # noqa: BLE001 — on journalise et on continue
                    logger.warning("%s : échec re-fetch (%s)", match_id, exc)
                    counts["errors"] += 1
                    continue
                async with conn.cursor() as pcur:
                    await pcur.executemany(
                        """
                        UPDATE match_participants
                        SET cc_time_s = %(cc_time_s)s, immobilizations = %(immobilizations)s
                        WHERE match_id = %(match_id)s AND team_id = %(team_id)s
                          AND role = %(role)s
                        """,
                        rows,
                    )
                counts["updated"] += 1
                if counts["updated"] % 100 == 0:
                    logger.info("... %d matchs mis à jour", counts["updated"])
    finally:
        await conn.close()
    logger.info("backfill participant CC terminé : %s", dict(counts))
    return dict(counts)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="trio_lab.stats.backfill_participant_cc", description=__doc__
    )
    parser.add_argument("--jgl-champion", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    db.use_selector_event_loop()
    asyncio.run(run(jgl_champion=args.jgl_champion, limit=args.limit))


if __name__ == "__main__":
    main()
