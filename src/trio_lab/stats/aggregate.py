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

# Sommes de stats partagées trio/duo : les stats d'un duo sont les stats
# d'équipe des parties où il apparaît, quel que soit le 3e membre.
# vision_score, drakes_taken, cc_time_s : PAR MINUTE (/ durée en minutes), pas
# cumulés — le cumulé est mécaniquement gonflé par la durée de la partie (plus
# de temps = plus de wards/drakes/CC possibles). Corrélations mesurées avec la
# durée (retour utilisateur, 2026-07-13) : vision +0.22, drakes +0.41, CC +0.64
# (le pire des trois) — le score par minute isole le vrai signal de l'artefact
# de durée. Voir aussi `ccref/score.py` (EMPIRICAL_CEILING_S_PER_MIN, recalibré
# pour l'échelle par minute).
_STAT_SUMS_SQL = """
           sum(t.gold_diff_5), count(t.gold_diff_5),
           sum(t.gold_diff_10), count(t.gold_diff_10),
           sum(t.gold_diff_15), count(t.gold_diff_15),
           sum(t.vision_score / (m.game_duration_s / 60.0)), count(t.vision_score),
           sum(t.drakes_taken / (m.game_duration_s / 60.0)), count(t.drakes_taken),
           count(*) FILTER (WHERE t.soul_taken), count(t.soul_taken),
           count(*) FILTER (WHERE t.herald_taken), count(t.herald_taken),
           count(*) FILTER (WHERE t.first_tower), count(t.first_tower),
           sum(t.cc_time_s / (m.game_duration_s / 60.0)), count(t.cc_time_s)
"""
_STAT_SUMS_COLUMNS = """
                          gold5_sum, gold5_n, gold10_sum, gold10_n, gold15_sum, gold15_n,
                          vision_sum, vision_n, drakes_sum, drakes_n,
                          soul_sum, soul_n, herald_sum, herald_n, tower1_sum, tower1_n,
                          cc_sum, cc_n
"""

# CC empirique par membre (migration 020), en plus du total (_STAT_SUMS_SQL).
# Trio : colonnes directes (t.jgl/mid/sup_cc_time_s). Duo : pas de rôle fixe
# (jgl_mid/jgl_sup/mid_sup selon `roles`) — champ_a_cc/champ_b_cc génériques,
# la CROSS JOIN LATERAL choisit la bonne colonne source par paire de rôles.
_TRIO_CC_POSITION_SQL = """
           sum(t.jgl_cc_time_s / (m.game_duration_s / 60.0)), count(t.jgl_cc_time_s),
           sum(t.mid_cc_time_s / (m.game_duration_s / 60.0)), count(t.mid_cc_time_s),
           sum(t.sup_cc_time_s / (m.game_duration_s / 60.0)), count(t.sup_cc_time_s)
"""
_TRIO_CC_POSITION_COLUMNS = "jgl_cc_sum, jgl_cc_n, mid_cc_sum, mid_cc_n, sup_cc_sum, sup_cc_n"
_DUO_CC_POSITION_SQL = """
           sum(d.champ_a_cc_time_s / (m.game_duration_s / 60.0)), count(d.champ_a_cc_time_s),
           sum(d.champ_b_cc_time_s / (m.game_duration_s / 60.0)), count(d.champ_b_cc_time_s)
"""
_DUO_CC_POSITION_COLUMNS = "champ_a_cc_sum, champ_a_cc_n, champ_b_cc_sum, champ_b_cc_n"

_DUO_SQL = f"""
    INSERT INTO agg_duo (patch, platform, roles, champ_a, champ_b, games, wins,
                         {_STAT_SUMS_COLUMNS}, {_DUO_CC_POSITION_COLUMNS})
    SELECT m.patch, m.platform, d.roles, d.champ_a, d.champ_b,
           count(*), count(*) FILTER (WHERE t.win),
           {_STAT_SUMS_SQL}, {_DUO_CC_POSITION_SQL}
    FROM match_trio_stats t
    JOIN matches m USING (match_id)
    CROSS JOIN LATERAL (VALUES
        ('jgl_mid', t.jgl_champion, t.mid_champion, t.jgl_cc_time_s, t.mid_cc_time_s),
        ('jgl_sup', t.jgl_champion, t.sup_champion, t.jgl_cc_time_s, t.sup_cc_time_s),
        ('mid_sup', t.mid_champion, t.sup_champion, t.mid_cc_time_s, t.sup_cc_time_s)
    ) AS d(roles, champ_a, champ_b, champ_a_cc_time_s, champ_b_cc_time_s)
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, d.roles, d.champ_a, d.champ_b
"""

