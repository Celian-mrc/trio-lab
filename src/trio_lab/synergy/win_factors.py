"""Régression logistique multi-variables : qu'est-ce qui fait gagner ?

Matérialise l'analyse menée en session (2026-07-19) sur match_trio_stats,
productionisée : mêmes features, même méthode (IRLS — Newton-Raphson pour la
logistique — pure Python, cohérent avec la philosophie du projet, pas de
numpy/scipy pour un ajustement à ~10 variables sur quelques dizaines de
milliers de lignes).

Deux populations, deux jeux de coefficients :
- « all » : toutes les games (patch_window courant, cas complets).
- « behind_gold15 » : games où le trio est derrière au gold à 15 min —
  leviers de comeback, mesurés différemment de la population complète
  (vision/efficacité ressources y pèsent 2-3x plus, cf. recherche session).

Poids par patch : mêmes poids que `synergy.compute`/`windows.PatchWindow`
(pas de coupure de rework — aucune variable ici n'est liée à un champion
précis), appliqués comme poids d'observation dans l'IRLS (`w_i = poids_patch
× μ(1-μ)`), pas seulement une repondération a posteriori.

Rafraîchissement MANUEL (`python -m trio_lab.synergy.win_factors`), jamais
dans le cycle service : un facteur de victoire est un signal de patch, pas de
cycle de collecte — même philosophie que `ccref.sync_theoretical`.
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
    "gold_diff_15",
    "cc_per_min",
    "vision_per_min",
    "damage_share",
    "jgl_dmg_per_gold",
    "mid_dmg_per_gold",
    "sup_dmg_per_gold",
    "kill_participation_pre15",
    "herald_taken",
    "soul_taken",
    "first_tower",
)
CONTINUOUS = frozenset(FEATURES) - {"herald_taken", "soul_taken", "first_tower"}
DEFAULT_MIN_ROWS = 200  # sous ce seuil, l'ajustement est trop instable pour être publié


def _fetch_rows(conn: psycopg.Connection, patches: list[str], *, behind_only: bool) -> list[dict]:
    where = (
        "t.gold_diff_15 IS NOT NULL AND t.jgl_dmg_per_gold IS NOT NULL"
        " AND t.mid_dmg_per_gold IS NOT NULL AND t.sup_dmg_per_gold IS NOT NULL"
        " AND t.damage_share IS NOT NULL AND t.kill_participation_pre15 IS NOT NULL"
        " AND m.patch = ANY(%(patches)s)"
    )
    if behind_only:
        where += " AND t.gold_diff_15 < 0"
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"""
            SELECT m.patch, t.win,
                   t.gold_diff_15,
                   t.cc_time_s / (m.game_duration_s / 60.0) AS cc_per_min,
                   t.vision_score / (m.game_duration_s / 60.0) AS vision_per_min,
                   t.damage_share,
                   t.jgl_dmg_per_gold, t.mid_dmg_per_gold, t.sup_dmg_per_gold,
                   t.kill_participation_pre15,
                   t.herald_taken, t.soul_taken, t.first_tower
            FROM match_trio_stats t
            JOIN matches m USING (match_id)
            WHERE {where}
            """,  # noqa: S608 — `where` est une liste blanche fixe, jamais interpolée
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
    ON CONFLICT (window_label, population, feature)
    DO UPDATE SET coef = EXCLUDED.coef, odds_ratio = EXCLUDED.odds_ratio, n = EXCLUDED.n,
                  computed_at = now()
"""


def refresh(
    window: PatchWindow, *, dsn: str | None = None, min_rows: int = DEFAULT_MIN_ROWS
) -> dict[str, int]:
    """Ajuste les 2 régressions (population complète, derrière au gold@15) et
    matérialise dans `score_win_factors`. Retourne le nombre de lignes écrites
    par population (0 si sous le seuil `min_rows`, pas d'erreur)."""
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
