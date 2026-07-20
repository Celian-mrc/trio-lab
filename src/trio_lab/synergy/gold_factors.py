"""Régression linéaire : qu'est-ce qui CONSTRUIT l'avantage au gold @15 ?

En amont de `win_factors.py` (qui prédit la victoire à partir de l'état de
jeu, `team_gold_diff_15` y compris) : ce modèle prédit `gold_diff_15`
lui-même — variable CONTINUE, OLS pondéré (forme fermée, pas d'itération,
contrairement à l'IRLS logistique) — à partir de 2 blocs temporellement
ordonnés (audit méthodologique en 2 recherches, 2026-07-20, sources dans
docs/ROADMAP.md) :

1. DRAFT (avant la partie) — représenté par 3 scores déjà matérialisés
   ailleurs dans le site (target/mean encoding régularisé, pas de one-hot
   sur ~170 champions × 5 rôles) :
   - `team_baseline_wr` : WR baseline des 5 picks (`agg_champion`, AU PATCH
     de la game elle-même, pas la fenêtre — reflète la force du pick à
     l'époque où cette game a été jouée).
   - `team_matchup_delta` : delta de matchup 1v1 même rôle vs l'adversaire
     direct (`score_matchup`), moyenné sur les rôles où la donnée existe.
   - `team_trio_synergy` : synergie du trio jgl/mid/sup posé (`score_trio`)
     — top/bot n'ont pas de synergie native pour l'instant (pas de "trio"
     les concernant), extension possible plus tard (ex. duo bot lane).
2. EXÉCUTION PRÉCOCE (0-15 min) — uniquement ce qui a une vraie coupure
   temporelle à 15 min :
   - `jgl_cs_diff_15` (`match_trio_stats`, déjà team-level malgré son nom).
   - `first_blood_team` : au moins un membre de l'équipe crédité du 1er sang
     (`match_role_stats.first_blood`, agrégé par OR — événement unique,
     toujours avant 15 min par construction).
   - `herald_taken_pre15`/`dragons_taken_pre15` (migration 030) : dérivés
     des events de timeline (déjà timestampés) avec une coupure exacte à
     15 min — contrairement à `herald_taken`/`soul_taken` (partie entière,
     inclus dans win_factors, jamais ici : causalité inversée, un héraut
     pris à 22 min ne peut pas causer un avantage mesuré à 15 min).
   - `wards_pre15` (migration 030) : Riot n'expose `visionScore` qu'en
     cumulé fin de partie, à AUCUN timestamp intermédiaire — proxy le plus
     honnête borné à 15 min, nombre de wards posées + détruites (events
     bruts WARD_PLACED/WARD_KILL de la timeline). Pas identique à
     `vision_score`.
   Ces 3 dernières colonnes n'ont PAS de backfill possible (timeline brute
   jamais conservée après extraction, CLAUDE.md) : NULL sur tout
   l'historique déjà collecté, se peuplent seulement à partir du déploiement
   de la migration 030 — `_FETCH_SQL` les filtre sur leur présence plutôt
   que planter, `refresh()` publie normalement (ou reste sous `min_rows` et
   se tait) une fois assez de games fraîches accumulées.

UN SEUL modèle à 2 blocs, pas 2 modèles en cascade : une cascade (draft →
gold, puis comportements → résidu) doit résidualiser les DEUX côtés pour
respecter Frisch-Waugh-Lovell, et même correcte elle biaise les
erreurs-types ("generated regressors", Pagan 1984, *Int. Econ. Rev.*
25(1):221-247). `_fit_population` ajuste deux régressions IMBRIQUÉES
(bloc draft seul, puis draft+exécution) pour rapporter R²(draft),
R²(complet) et ΔR² — la part de variance que l'exécution ajoute au-delà du
draft — sans jamais construire de résidu généré.

Diagnostic VIF + ridge adaptatif : partagés avec `win_factors.py` via
`_linalg` (même philosophie, cf. son docstring).

Rafraîchissement MANUEL (`python -m trio_lab.synergy.gold_factors`), jamais
dans le cycle service — même raisonnement que `win_factors.py`.
"""

