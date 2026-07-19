"""Régression logistique multi-variables : qu'est-ce qui fait gagner ?

Matérialise l'analyse menée en session (2026-07-19), productionisée : IRLS —
Newton-Raphson pour la logistique — pure Python, cohérent avec la philosophie
du projet, pas de numpy/scipy pour un ajustement à ~12 variables sur
quelques dizaines de milliers de lignes.

Stats d'ÉQUIPE COMPLÈTE (5 rôles), pas seulement jgl/mid/sup : contrairement
à la première version (qui lisait `match_trio_stats`, limité au trio),
`_fetch_rows` agrège `match_role_stats` (les 5 rôles) par équipe — un coach
raisonne en gold/vision/CC d'ÉQUIPE, pas d'un sous-ensemble de 3 joueurs sur
5 (retour utilisateur, 2026-07-19). `damage_share` et `kill_participation`
(spécifiques au trio, pas de sens au niveau équipe complète — la part de
l'équipe dans les dégâts de l'équipe vaut toujours 100 %) sont retirées ;
`jgl_cs_diff_15` (déjà calculé, team-level malgré son nom) est ajoutée.

Deux populations, deux jeux de coefficients :
- « all » : toutes les games (fenêtre courante, cas complets).
- « behind_gold15 » : games où l'ÉQUIPE est derrière au gold à 15 min —
  leviers de comeback, mesurés différemment de la population complète
  (vision/efficacité ressources y pèsent plus, cf. recherche session).

Poids par patch : mêmes poids que `synergy.compute`/`windows.PatchWindow`
(pas de coupure de rework — aucune variable ici n'est liée à un champion
précis), appliqués comme poids d'observation dans l'IRLS (`w_i = poids_patch
× μ(1-μ)`), pas seulement une repondération a posteriori.

Rafraîchissement MANUEL (`python -m trio_lab.synergy.win_factors`), jamais
dans le cycle service : un facteur de victoire est un signal de patch, pas de
cycle de collecte — même philosophie que `ccref.sync_theoretical`. Dépend de
`match_role_stats` (déployée le 19/07/2026, historique plus court que
`match_trio_stats`) : volumétrie qui grandit avec le temps.
"""

from __future__ import annotations

import argparse
import logging
import math

import psycopg

from trio_lab import config, db
from trio_lab.synergy.windows import PatchWindow, make_window

logger = logging.getLogger(__name__)

# Continues : standardisées (z-score) avant ajustement, coefficient lisible
# comme « effet pour +1 écart-type ». Booléennes (herald/soul/first_tower) :
# laissées 0/1, coefficient = effet de 0 → 1 directement.
FEATURES = (
    "team_gold_diff_15",
    "team_cc_per_min",
    "team_vision_per_min",
    "jgl_cs_diff_15",
    "top_dmg_per_gold",
    "jgl_dmg_per_gold",
    "mid_dmg_per_gold",
    "bot_dmg_per_gold",
    "sup_dmg_per_gold",
    "herald_taken",
    "soul_taken",
    "first_tower",
)
CONTINUOUS = frozenset(FEATURES) - {"herald_taken", "soul_taken", "first_tower"}
DEFAULT_MIN_ROWS = 200  # sous ce seuil, l'ajustement est trop instable pour être publié

# Colonnes dmg_per_gold par rôle, dérivées par pivot (FILTER) d'une seule
# ligne match_role_stats par (match, équipe) — évite un GROUP BY par rôle.
_ROLE_DMG_PER_GOLD_SQL = ", ".join(
    f"max(dmg_per_gold) FILTER (WHERE role = '{riot_role}') AS {feature}"
    for riot_role, feature in (
        ("TOP", "top_dmg_per_gold"),
        ("JUNGLE", "jgl_dmg_per_gold"),
        ("MIDDLE", "mid_dmg_per_gold"),
        ("BOTTOM", "bot_dmg_per_gold"),
        ("UTILITY", "sup_dmg_per_gold"),
    )
)


