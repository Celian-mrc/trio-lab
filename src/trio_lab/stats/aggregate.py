"""Rafraîchissement des tables agrégées (agg_champion, agg_duo, agg_trio).

Idempotent par patch : DELETE puis INSERT…SELECT dans une seule transaction —
une lecture concurrente voit l'ancien ou le nouveau jeu complet, jamais un
état intermédiaire. Le grain inclut `platform` : la région se filtre à la
lecture, elle ne branche jamais le code (CLAUDE.md #6).

Usage : `python -m trio_lab.stats.aggregate --patch 16.13`
"""

from __future__ import annotations

import argparse
import logging

import psycopg

from trio_lab import config, db

logger = logging.getLogger(__name__)

_CHAMPION_SQL = """
    INSERT INTO agg_champion (patch, platform, role, champion_id, games, wins)
    SELECT m.patch, m.platform, p.role, p.champion_id,
           count(*), count(*) FILTER (WHERE p.win)
    FROM match_participants p
    JOIN matches m USING (match_id)
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, p.role, p.champion_id
"""

_DUO_SQL = """
    INSERT INTO agg_duo (patch, platform, roles, champ_a, champ_b, games, wins)
    SELECT m.patch, m.platform, d.roles, d.champ_a, d.champ_b,
           count(*), count(*) FILTER (WHERE t.win)
    FROM match_trio_stats t
    JOIN matches m USING (match_id)
    CROSS JOIN LATERAL (VALUES
        ('jgl_mid', t.jgl_champion, t.mid_champion),
        ('jgl_sup', t.jgl_champion, t.sup_champion),
        ('mid_sup', t.mid_champion, t.sup_champion)
    ) AS d(roles, champ_a, champ_b)
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, d.roles, d.champ_a, d.champ_b
"""

_TRIO_SQL = """
    INSERT INTO agg_trio (patch, platform, jgl_champion, mid_champion, sup_champion,
                          games, wins)
    SELECT m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion,
           count(*), count(*) FILTER (WHERE t.win)
    FROM match_trio_stats t
    JOIN matches m USING (match_id)
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion
"""

_TABLES_SQL = {"agg_champion": _CHAMPION_SQL, "agg_duo": _DUO_SQL, "agg_trio": _TRIO_SQL}


def refresh(patch: str, *, dsn: str | None = None) -> dict[str, int]:
    """Recalcule les agrégats d'un patch. Retourne le nombre de lignes par table."""
    counts: dict[str, int] = {}
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        for table, sql in _TABLES_SQL.items():
            conn.execute(
                psycopg.sql.SQL("DELETE FROM {} WHERE patch = %(patch)s").format(
                    psycopg.sql.Identifier(table)
                ),
                {"patch": patch},
            )
            cur = conn.execute(sql, {"patch": patch})
            counts[table] = cur.rowcount
    logger.info("agrégats %s rafraîchis : %s", patch, counts)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.stats.aggregate", description=__doc__)
    parser.add_argument("--patch", required=True, help="patch API à agréger, ex. 16.13")
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    refresh(args.patch)


if __name__ == "__main__":
    main()