from __future__ import annotations

import argparse
import logging

import psycopg

from trio_lab import config, db
from trio_lab.synergy import _linalg
from trio_lab.synergy.windows import PatchWindow, make_window

logger = logging.getLogger(__name__)

DRAFT_FEATURES = ("team_baseline_wr", "team_matchup_delta", "team_trio_synergy")
# herald_taken_pre15/dragons_taken_pre15/wards_pre15 (migration 030) : sur
# match_trio_stats, sans backfill possible (timeline brute jamais conservée,
# cf. migration) — NULL sur tout l'historique déjà collecté, se peuplent
# seulement à partir du déploiement. `_FETCH_SQL` filtre sur leur présence
# plutôt que planter (même principe que les 7 paires de duo étendues).
EXECUTION_FEATURES = (
    "jgl_cs_diff_15",
    "first_blood_team",
    "herald_taken_pre15",
    "dragons_taken_pre15",
    "wards_pre15",
)
FEATURES = DRAFT_FEATURES + EXECUTION_FEATURES
BLOCK_OF = {f: "draft" for f in DRAFT_FEATURES} | {f: "execution" for f in EXECUTION_FEATURES}
CONTINUOUS = frozenset(FEATURES) - {"first_blood_team", "herald_taken_pre15"}
DEFAULT_MIN_ROWS = 200  # sous ce seuil, l'ajustement est trop instable pour être publié

_FETCH_SQL = """
    WITH picks AS (
        SELECT mrs.match_id, mrs.team_id, mrs.role, mrs.champion_id, mrs.gold_15,
               m.patch,
               enemy.champion_id AS enemy_champion_id,
               enemy.gold_15 AS enemy_gold_15
        FROM match_role_stats mrs
        JOIN matches m ON m.match_id = mrs.match_id
        JOIN match_role_stats enemy
            ON enemy.match_id = mrs.match_id AND enemy.role = mrs.role
           AND enemy.team_id <> mrs.team_id
        WHERE m.patch = ANY(%(patches)s)
          AND mrs.gold_15 IS NOT NULL AND enemy.gold_15 IS NOT NULL
    ),
    -- agg_champion n'a PAS de ligne platform='all' matérialisée (contrairement
    -- à score_matchup/score_trio, cf. `scores.add_combined_platform`) : il
    -- faut sommer nous-mêmes toutes les régions, même logique que
    -- `queries.champion_role_baseline_list`.
    baseline_wr AS (
        SELECT patch, role, champion_id,
               sum(wins)::real / NULLIF(sum(games), 0) AS wr
        FROM agg_champion
        WHERE patch = ANY(%(patches)s)
        GROUP BY patch, role, champion_id
    ),
    enriched AS (
        SELECT p.match_id, p.team_id, p.patch, p.gold_15, p.enemy_gold_15,
               bc.wr AS baseline_wr,
               sm.delta AS matchup_delta
        FROM picks p
        LEFT JOIN baseline_wr bc
            ON bc.patch = p.patch AND bc.role = p.role AND bc.champion_id = p.champion_id
        LEFT JOIN score_matchup sm
            ON sm.window_label = %(window_label)s AND sm.platform = 'all' AND sm.role = p.role
           AND sm.champ_a = p.champion_id AND sm.champ_b = p.enemy_champion_id
    ),
    team_agg AS (
        SELECT match_id, team_id, patch,
               sum(gold_15) - sum(enemy_gold_15) AS team_gold_diff_15,
               avg(baseline_wr) AS team_baseline_wr,
               avg(matchup_delta) AS team_matchup_delta,
               count(*) AS n_roles
        FROM enriched
        GROUP BY match_id, team_id, patch
    ),
    first_blood_agg AS (
        -- bool_or(first_blood) reste NULL si les 5 lignes de l'équipe ont
        -- first_blood NULL (colonne ajoutée par la migration 025, pas
        -- rétro-remplie sur tout l'historique) — coalesce à false, pas
        -- d'autre signal disponible pour ces games.
        SELECT match_id, team_id, coalesce(bool_or(first_blood), false) AS first_blood_team
        FROM match_role_stats
        GROUP BY match_id, team_id
    )
    SELECT ta.patch,
           ta.team_gold_diff_15,
           ta.team_baseline_wr,
           coalesce(ta.team_matchup_delta, 0.0) AS team_matchup_delta,
           coalesce(st.synergy, 0.0) AS team_trio_synergy,
           mt.jgl_cs_diff_15,
           fb.first_blood_team,
           mt.herald_taken_pre15,
           mt.dragons_taken_pre15,
           mt.wards_pre15
    FROM team_agg ta
    JOIN match_trio_stats mt ON mt.match_id = ta.match_id AND mt.team_id = ta.team_id
    JOIN first_blood_agg fb ON fb.match_id = ta.match_id AND fb.team_id = ta.team_id
    LEFT JOIN score_trio st
        ON st.window_label = %(window_label)s AND st.platform = 'all'
       AND st.jgl_champion = mt.jgl_champion AND st.mid_champion = mt.mid_champion
       AND st.sup_champion = mt.sup_champion
    WHERE ta.n_roles = 5 AND ta.team_baseline_wr IS NOT NULL
      AND mt.jgl_cs_diff_15 IS NOT NULL
      -- migration 030, pas de backfill : NULL tant que la game a été
      -- collectée avant le déploiement — toujours les 3 ensemble (même
      -- INSERT dans extract.pre15_stats), un seul filtre suffit.
      AND mt.herald_taken_pre15 IS NOT NULL
"""


