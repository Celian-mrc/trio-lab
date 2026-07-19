"""Fixtures partagées pytest.

Le mock HTTP du client Riot se fait via `aioresponses`, localement dans les
tests du module `collector`. Les tests d'intégration Postgres (storage) sont
sautés si `TEST_DATABASE_URL` n'est pas définie.
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from trio_lab import db

# psycopg async ne supporte pas le ProactorEventLoop (défaut Windows) — même
# bascule que dans les CLI (cf. db.use_selector_event_loop).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Base JETABLE des tests d'intégration Postgres (les tests tronquent les tables).
TEST_DSN = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def project_root() -> Path:
    """Chemin absolu vers la racine du dépôt."""
    return PROJECT_ROOT


@pytest.fixture
async def pg_conn():
    """Connexion au Postgres de test, migrations appliquées, tables tronquées.

    Saute le test si `TEST_DATABASE_URL` est absente. `matches` cascade sur
    participants, trio_stats et objective_events.
    """
    if not TEST_DSN:
        pytest.skip("TEST_DATABASE_URL absente (Postgres de test requis)")
    db.apply_migrations(TEST_DSN)
    conn = await db.connect(TEST_DSN)
    await conn.execute(
        "TRUNCATE players, matches, match_fetch_journal,"
        " agg_champion, agg_duo, agg_trio,"
        " agg_trio_duration, agg_duo_duration, agg_matchup,"
        " score_duo, score_trio, score_matchup,"
        " champion_cc_theoretical CASCADE"
    )
    try:
        yield conn
    finally:
        await conn.close()