# Paires de rôles hors trio jgl/mid/sup (Phase 7, duo généralisé) : sourcées
# sur match_role_stats (5 rôles bruts) plutôt que match_trio_stats, qui ne
# connaît que jgl/mid/sup. Gold : vrai diff de la PAIRE (ses 2 rôles vs les 2
# mêmes rôles de l'équipe adverse, auto-jointure ea/eb) — plus précis que le
# gold_diff_X trio réutilisé pour les 3 paires historiques ci-dessus.
# Objectifs (drakes/soul/herald/tour) : lus depuis match_trio_stats (jointure
# sur match_id/team_id) — stats d'équipe déjà calculées là, identiques quelle
# que soit la paire de rôles, pas dupliquées ici.
_DUO_EXT_SQL = f"""
    INSERT INTO agg_duo (patch, platform, roles, champ_a, champ_b, games, wins,
                         {_STAT_SUMS_COLUMNS}, {_DUO_CC_POSITION_COLUMNS})
    SELECT m.patch, m.platform, pair.roles, ra.champion_id, rb.champion_id,
           count(*), count(*) FILTER (WHERE mt.win),
           sum((ra.gold_5 + rb.gold_5) - (ea.gold_5 + eb.gold_5)),
           count((ra.gold_5 + rb.gold_5) - (ea.gold_5 + eb.gold_5)),
           sum((ra.gold_10 + rb.gold_10) - (ea.gold_10 + eb.gold_10)),
           count((ra.gold_10 + rb.gold_10) - (ea.gold_10 + eb.gold_10)),
           sum((ra.gold_15 + rb.gold_15) - (ea.gold_15 + eb.gold_15)),
           count((ra.gold_15 + rb.gold_15) - (ea.gold_15 + eb.gold_15)),
           sum((ra.vision_score + rb.vision_score) / (m.game_duration_s / 60.0)), count(*),
           sum(mt.drakes_taken / (m.game_duration_s / 60.0)), count(mt.drakes_taken),
           count(*) FILTER (WHERE mt.soul_taken), count(mt.soul_taken),
           count(*) FILTER (WHERE mt.herald_taken), count(mt.herald_taken),
           count(*) FILTER (WHERE mt.first_tower), count(mt.first_tower),
           sum((ra.cc_time_s + rb.cc_time_s) / (m.game_duration_s / 60.0)), count(*),
           sum(ra.cc_time_s / (m.game_duration_s / 60.0)), count(ra.cc_time_s),
           sum(rb.cc_time_s / (m.game_duration_s / 60.0)), count(rb.cc_time_s)
    FROM matches m
    JOIN match_trio_stats mt ON mt.match_id = m.match_id
    CROSS JOIN LATERAL (VALUES
        ('top_jgl', 'TOP', 'JUNGLE'), ('top_mid', 'TOP', 'MIDDLE'),
        ('top_bot', 'TOP', 'BOTTOM'), ('top_sup', 'TOP', 'UTILITY'),
        ('jgl_bot', 'JUNGLE', 'BOTTOM'), ('mid_bot', 'MIDDLE', 'BOTTOM'),
        ('bot_sup', 'BOTTOM', 'UTILITY')
    ) AS pair(roles, role_a, role_b)
    JOIN match_role_stats ra
        ON ra.match_id = m.match_id AND ra.team_id = mt.team_id AND ra.role = pair.role_a
    JOIN match_role_stats rb
        ON rb.match_id = m.match_id AND rb.team_id = mt.team_id AND rb.role = pair.role_b
    JOIN match_role_stats ea
        ON ea.match_id = m.match_id AND ea.team_id <> mt.team_id AND ea.role = pair.role_a
    JOIN match_role_stats eb
        ON eb.match_id = m.match_id AND eb.team_id = ea.team_id AND eb.role = pair.role_b
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, pair.roles, ra.champion_id, rb.champion_id
"""

