"""CLI des scores de synergie.

    python -m trio_lab.synergy --patches 16.13
    python -m trio_lab.synergy --patches 16.14,16.13,16.12 --k 200

`--patches` : fenêtre du patch le plus récent au plus ancien (1 à 3), poids
1.0/0.6/0.35. Prérequis : `python -m trio_lab.stats.aggregate --patch X` pour
chaque patch de la fenêtre.
"""

from __future__ import annotations

import argparse
import logging

from trio_lab import config
from trio_lab.synergy import compute, scores, windows


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.synergy", description=__doc__)
    parser.add_argument(
        "--patches",
        required=True,
        help="fenêtre, du plus récent au plus ancien, ex. 16.14,16.13",
    )
    parser.add_argument(
        "--k",
        type=float,
        default=scores.DEFAULT_PRIOR_K,
        help="force du prior duo→trio en games-équivalents",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    window = windows.make_window([p.strip() for p in args.patches.split(",") if p.strip()])
    compute.refresh(window, k=args.k)


if __name__ == "__main__":
    main()
