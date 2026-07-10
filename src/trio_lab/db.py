"""Accès Postgres : migrations (sync) et connexion runtime (async).

Les migrations sont des fichiers SQL numérotés (`migrations/NNN_*.sql`),
auto-contenus : chaque fichier porte son propre BEGIN/COMMIT et insère sa ligne
dans `schema_migrations`. `apply_migrations` se contente d'exécuter, dans
l'ordre, ceux dont la version n'est pas encore appliquée.

Usage CLI : `python -m trio_lab.db` applique les migrations sur DATABASE_URL.
"""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

from trio_lab import config

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = config.PROJECT_ROOT / "migrations"


def require_dsn(dsn: str | None = None) -> str:
    """Retourne le DSN fourni ou celui du .env, erreur explicite sinon."""
    resolved = dsn if dsn is not None else config.DATABASE_URL
    if not resolved:
        raise RuntimeError(
            "DATABASE_URL absente : renseigne-la dans le fichier .env (voir .env.example)."
        )
    return resolved


def applied_versions(conn: psycopg.Connection) -> set[int]:
    """Versions déjà appliquées ; ensemble vide si la base est vierge."""
    exists = conn.execute("SELECT to_regclass('schema_migrations')").fetchone()
    if exists is None or exists[0] is None:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def apply_migrations(dsn: str | None = None, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Applique les migrations manquantes dans l'ordre. Retourne les versions appliquées.

    Connexion en autocommit : c'est le BEGIN/COMMIT de chaque fichier qui délimite
    la transaction, une migration qui échoue est donc rollbackée entièrement.
    """
    with psycopg.connect(require_dsn(dsn), autocommit=True) as conn:
        done = applied_versions(conn)
        applied: list[int] = []
        for path in sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql")):
            version = int(path.name[:3])
            if version in done:
                continue
            logger.info("migration %s", path.name)
            conn.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
            applied.append(version)
        return applied


async def connect(dsn: str | None = None) -> psycopg.AsyncConnection:
    """Connexion async pour le collector, en autocommit.

    Autocommit : chaque écriture du collector est atomique par instruction ; les
    écritures multi-tables (match + participants) ouvrent explicitement un bloc
    `async with conn.transaction()` dans `storage`.
    """
    return await psycopg.AsyncConnection.connect(require_dsn(dsn), autocommit=True)


if __name__ == "__main__":
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
    versions = apply_migrations()
    if versions:
        logger.info("migrations appliquées : %s", versions)
    else:
        logger.info("base déjà à jour")