def _fetch_rows(conn: psycopg.Connection, window: PatchWindow) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(_FETCH_SQL, {"patches": list(window.patches), "window_label": window.label})
        return cur.fetchall()


def _standardize(rows: list[dict]) -> tuple[dict[str, float], dict[str, float]]:
    means: dict[str, float] = {}
    stds: dict[str, float] = {}
    n = len(rows)
    for f in CONTINUOUS:
        vals = [float(r[f]) for r in rows]
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        means[f] = mean
        stds[f] = (var**0.5) or 1.0
    return means, stds


def _design_matrix(
    rows: list[dict],
    means: dict[str, float],
    stds: dict[str, float],
    weights: dict[str, float],
    features: tuple[str, ...],
) -> tuple[list[list[float]], list[float], list[float]]:
    x_rows: list[list[float]] = []
    y: list[float] = []
    row_weights: list[float] = []
    for r in rows:
        x_row = [1.0]  # intercept
        for f in features:
            v = float(r[f])
            if f in CONTINUOUS:
                v = (v - means[f]) / stds[f]
            x_row.append(v)
        x_rows.append(x_row)
        y.append(float(r["team_gold_diff_15"]))
        row_weights.append(weights.get(r["patch"], 0.0))
    return x_rows, y, row_weights


def _fit_block(
    rows: list[dict],
    means: dict[str, float],
    stds: dict[str, float],
    weights: dict[str, float],
    features: tuple[str, ...],
    *,
    ridge: float,
) -> tuple[list[float], float]:
    """Ajuste un OLS pondéré sur `features` (sous-ensemble de FEATURES) et
    retourne (coefficients, R² pondéré) — un seul fit, forme fermée."""
    x_rows, y, row_weights = _design_matrix(rows, means, stds, weights, features)
    beta = _linalg.fit_weighted_ols(x_rows, y, row_weights, ridge=ridge)
    r2 = _linalg.weighted_r_squared(x_rows, y, beta, row_weights)
    return beta, r2


