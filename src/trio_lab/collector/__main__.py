"""CLI du collector.

    python -m trio_lab.collector --patch 16.13 [--platforms na1,euw1,kr]
                                 [--target N] [--max-pages 5] [--max-attempts 3]
    python -m trio_lab.collector --service [--target 5000] [--keep 3]

`--service` (Railway 24/24) : cycles sans fin — patch courant auto (Data
Dragon), batch de `--target` matchs/plateforme, refresh agrégats + scores +
counters, purge de rétention quotidienne (`--keep` patchs conservés).
En mode batch, sans `--target`, boucle sans fin sur le patch donné. Le dossier
de données (archives timeline) vient du `.env` (DATA_DIR, ARCHIVE_TIMELINES).
Les migrations doivent avoir été appliquées (`python -m trio_lab.db`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from trio_lab import config, db, maintenance
from trio_lab.collector import collect, ladder, service, storage


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.collector", description=__doc__)
    parser.add_argument("--patch", help="patch API à collecter, ex. 16.13 (requis hors --service)")
    parser.add_argument(
        "--service",
        action="store_true",
        help="mode service 24/24 : patch auto + refresh scores + rétention",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=maintenance.DEFAULT_KEEP,
        help="(--service) patchs conservés par la purge de rétention",
    )
    parser.add_argument(
        "--platforms",
        default="na1,euw1,kr",
        help="plateformes séparées par des virgules (défaut : na1,euw1,kr)",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=None,
        help="matchs à télécharger par plateforme (défaut : sans fin)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=ladder.DEFAULT_MAX_PAGES,
        help="pages league/entries par division à la découverte",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=storage.DEFAULT_MAX_ATTEMPTS,
        help="tentatives avant échec permanent d'un match",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.use_selector_event_loop()
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    if args.service:
        service.run_service(
            platforms=platforms,
            batch_target=args.target or service.DEFAULT_BATCH_TARGET,
            keep_patches=args.keep,
        )
        return
    if not args.patch:
        parser.error("--patch est requis hors mode --service")
    asyncio.run(
        collect.run(
            platforms=platforms,
            patch=args.patch,
            target=args.target,
            max_pages=args.max_pages,
            max_attempts=args.max_attempts,
        )
    )


if __name__ == "__main__":
    main()