def _fetch_rows(conn: psycopg.Connection, patches: list[str], *, behind_only: bool) -> list[dict]:
    dpg_not_null = " AND ".join(
        f"ta.{f} IS NOT NULL"
        for f in ("top_dmg_per_gold", "jgl_dmg_per_gold", "mid_dmg_per_gold")
        + ("bot_dmg_per_gold", "sup_dmg_per_gold")
    )
    where = f"m.patch = ANY(%(patches)s) AND {dpg_not_null} AND mt.jgl_cs_diff_15 IS NOT NULL"
    if behind_only:
        where += " AND (ta.gold_15 - ea.gold_15) < 0"
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"""
            WITH team_agg AS (
                SELECT match_id, team_id,
                       sum(gold_15) AS gold_15,
                       sum(cc_time_s) AS cc_time_s,
                       sum(vision_score) AS vision_score,
                       {_ROLE_DMG_PER_GOLD_SQL},
                       count(*) AS n_roles
                FROM match_role_stats
                WHERE gold_15 IS NOT NULL
                GROUP BY match_id, team_id
            )
            SELECT m.patch, mt.win,
                   ta.gold_15 - ea.gold_15 AS team_gold_diff_15,
                   ta.cc_time_s / (m.game_duration_s / 60.0) AS team_cc_per_min,
                   ta.vision_score / (m.game_duration_s / 60.0) AS team_vision_per_min,
                   ta.top_dmg_per_gold, ta.jgl_dmg_per_gold, ta.mid_dmg_per_gold,
                   ta.bot_dmg_per_gold, ta.sup_dmg_per_gold,
                   mt.jgl_cs_diff_15, mt.herald_taken, mt.soul_taken, mt.first_tower
            FROM team_agg ta
            JOIN team_agg ea ON ea.match_id = ta.match_id AND ea.team_id <> ta.team_id
            JOIN matches m ON m.match_id = ta.match_id
            JOIN match_trio_stats mt ON mt.match_id = ta.match_id AND mt.team_id = ta.team_id
            WHERE ta.n_roles = 5 AND ea.n_roles = 5 AND {where}
            """,  # noqa: S608 — `where`/`_ROLE_DMG_PER_GOLD_SQL` sont des listes blanches fixes
            {"patches": patches},
        )
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
        stds[f] = math.sqrt(var) or 1.0
    return means, stds


def _design_matrix(
    rows: list[dict], means: dict[str, float], stds: dict[str, float], weights: dict[str, float]
) -> tuple[list[list[float]], list[float], list[float]]:
    x_rows: list[list[float]] = []
    y: list[float] = []
    row_weights: list[float] = []
    for r in rows:
        x_row = [1.0]  # intercept
        for f in FEATURES:
            v = float(r[f])
            if f in CONTINUOUS:
                v = (v - means[f]) / stds[f]
            x_row.append(v)
        x_rows.append(x_row)
        y.append(1.0 if r["win"] else 0.0)
        row_weights.append(weights.get(r["patch"], 0.0))
    return x_rows, y, row_weights


