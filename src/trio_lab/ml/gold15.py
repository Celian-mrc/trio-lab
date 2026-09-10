"""Le draft explique-t-il le gold@15 ÉQUIPE (5 rôles), pas juste la victoire ?

Retour utilisateur (2026-09-09) : `win_factors` a établi que `team_gold_diff_15`
(gold de l'ÉQUIPE ENTIÈRE à 15 min, 5 rôles) est le facteur le plus
déterminant de la victoire (AUC 0,82-0,85). Le v1/v2 de `ml.train` a montré
que le draft seul explique très peu la VICTOIRE (AUC ~0,54) malgré un feature
set élargi. Deux explications possibles, à distinguer empiriquement plutôt
qu'en spéculant :
(a) le draft n'explique même pas bien l'état à 15 min → chercher un signal
    de victoire depuis le draft est sans espoir avec ces données ;
(b) le draft explique raisonnablement le gold@15, mais le gold@15 seul ne
    détermine pas qui gagne (tout ce qui se passe ensuite noie le signal) →
    un modèle en 2 étages (draft → gold@15 prédit → victoire, en réutilisant
    la relation gold@15→victoire déjà établie par `win_factors`) pourrait
    battre le modèle direct draft→victoire.

Ce module teste ça : RÉGRESSION (pas classification) sur le `team_gold_diff_15`
RÉEL de chaque match (calculé depuis `match_role_stats`, même requête que
`synergy/resilience.py::_TEAM_AGG_SQL`/`stats/aggregate.py`), avec EXACTEMENT
les mêmes features de draft que `ml.features` (réutilisées telles quelles,
mêmes anti-fuite leave-one-patch-out) — seul le label change.
"""

from __future__ import annotations

import logging

import psycopg

from trio_lab.ml import features

logger = logging.getLogger(__name__)


def _fetch_team_gold_diff_15(conn: psycopg.Connection, patches: list[str]) -> dict:
    """Clé (match_id, team_id) -> gold@15 de l'ÉQUIPE ENTIÈRE (5 rôles) MOINS
    celui de l'équipe adverse — le vrai label observé du match, PAS une
    moyenne historique. Même construction que `resilience._TEAM_AGG_SQL`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH team_agg AS (
                SELECT mrs.match_id, mrs.team_id, sum(mrs.gold_15) AS gold_15,
                       count(*) AS n_roles
                FROM match_role_stats mrs
                JOIN matches m ON m.match_id = mrs.match_id AND m.patch = ANY(%(patches)s)
                WHERE mrs.gold_15 IS NOT NULL
                GROUP BY mrs.match_id, mrs.team_id
            )
            SELECT ta.match_id, ta.team_id, ta.gold_15 - ea.gold_15 AS team_gold_diff_15
            FROM team_agg ta
            JOIN team_agg ea ON ea.match_id = ta.match_id AND ea.team_id <> ta.team_id
            WHERE ta.n_roles = 5 AND ea.n_roles = 5
            """,
            {"patches": patches},
        )
        return {(match_id, team_id): diff for match_id, team_id, diff in cur}


def build_gold15_table(
    conn: psycopg.Connection, patches: list[str]
) -> tuple[list[list[float]], list[float], list[str]]:
    """Construit `(X, y, match_ids)` : `X` = mêmes features de draft que
    `ml.features.FEATURE_NAMES`, `y` = `team_gold_diff_15` RÉEL du match
    (continu, pas un label 0/1). Réutilise directement les fonctions privées
    de `ml.features` — même package, pas de duplication."""
    agg_champion = features._fetch_agg_champion(conn, patches)
    agg_duo = features._fetch_agg_duo(conn, patches)
    agg_trio = features._fetch_agg_trio(conn, patches)
    agg_matchup = features._fetch_agg_matchup(conn, patches)
    agg_trio_stats = features._fetch_agg_trio_stats(conn, patches)
    range_theoretical = features._fetch_range_theoretical(conn)
    agg_trio_duration = features._fetch_agg_trio_duration(conn, patches)
    team_gold_diff_15 = _fetch_team_gold_diff_15(conn, patches)

    X: list[list[float]] = []
    y: list[float] = []
    match_ids: list[str] = []
    dropped_features = 0
    dropped_no_label = 0
    # team_id n'est pas exposé par `features._fetch_match_rows` (jointure
    # us/them sans le garder) : requête dédiée pour indexer team_gold_diff_15.
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT us.match_id, us.team_id, m.patch,
                   us.jgl_champion AS us_jgl, us.mid_champion AS us_mid,
                   us.sup_champion AS us_sup,
                   them.jgl_champion AS them_jgl, them.mid_champion AS them_mid,
                   them.sup_champion AS them_sup
            FROM match_trio_stats us
            JOIN match_trio_stats them
                ON them.match_id = us.match_id AND them.team_id <> us.team_id
            JOIN matches m ON m.match_id = us.match_id AND m.patch = ANY(%(patches)s)
            """,
            {"patches": patches},
        )
        rows_with_team = cur.fetchall()

    for row in rows_with_team:
        label = team_gold_diff_15.get((row["match_id"], row["team_id"]))
        if label is None:
            dropped_no_label += 1
            continue
        row_features = features._row_features(
            row,
            agg_champion,
            agg_duo,
            agg_trio,
            agg_matchup,
            agg_trio_stats,
            range_theoretical,
            agg_trio_duration,
            patches,
        )
        if row_features is None:
            dropped_features += 1
            continue
        X.append(row_features)
        y.append(float(label))
        match_ids.append(row["match_id"])

    logger.info(
        "gold15 fenêtre %s : %d lignes complètes, %d exclues (baseline manquante), "
        "%d exclues (pas de team_gold_diff_15 — match_role_stats incomplet)",
        "+".join(patches),
        len(X),
        dropped_features,
        dropped_no_label,
    )
    return X, y, match_ids
