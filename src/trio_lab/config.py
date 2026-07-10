"""Chargement de la configuration depuis le `.env` à la racine du projet.

Source unique de vérité pour les secrets et constantes d'environnement (voir
`.env.example`). Aucune validation stricte ici — les modules qui consomment une
variable valident ce dont ils ont besoin au moment de l'utiliser.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Racine du projet = deux niveaux au-dessus de ce fichier (src/trio_lab/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Charge le .env s'il existe. Silencieux si absent (cas des tests / CI).
load_dotenv(PROJECT_ROOT / ".env")


def _get(key: str, default: str | None = None) -> str | None:
    """Lit une variable d'environnement, avec valeur par défaut optionnelle."""
    return os.getenv(key, default)


# --- Riot API ---
RIOT_API_KEY: str | None = _get("RIOT_API_KEY")

# --- Postgres ---
DATABASE_URL: str | None = _get("DATABASE_URL")

# --- Divers ---
LOG_LEVEL: str = _get("LOG_LEVEL", "INFO")
DATA_DIR: Path = Path(_get("DATA_DIR", str(PROJECT_ROOT / "data")))
