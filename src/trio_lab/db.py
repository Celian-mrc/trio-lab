"""Accès Postgres : migrations (sync) et connexion runtime (async).

Les migrations sont des fichiers SQL numérotés (`migrations/NNN_*.sql`),
auto-contenus : chaque fichier porte son propre BEGIN/COMMIT et insère sa ligne
dans `schema_migrations`. `apply_migrations` se contente d'exécuter, dans
l'ordre, ceux dont la version n'est pas encore appliquée.

Usage CLI : `python -m trio_lab.db` applique les migrations sur DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import psycopg

from trio_lab import config

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = config.PROJECT_ROOT / "migrations"


def use_selector_event_loop() -> None:
    """Bascule sur SelectorEventLoop sous Windows, avant tout `asyncio.run`.

    psycopg async ne supporte pas le ProactorEventLoop (défaut Windows) ;
    no-op sur Linux (Railway). À appeler par chaque point d'entrée async.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


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


def _strip_line_comments(sql: str) -> str:
    """Retire les `-- commentaire` fin de ligne, guillemets simples respectés.

    Trouvé en CI le 2026-09-02 (migration 001, jamais rejouée sur une base
    vierge depuis son application initiale en prod, donc jamais testée) :
    un commentaire français utilisait `;` comme ponctuation de phrase
    ("...calculées à l'ingestion ; le détail ordonné..."), pas seulement
    comme séparateur SQL. Le split naïf sur `;` coupait le commentaire en
    deux, laissant sa seconde moitié sans son préfixe `--` — Postgres tente
    alors de la parser comme du SQL. 13 autres occurrences trouvées dans les
    migrations existantes au même audit."""
    lines = []
    for line in sql.splitlines():
        in_string = False
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                in_string = not in_string
            elif not in_string and line[i : i + 2] == "--":
                cut = i
                break
            i += 1
        lines.append(line[:cut])
    return "\n".join(lines)


def _split_statements(sql: str) -> list[str]:
    """Découpe un fichier de migration en instructions séparées (sur `;`).

    Nécessaire depuis la migration 038 (`CREATE INDEX CONCURRENTLY`) : le
    protocole simple query de Postgres regroupe implicitement PLUSIEURS
    instructions envoyées d'un coup dans une seule transaction, même sur une
    connexion en autocommit côté client — or `CONCURRENTLY` refuse
    justement de tourner dans un bloc de transaction. Envoyer chaque
    instruction séparément règle ça, et ne change rien pour les migrations
    BEGIN/COMMIT existantes (l'état de transaction Postgres est au niveau
    de la session, pas du message réseau — `BEGIN` puis `COMMIT` dans deux
    appels `execute()` séparés délimitent la même transaction que dans un
    seul). Commentaires retirés avant le split (cf. `_strip_line_comments`)
    pour qu'un `;` dans un commentaire ne coupe pas une instruction en deux.
    Split naïf sûr sinon : aucune migration n'utilise de corps
    dollar-quoté (`$$`) ni de point-virgule dans un littéral (vérifié)."""
    sql = _strip_line_comments(sql)
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


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
            for statement in _split_statements(path.read_text(encoding="utf-8")):
                conn.execute(statement)  # type: ignore[arg-type]
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
