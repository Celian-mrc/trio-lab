"""CLI one-shot : génère le brouillon cc_reference.draft.csv depuis le wiki LoL.

    python -m trio_lab.ccref

À relancer uniquement lors d'un rework de champion (puis relecture du diff).
"""

from __future__ import annotations

import logging

from trio_lab import config
from trio_lab.ccref import build


def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    rows = build.build_rows()
    build.write_draft(rows)


if __name__ == "__main__":
    main()