def _fit_window(
    conn: psycopg.Connection, window: PatchWindow, *, min_rows: int
) -> list[dict] | None:
    rows = _fetch_rows(conn, window)
    if len(rows) < min_rows:
        logger.info(
            "gold_factors %s : %d lignes < seuil %d, ignoré", window.label, len(rows), min_rows
        )
        return None

    means, stds = _standardize(rows)
    weights = window.weights_for(())  # pas de coupure de rework, cf. win_factors

    continuous_cols = [i + 1 for i, f in enumerate(FEATURES) if f in CONTINUOUS]
    full_x_rows, _, _ = _design_matrix(rows, means, stds, weights, FEATURES)
    vifs = _linalg.compute_vif(full_x_rows, continuous_cols)
    max_vif = max(vifs.values(), default=0.0)
    if max_vif > _linalg.VIF_ALERT_THRESHOLD:
        offenders = {
            FEATURES[c - 1]: round(v, 1) for c, v in vifs.items() if v > _linalg.VIF_ALERT_THRESHOLD
        }
        ridge = _linalg.VIF_RIDGE
        logger.warning(
            "gold_factors %s : VIF > %.0f détecté %s, ridge renforcé à %.1f",
            window.label,
            _linalg.VIF_ALERT_THRESHOLD,
            offenders,
            ridge,
        )
    else:
        ridge = _linalg.DEFAULT_RIDGE
        logger.info(
            "gold_factors %s : VIF max %.2f (sous le seuil %.0f)",
            window.label,
            max_vif,
            _linalg.VIF_ALERT_THRESHOLD,
        )

    # Régression imbriquée 1 : draft seul → R²(draft).
    _, r2_draft_only = _fit_block(rows, means, stds, weights, DRAFT_FEATURES, ridge=ridge)
    # Régression imbriquée 2 : draft + exécution → coefficients finaux + R²(complet).
    beta_full, r2_full = _fit_block(rows, means, stds, weights, FEATURES, ridge=ridge)

    n = len(rows)
    feature_rows = [
        {
            "window_label": window.label,
            "block": BLOCK_OF.get(name),
            "feature": name,
            "coef": coef,
            "n": n,
        }
        for name, coef in zip(("intercept", *FEATURES), beta_full, strict=True)
    ]
    diagnostic_rows = [
        {
            "window_label": window.label,
            "block": None,
            "feature": "_r2_draft_only",
            "coef": r2_draft_only,
            "n": n,
        },
        {
            "window_label": window.label,
            "block": None,
            "feature": "_r2_full",
            "coef": r2_full,
            "n": n,
        },
    ]
    return feature_rows + diagnostic_rows


_INSERT_SQL = """
    INSERT INTO score_gold_factors (window_label, block, feature, coef, n)
    VALUES (%(window_label)s, %(block)s, %(feature)s, %(coef)s, %(n)s)
"""


def refresh(
    window: PatchWindow, *, dsn: str | None = None, min_rows: int = DEFAULT_MIN_ROWS
) -> int:
    """Ajuste le modèle et matérialise dans `score_gold_factors`. Retourne le
    nombre de lignes écrites (0 si sous le seuil `min_rows`, pas d'erreur).

    DELETE + INSERT (pas UPSERT), même raisonnement que `win_factors.refresh`
    (table minuscule, FEATURES peut changer d'une session à l'autre).
    """
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        # cf. win_factors.refresh : timeout par défaut trop court + piège de
        # planification Postgres sur les filtres portant sur des colonnes
        # dérivées d'agrégat dans une CTE (n_roles = 5 ici aussi).
        conn.execute("SET LOCAL statement_timeout = '5min'")
        conn.execute("SET LOCAL enable_nestloop = off")
        rows_to_write = _fit_window(conn, window, min_rows=min_rows)
        count = len(rows_to_write) if rows_to_write else 0
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM score_gold_factors WHERE window_label = %s", (window.label,))
            if rows_to_write:
                cur.executemany(_INSERT_SQL, rows_to_write)
    logger.info("gold_factors fenêtre %s rafraîchie : %d lignes", window.label, count)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.synergy.gold_factors", description=__doc__)
    parser.add_argument(
        "--patches", required=True, help="fenêtre, du plus récent au plus ancien, ex. 16.14,16.13"
    )
    parser.add_argument("--min-rows", type=int, default=DEFAULT_MIN_ROWS)
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    window = make_window([p.strip() for p in args.patches.split(",") if p.strip()])
    refresh(window, min_rows=args.min_rows)


if __name__ == "__main__":
    main()
