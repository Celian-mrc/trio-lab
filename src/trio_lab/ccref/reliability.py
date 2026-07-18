"""Correction connue de fiabilité empirique du CC — cas Nocturne.

Nocturne gonfle `timeCCingOthers` sans immobiliser réellement plus que la
moyenne (ratio secondes-de-CC / immobilisation ~21x la médiane des autres
champions, données prod du 2026-07-12) : son ultime Paranoia réduit la
vision de tous les ennemis vivants (jusqu'à 4 cibles, ~8 s chacune), un
effet global sans commune mesure avec un vrai CC dur (stun/root), très
probablement compté à tort comme du CC dans les stats de fin de partie de
Riot — même famille de biais que `totalTimeCCDealt`, déjà écarté (migration
005). Confirmé par le score théorique du kit (`cc_reference.csv`) : seul
son E (fear 1.75 s) y est noté, Paranoia n'y figure pas du tout.

Le coefficient a été dérivé une fois (barrière de Tukey Q3+3×IQR sur ce
ratio, calculée sur `match_participants` — seul Nocturne dépassait le
seuil sur l'échantillon observé, 134 champions) puis gelé en constante :
une mécanique de détection statistique périodique serait disproportionnée
pour un seul champion. À réévaluer si un autre kit présente un jour un
écart similaire (cf. memory phase2b-relecture-workflow).
"""

from __future__ import annotations

import logging

import psycopg

logger = logging.getLogger(__name__)

# {champion_id: coefficient ∈ (0, 1]} appliqué à `timeCCingOthers` avant
# sommation trio (cf. stats.extract.combat_stats). Absent d'ici = 1.0.
CC_TIME_RELIABILITY: dict[int, float] = {
    56: 0.22,  # Nocturne — cf. docstring du module
}


def backfill_trio_cc(
    conn: psycopg.Connection,
    *,
    patch: str | None = None,
    reliability: dict[int, float] = CC_TIME_RELIABILITY,
) -> int:
    """Recalcule `match_trio_stats.cc_time_s` depuis `match_participants` corrigé.

    Limité aux matchs dont `match_participants` porte encore `cc_time_s`
    (rétention plus courte que `match_trio_stats`, et trou historique avant
    le 2026-07-10 — cf. `stats.backfill_participant_cc`) — au-delà, la
    donnée source est absente et le trio garde son ancienne valeur.
    `patch=None` : tous les matchs éligibles."""
    champ_ids = list(reliability.keys())
    coeffs = list(reliability.values())
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE match_trio_stats t
            SET cc_time_s = corrected.total
            FROM (
                SELECT p.match_id, p.team_id,
                       round(sum(p.cc_time_s * coalesce(r.reliability, 1.0)))::int AS total
                FROM match_participants p
                JOIN matches m ON m.match_id = p.match_id
                LEFT JOIN (
                    SELECT * FROM unnest(%(champ_ids)s::int[], %(coeffs)s::real[])
                        AS r(champion_id, reliability)
                ) r ON r.champion_id = p.champion_id
                WHERE p.role IN ('JUNGLE', 'MIDDLE', 'UTILITY')
                  AND p.cc_time_s IS NOT NULL
                  AND (%(patch)s::text IS NULL OR m.patch = %(patch)s::text)
                GROUP BY p.match_id, p.team_id
            ) corrected
            WHERE t.match_id = corrected.match_id AND t.team_id = corrected.team_id
            """,
            {"champ_ids": champ_ids, "coeffs": coeffs, "patch": patch},
        )
        n = cur.rowcount
    logger.info("match_trio_stats.cc_time_s recalculé pour %d équipes (patch=%s)", n, patch)
    return n


def backfill_trio_cc_per_role(
    conn: psycopg.Connection,
    *,
    patch: str | None = None,
    reliability: dict[int, float] = CC_TIME_RELIABILITY,
) -> int:
    """Renseigne `jgl/mid/sup_cc_time_s` (migration 020) depuis `match_participants`.

    Backfill un-shot pour les lignes ingérées avant l'ajout de ces colonnes
    (2026-07-18) : ne touche que les lignes encore NULL (idempotent, ne
    recalcule jamais ce que le collector a déjà posé) et seulement les
    matchs dont les 3 participants portent encore `cc_time_s` (même trou
    historique que `backfill_trio_cc`, avant le 2026-07-10)."""
    champ_ids = list(reliability.keys())
    coeffs = list(reliability.values())
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE match_trio_stats t
            SET jgl_cc_time_s = corrected.jgl,
                mid_cc_time_s = corrected.mid,
                sup_cc_time_s = corrected.sup
            FROM (
                SELECT pj.match_id, pj.team_id,
                       round(pj.cc_time_s * coalesce(rj.reliability, 1.0))::int AS jgl,
                       round(pm.cc_time_s * coalesce(rm.reliability, 1.0))::int AS mid,
                       round(pu.cc_time_s * coalesce(ru.reliability, 1.0))::int AS sup
                FROM match_participants pj
                JOIN match_participants pm
                    ON pm.match_id = pj.match_id AND pm.team_id = pj.team_id AND pm.role = 'MIDDLE'
                JOIN match_participants pu
                    ON pu.match_id = pj.match_id AND pu.team_id = pj.team_id AND pu.role = 'UTILITY'
                JOIN matches m ON m.match_id = pj.match_id
                LEFT JOIN (
                    SELECT * FROM unnest(%(champ_ids)s::int[], %(coeffs)s::real[])
                        AS r(champion_id, reliability)
                ) rj ON rj.champion_id = pj.champion_id
                LEFT JOIN (
                    SELECT * FROM unnest(%(champ_ids)s::int[], %(coeffs)s::real[])
                        AS r(champion_id, reliability)
                ) rm ON rm.champion_id = pm.champion_id
                LEFT JOIN (
                    SELECT * FROM unnest(%(champ_ids)s::int[], %(coeffs)s::real[])
                        AS r(champion_id, reliability)
                ) ru ON ru.champion_id = pu.champion_id
                WHERE pj.role = 'JUNGLE'
                  AND pj.cc_time_s IS NOT NULL
                  AND pm.cc_time_s IS NOT NULL
                  AND pu.cc_time_s IS NOT NULL
                  AND (%(patch)s::text IS NULL OR m.patch = %(patch)s::text)
            ) corrected
            WHERE t.match_id = corrected.match_id AND t.team_id = corrected.team_id
              AND t.jgl_cc_time_s IS NULL
            """,
            {"champ_ids": champ_ids, "coeffs": coeffs, "patch": patch},
        )
        n = cur.rowcount
    logger.info("jgl/mid/sup_cc_time_s backfillés pour %d équipes (patch=%s)", n, patch)
    return n