def _solve(matrix: list[list[float]], b: list[float]) -> list[float]:
    """Résolution par élimination de Gauss avec pivot partiel — matrice p×p,
    p ≈ 12 (intercept + FEATURES) : trivial en pure Python à cette taille."""
    n = len(b)
    aug = [row[:] + [b[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= pv
        for r in range(n):
            if r != col and aug[r][col] != 0.0:
                factor = aug[r][col]
                for j in range(col, n + 1):
                    aug[r][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]


def _fit_logistic_irls(
    x_rows: list[list[float]],
    y: list[float],
    row_weights: list[float],
    *,
    n_iter: int = 25,
    ridge: float = 1e-6,
) -> list[float]:
    """Newton-Raphson (IRLS) pondéré : poids d'observation `row_weights` (poids
    de patch de la fenêtre) multiplié au poids IRLS standard μ(1-μ) — pas une
    repondération a posteriori, un vrai ajustement pondéré."""
    n, p = len(x_rows), len(x_rows[0])
    beta = [0.0] * p
    for _ in range(n_iter):
        xtwx = [[0.0] * p for _ in range(p)]
        xtwz = [0.0] * p
        for i in range(n):
            xi = x_rows[i]
            eta = max(min(sum(b * x for b, x in zip(beta, xi, strict=True)), 30.0), -30.0)
            mu = 1.0 / (1.0 + math.exp(-eta))
            variance = max(mu * (1.0 - mu), 1e-6)
            z = eta + (y[i] - mu) / variance
            w = row_weights[i] * variance
            for a in range(p):
                wxa = w * xi[a]
                xtwz[a] += wxa * z
                for b_ in range(p):
                    xtwx[a][b_] += wxa * xi[b_]
        for a in range(p):
            xtwx[a][a] += ridge
        beta_new = _solve(xtwx, xtwz)
        diff = max(abs(beta_new[i] - beta[i]) for i in range(p))
        beta = beta_new
        if diff < 1e-7:
            break
    return beta


def _fit_population(
    conn: psycopg.Connection, window: PatchWindow, *, behind_only: bool, min_rows: int
) -> list[dict] | None:
    rows = _fetch_rows(conn, list(window.patches), behind_only=behind_only)
    if len(rows) < min_rows:
        logger.info(
            "win_factors %s (behind_only=%s) : %d lignes < seuil %d, ignoré",
            window.label,
            behind_only,
            len(rows),
            min_rows,
        )
        return None
    means, stds = _standardize(rows)
    weights = window.weights_for(())  # pas de coupure de rework : aucune variable par champion
    x_rows, y, row_weights = _design_matrix(rows, means, stds, weights)
    beta = _fit_logistic_irls(x_rows, y, row_weights)
    population = "behind_gold15" if behind_only else "all"
    return [
        {
            "window_label": window.label,
            "population": population,
            "feature": name,
            "coef": coef,
            "odds_ratio": math.exp(coef),
            "n": len(rows),
        }
        for name, coef in zip(("intercept", *FEATURES), beta, strict=True)
    ]


_INSERT_SQL = """
    INSERT INTO score_win_factors (window_label, population, feature, coef, odds_ratio, n)
    VALUES (%(window_label)s, %(population)s, %(feature)s, %(coef)s, %(odds_ratio)s, %(n)s)
"""


def refresh(
    window: PatchWindow, *, dsn: str | None = None, min_rows: int = DEFAULT_MIN_ROWS
) -> dict[str, int]:
    """Ajuste les 2 régressions (population complète, derrière au gold@15) et
    matérialise dans `score_win_factors`. Retourne le nombre de lignes écrites
    par population (0 si sous le seuil `min_rows`, pas d'erreur).

    DELETE + INSERT (pas UPSERT) : la table est minuscule (~2×13 lignes par
    fenêtre) et `FEATURES` change parfois d'une session à l'autre (ex. ajout
    de jgl_cs_diff_15) — un UPSERT laisserait les anciennes features orphelines
    en base indéfiniment, contrairement à score_duo/score_trio où l'UPSERT
    évite un vrai problème de volumétrie (cf. mémoire supabase-disk-growth) :
    ce n'est pas la même échelle, DELETE+INSERT est plus simple et plus sûr ici.
    """
    counts: dict[str, int] = {}
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        rows_to_write: list[dict] = []
        for behind_only in (False, True):
            fitted = _fit_population(conn, window, behind_only=behind_only, min_rows=min_rows)
            population = "behind_gold15" if behind_only else "all"
            counts[population] = len(fitted) if fitted else 0
            if fitted:
                rows_to_write.extend(fitted)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM score_win_factors WHERE window_label = %s", (window.label,))
            if rows_to_write:
                cur.executemany(_INSERT_SQL, rows_to_write)
    logger.info("win_factors fenêtre %s rafraîchis : %s", window.label, counts)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.synergy.win_factors", description=__doc__)
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
