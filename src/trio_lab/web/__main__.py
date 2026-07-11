"""Sert l'interface : `python -m trio_lab.web` (port via $PORT, défaut 8000)."""

from __future__ import annotations

import logging
import os

import uvicorn

from trio_lab import config
from trio_lab.web.app import create_app


def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
