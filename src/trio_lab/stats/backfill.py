"""Backfill des stats trio pour les matchs ingérés avant la Phase 2.

Pour chaque match en base sans ligne `match_trio_stats` : timeline relue depuis
l'archive locale (JSON.gz), detail re-fetché via l'API (1 appel/match — le
detail n'est pas archivé), extraction puis insertion. Idempotent : relancer
ignore les matchs déjà backfillés ; un match sans archive est compté et sauté.

Usage : `python -m trio_lab.stats.backfill`
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from collections import Counter
from pathlib import Path

from trio_lab import config, db
from trio_lab.ccref.reliability import CC_TIME_RELIABILITY
from trio_lab.collector import storage
from trio_lab.collector.client import RiotClient
from trio_lab.collector.parsing import ParseError
from trio_lab.stats import extract

logger = logging.getLogger(__name__)


async def run(*, data_dir: Path | None = None, dsn: str | None = None) -> dict[str, int]:
    """Backfill de tous les matchs sans stats trio. Retourne les compteurs."""
    resolved_dir = data_dir if data_dir is not None else config.DATA_DIR
    counts: Counter[str] = Counter()
    conn = await db.connect(dsn)
    try:
        cur = await conn.execute(
            """
            SELECT m.match_id, m.platform, m.patch
            FROM matches m
            LEFT JOIN match_trio_stats t ON t.match_id = m.match_id
            WHERE t.match_id IS NULL
            ORDER BY m.collected_at
            """
        )
        todo = await cur.fetchall()
        if not todo:
            logger.info("aucun match à backfiller")
            return dict(counts)
        logger.info("%d matchs sans stats trio", len(todo))

        async with RiotClient() as client:
            for match_id, platform, patch in todo:
                path = storage.timeline_path(resolved_dir, platform, patch, match_id)
                if not path.exists():
                    logger.warning("%s : timeline absente de l'archive (%s)", match_id, path)
                    counts["missing_timeline"] += 1
                    continue
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    timeline = json.load(fh)
                try:
                    detail = await client.get_match(match_id, platform=platform)
                    trio_stats, objective_events = extract.extract_match(
                        detail, timeline, CC_TIME_RELIABILITY
                    )
                except ParseError as exc:
                    logger.warning("%s : inextractible (%s)", match_id, exc)
                    counts["unextractable"] += 1
                    continue
                await storage.insert_trio_stats(conn, trio_stats, objective_events)
                counts["backfilled"] += 1
    finally:
        await conn.close()
    logger.info("backfill terminé : %s", dict(counts))
    return dict(counts)


def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    db.use_selector_event_loop()
    asyncio.run(run())


if __name__ == "__main__":
    main()
