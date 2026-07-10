"""CLI du collector.

    python -m trio_lab.collector --patch 16.13 [--platforms na1,euw1,kr]
                                 [--target N] [--max-pages 5] [--max-attempts 3]

Sans `--target`, boucle sans fin (mode service 24/24). Le dossier de données
(archives timeline) vient du `.env` (DATA_DIR). Les migrations doivent avoir
été appliquées (`python -m trio_lab.db`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from trio_lab import config, db
from trio_lab.collector import collect, ladder, storage


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.collector", description=__doc__)
    parser.add_argument("--patch", required=True, help="patch API à collecter, ex. 16.13")
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
    asyncio.run(
        collect.run(
            platforms=[p.strip() for p in args.platforms.split(",") if p.strip()],
            patch=args.patch,
            target=args.target,
            max_pages=args.max_pages,
            max_attempts=args.max_attempts,
        )
    )


if __name__ == "__main__":
    main()