_TRIO_SQL = f"""
    INSERT INTO agg_trio (patch, platform, jgl_champion, mid_champion, sup_champion,
                          games, wins,
                          {_STAT_SUMS_COLUMNS}, {_TRIO_CC_POSITION_COLUMNS})
    SELECT m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion,
           count(*), count(*) FILTER (WHERE t.win),
           {_STAT_SUMS_SQL}, {_TRIO_CC_POSITION_SQL}
    FROM match_trio_stats t
    JOIN matches m USING (match_id)
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion
"""

_DURATION_BUCKET_SQL = "LEAST(40, 5 * (m.game_duration_s / 300))"

_TRIO_DURATION_SQL = f"""
    INSERT INTO agg_trio_duration (patch, platform, jgl_champion, mid_champion,
                                   sup_champion, duration_bucket, games, wins)
    SELECT m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion,
           {_DURATION_BUCKET_SQL}, count(*), count(*) FILTER (WHERE t.win)
    FROM match_trio_stats t
    JOIN matches m USING (match_id)
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, t.jgl_champion, t.mid_champion, t.sup_champion,
             {_DURATION_BUCKET_SQL}
"""

_DUO_DURATION_SQL = f"""
    INSERT INTO agg_duo_duration (patch, platform, roles, champ_a, champ_b,
                                  duration_bucket, games, wins)
    SELECT m.patch, m.platform, d.roles, d.champ_a, d.champ_b,
           {_DURATION_BUCKET_SQL}, count(*), count(*) FILTER (WHERE t.win)
    FROM match_trio_stats t
    JOIN matches m USING (match_id)
    CROSS JOIN LATERAL (VALUES
        ('jgl_mid', t.jgl_champion, t.mid_champion),
        ('jgl_sup', t.jgl_champion, t.sup_champion),
        ('mid_sup', t.mid_champion, t.sup_champion)
    ) AS d(roles, champ_a, champ_b)
    WHERE m.patch = %(patch)s
    GROUP BY m.patch, m.platform, d.roles, d.champ_a, d.champ_b,
             {_DURATION_BUCKET_SQL}
"""

# agg_duo reçoit 2 INSERT (les 3 paires internes au trio depuis
# match_trio_stats, les 7 autres depuis match_role_stats) derrière un seul
# DELETE par patch — d'où des tuples de requêtes plutôt qu'une requête unique.
_TABLES_SQL: dict[str, tuple[str, ...]] = {
    "agg_champion": (_CHAMPION_SQL,),
    "agg_duo": (_DUO_SQL, _DUO_EXT_SQL),
    "agg_trio": (_TRIO_SQL,),
    "agg_trio_duration": (_TRIO_DURATION_SQL,),
    "agg_duo_duration": (_DUO_DURATION_SQL,),
}


def refresh(patch: str, *, dsn: str | None = None) -> dict[str, int]:
    """Recalcule les agrégats d'un patch. Retourne le nombre de lignes par table."""
    counts: dict[str, int] = {}
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        # Les sommes par minute (vision/drakes/CC, 2026-07-13) ajoutent une
        # division par ligne à _DUO_SQL/_TRIO_SQL — au-delà du statement_timeout
        # par défaut du rôle applicatif sur de gros volumes de matchs.
        conn.execute("SET LOCAL statement_timeout = '10min'")
        for table, sqls in _TABLES_SQL.items():
            conn.execute(
                psycopg.sql.SQL("DELETE FROM {} WHERE patch = %(patch)s").format(
                    psycopg.sql.Identifier(table)
                ),
                {"patch": patch},
            )
            counts[table] = sum(conn.execute(sql, {"patch": patch}).rowcount for sql in sqls)
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
