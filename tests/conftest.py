"""Fixtures partagées pytest.

Le mock HTTP du client Riot se fait via `aioresponses`, localement dans les
tests du module `collector`. Les tests d'intégration Postgres (storage) sont
sautés si `TEST_DATABASE_URL` n'est pas définie.
"""

import asyncio
import sys
from pathlib import Path

import pytest

# psycopg async ne supporte pas le ProactorEventLoop (défaut Windows) — même
# bascule que dans le CLI (trio_lab.collector.__main__).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root() -> Path:
    """Chemin absolu vers la racine du dépôt."""
    return PROJECT_ROOT
