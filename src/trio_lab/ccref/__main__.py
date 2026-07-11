"""CLI one-shot : génère le brouillon cc_reference.draft.csv depuis le wiki LoL.

    python -m trio_lab.ccref             # brouillon (relecture humaine ensuite)
    python -m trio_lab.ccref --freeze    # brouillon + gel en cc_reference.csv

Ne geler qu'après relecture humaine complète (règle 4). À relancer lors d'un
rework de champion (puis relecture du diff et re-gel).
"""

from __future__ import annotations

import argparse
import logging

from trio_lab import config
from trio_lab.ccref import build


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.ccref", description=__doc__)
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="gèle aussi le résultat en cc_reference.csv (relecture humaine préalable requise)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    rows = build.build_rows()
    build.write_draft(rows)
    if args.freeze:
        build.write_frozen(rows)


if __name__ == "__main__":
    main()
