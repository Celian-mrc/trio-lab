"""Rafraîchissement des tables agrégées (agg_champion, agg_duo, agg_trio,
agg_trio_vs_champion).

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

# Sommes de stats partagées trio/duo : les stats d'un duo sont les stats
# d'équipe des parties où il apparaît, quel que soit le 3e membre.
_STAT_SUMS_SQL = """
           sum(t.gold_diff_5), count(t.gold_diff_5),
           sum(t.gold_diff_10), count(t.gold_diff_10),
           sum(t.gold_diff_15), count(t.gold_diff_15),
           sum(t.vision_score), count(t.vision_score),
           sum(t.drakes_taken), count(t.drakes_taken),
           count(*) FILTER (WHERE t.soul_taken), count(t.soul_taken),
           count(*) FILTER (WHERE t.herald_taken), count(t.herald_taken),
           count(*) FILTER (WHERE t.first_tower), count(t.first_tower)
"""
_STAT_SUMS_COLUMNS = """
                          gold5_sum, gold5_n, gold10_sum, gold10_n, gold15_sum, gold15_n,
                          vision_sum, vision_n, drakes_sum, drakes_n,
                          soul_sum, soul_n, herald_sum, herald_n, tower1_sum, tower1_n
"""

_DUO_SQL = f"""
    INSERT INTO agg_duo (patch, platform, roles, champ_a, champ_b, games, wins,
                         {_STAT_SUMS_COLUMNS})
    SELECT m.patch, m.platform, d.roles, d.champ_a, d.champ_b,
           count(*), count(*) FILTER (WHERE t.win),
           {_STAT_SUMS_SQL}
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

_TRIO_SQL = f"""
    INSERT INTO agg_trio (patch, platform, jgl_champion, mid_champion, sup_champion,
                          games, wins,
                          {_STAT_SUMS_COLUMNS})
    SELECT m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion,
           count(*), count(*) FILTER (WHERE t.win),
           {_STAT_SUMS_SQL}
    FROM match_trio_stats t
    JOIN matches m USING (match_id)
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion
"""

_TRIO_VS_CHAMPION_SQL = """
    INSERT INTO agg_trio_vs_champion (patch, platform, jgl_champion, mid_champion,
                                      sup_champion, enemy_role, enemy_champion, games, wins)
    SELECT m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion,
           p.role, p.champion_id,
           count(*), count(*) FILTER (WHERE t.win)
    FROM match_trio_stats t
    JOIN matches m USING (match_id)
    JOIN match_participants p ON p.match_id = t.match_id AND p.team_id <> t.team_id
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion,
             p.role, p.champion_id
"""

_TABLES_SQL = {
    "agg_champion": _CHAMPION_SQL,
    "agg_duo": _DUO_SQL,
    "agg_trio": _TRIO_SQL,
    "agg_trio_vs_champion": _TRIO_VS_CHAMPION_SQL,
}


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
