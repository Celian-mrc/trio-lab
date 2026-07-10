"""Fixtures partagées pytest.

Le mock HTTP du client Riot se fait via `aioresponses`, localement dans les
tests du module `collector`. Les tests d'intégration Postgres (storage) sont
sautés si `TEST_DATABASE_URL` n'est pas définie.
"""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root() -> Path:
    """Chemin absolu vers la racine du dépôt."""
    return PROJECT_ROOT
