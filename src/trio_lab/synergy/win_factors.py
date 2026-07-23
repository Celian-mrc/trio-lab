"""Régression logistique multi-variables : qu'est-ce qui fait gagner ?

Matérialise l'analyse menée en session (2026-07-19), productionisée : IRLS —
Newton-Raphson pour la logistique — pure Python, cohérent avec la philosophie
du projet, pas de numpy/scipy pour un ajustement à ~12 variables sur
quelques dizaines de milliers de lignes.

`herald_taken`/`soul_taken`/`first_tower` RETIRÉS des features (2026-07-24,
retour utilisateur + audit) : ce sont des résultats DE FIN DE PARTIE (l'âme
de dragon n'arrive quasiment jamais avant 25-30 min), pas bornés à 15 min —
`gold_factors.py` les excluait déjà pour cette raison précise ("un héraut
pris à 22 min ne peut pas causer un avantage mesuré à 15 min"), `win_factors`
ne s'appliquait pas la même règle à lui-même. Mesuré avant/après sur la
fenêtre 16.14+16.13 : AUC en échantillon 0,852 avec ces 3 variables, 0,822
sans — la baisse est modeste (beaucoup du signal de `soul_taken` recoupe déjà
`team_gold_diff_15`, une équipe qui prend l'âme était généralement déjà
menante au gold), mais le principe reste le même que `gold_factors` : un
facteur "qui fait gagner à 15 min" ne doit pas être en partie la victoire
déjà actée.

AUC hors-échantillon (`_auc_test`, ligne de diagnostic comme
`gold_factors._r2_*`) : jamais calculé avant 2026-07-24 — l'audit qui a mené
à retirer les 3 variables ci-dessus a aussi révélé qu'aucun AUC n'existait
nulle part dans le module. Un AUC calculé sur les données ayant servi à
l'ajustement serait optimiste par construction (le modèle "a déjà vu" ces
games) ; `_fit_diagnostic_auc` évite ça avec un vrai split déterministe 80/20
par hash du `match_id` (jamais les 2 perspectives d'un même match dans des
ensembles différents) : coefficients ajustés sur le train (80 %), AUC mesuré
sur le test jamais vu (20 %). Les coefficients SERVIS aux utilisateurs sont
ajustés séparément sur 100 % des données (précision maximale) — l'ajustement
train-only ne sert qu'au diagnostic, jamais affiché comme coefficient.

Stats d'ÉQUIPE COMPLÈTE (5 rôles), pas seulement jgl/mid/sup : contrairement
à la première version (qui lisait `match_trio_stats`, limité au trio),
`_fetch_rows` agrège `match_role_stats` (les 5 rôles) par équipe — un coach
raisonne en gold/vision/CC d'ÉQUIPE, pas d'un sous-ensemble de 3 joueurs sur
5 (retour utilisateur, 2026-07-19). `damage_share` et `kill_participation`
(spécifiques au trio, pas de sens au niveau équipe complète — la part de
l'équipe dans les dégâts de l'équipe vaut toujours 100 %) sont retirées ;
`jgl_cs_diff_15` (déjà calculé, team-level malgré son nom) est ajoutée.

`dégâts/gold` par rôle RETIRÉ (retour utilisateur 2026-07-19 + audit
méthodologique en 2 temps, sources citées dans docs/ROADMAP.md) : ce ratio
reflète surtout l'archétype du champion (un tank/support a un dégâts/gold
structurellement bas — ce n'est pas une contre-performance) plutôt qu'un
signal de performance actionnable par un coach ; remplacer par un dégâts/
minute aurait été pire (redondant avec l'avantage gold déjà dans le modèle).
Diagnostic de colinéarité (VIF par régression auxiliaire, `_linalg.compute_vif`,
partagé avec `gold_factors.py`)
calculé à chaque ajustement sur les features continues restantes, loggé
(jamais bloquant) ; `ridge` de l'IRLS augmenté automatiquement si un VIF
dépasse 5 (stabilise les coefficients sans les mettre à zéro, cf. littérature
sur les groupes de régresseurs corrélés).

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
import zlib

import psycopg

from trio_lab import config, db
from trio_lab.synergy import _linalg
from trio_lab.synergy.windows import PatchWindow, make_window

logger = logging.getLogger(__name__)

# Continues : standardisées (z-score) avant ajustement, coefficient lisible
# comme « effet pour +1 écart-type ». Toutes bornées à 15 min (cf. docstring
# module) : plus de variable booléenne de fin de partie depuis le 2026-07-24.
FEATURES = (
    "team_gold_diff_15",
    "team_cc_per_min",
    "team_vision_per_min",
    "jgl_cs_diff_15",
)
CONTINUOUS = frozenset(FEATURES)
DEFAULT_MIN_ROWS = 200  # sous ce seuil, l'ajustement est trop instable pour être publié
_TEST_SPLIT_MOD = 5  # 1 match sur 5 (hash déterministe du match_id) → test, jamais l'ajustement


def _fetch_rows(conn: psycopg.Connection, patches: list[str], *, behind_only: bool) -> list[dict]:
    where = "m.patch = ANY(%(patches)s) AND mt.jgl_cs_diff_15 IS NOT NULL"
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
                       count(*) AS n_roles
                FROM match_role_stats
                WHERE gold_15 IS NOT NULL
                GROUP BY match_id, team_id
            )
            SELECT ta.match_id, m.patch, mt.win,
                   ta.gold_15 - ea.gold_15 AS team_gold_diff_15,
                   ta.cc_time_s / (m.game_duration_s / 60.0) AS team_cc_per_min,
                   ta.vision_score / (m.game_duration_s / 60.0) AS team_vision_per_min,
                   mt.jgl_cs_diff_15
            FROM team_agg ta
            JOIN team_agg ea ON ea.match_id = ta.match_id AND ea.team_id <> ta.team_id
            JOIN matches m ON m.match_id = ta.match_id
            JOIN match_trio_stats mt ON mt.match_id = ta.match_id AND mt.team_id = ta.team_id
            WHERE ta.n_roles = 5 AND ea.n_roles = 5 AND {where}
            """,  # noqa: S608 — `where` est construit depuis des listes blanches fixes
            {"patches": patches},
        )
        return cur.fetchall()


def _is_test_match(match_id: str) -> bool:
    """Split déterministe 80/20 par hash du `match_id` (crc32, stable — pas
    `hash()` : randomisé par process via PYTHONHASHSEED) — les 2 lignes
    d'un même match (une par équipe) tombent toujours du même côté."""
    return zlib.crc32(match_id.encode()) % _TEST_SPLIT_MOD == 0


def _predict(beta: list[float], x_row: list[float]) -> float:
    eta = max(min(sum(b * x for b, x in zip(beta, x_row, strict=True)), 30.0), -30.0)
    return 1.0 / (1.0 + math.exp(-eta))


def _auc(y_true: list[float], y_score: list[float]) -> float | None:
    """AUC (statistique de Mann-Whitney) : probabilité qu'une game gagnée
    tirée au hasard ait un score prédit plus haut qu'une game perdue tirée
    au hasard. Gère les ex-aequo par rang moyen. `None` si une seule classe
    est présente dans `y_true` (pas de comparaison possible)."""
    n = len(y_true)
    order = sorted(range(n), key=lambda i: y_score[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and y_score[order[j + 1]] == y_score[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    n_pos = sum(1 for y in y_true if y == 1.0)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    sum_rank_pos = sum(r for r, y in zip(ranks, y_true, strict=True) if y == 1.0)
    return (sum_rank_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


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
        beta_new = _linalg.solve(xtwx, xtwz)
        diff = max(abs(beta_new[i] - beta[i]) for i in range(p))
        beta = beta_new
        if diff < 1e-7:
            break
    return beta


def _fit_diagnostic_auc(
    rows: list[dict], weights: dict[str, float], *, min_rows: int
) -> tuple[float | None, int]:
    """AUC hors-échantillon : ajustement sur le train (80 %), évaluation sur
    le test (20 %) jamais vu — split déterministe par `match_id`, jamais
    utilisé pour les coefficients SERVIS (ajustés à part sur 100 % des
    données, cf. `_fit_population`). Retourne `(None, 0)` si le train est
    trop petit ou si le test tombe sur une seule classe (games toutes
    gagnées ou toutes perdues, l'AUC n'a pas de sens) — jamais une erreur."""
    train_rows = [r for r in rows if not _is_test_match(r["match_id"])]
    test_rows = [r for r in rows if _is_test_match(r["match_id"])]
    if len(train_rows) < min_rows or not test_rows:
        return None, 0
    train_means, train_stds = _standardize(train_rows)
    train_x, train_y, train_w = _design_matrix(train_rows, train_means, train_stds, weights)
    train_beta = _fit_logistic_irls(train_x, train_y, train_w, ridge=_linalg.DEFAULT_RIDGE)
    test_x, test_y, _ = _design_matrix(test_rows, train_means, train_stds, weights)
    test_scores = [_predict(train_beta, x) for x in test_x]
    return _auc(test_y, test_scores), len(test_rows)


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
    population = "behind_gold15" if behind_only else "all"
    weights = window.weights_for(())  # pas de coupure de rework : aucune variable par champion

    auc, auc_n = _fit_diagnostic_auc(rows, weights, min_rows=min_rows)
    if auc is None:
        logger.info(
            "win_factors %s (%s) : AUC hors-échantillon non calculable", window.label, population
        )
    else:
        logger.info(
            "win_factors %s (%s) : AUC hors-échantillon %.3f (n=%d)",
            window.label,
            population,
            auc,
            auc_n,
        )

    # Coefficients SERVIS : ajustés sur 100 % des données (précision maximale),
    # séparément du split train/test ci-dessus qui ne sert qu'au diagnostic.
    means, stds = _standardize(rows)
    x_rows, y, row_weights = _design_matrix(rows, means, stds, weights)

    continuous_cols = [i + 1 for i, f in enumerate(FEATURES) if f in CONTINUOUS]
    vifs = _linalg.compute_vif(x_rows, continuous_cols)
    max_vif = max(vifs.values(), default=0.0)
    if max_vif > _linalg.VIF_ALERT_THRESHOLD:
        offenders = {
            FEATURES[c - 1]: round(v, 1) for c, v in vifs.items() if v > _linalg.VIF_ALERT_THRESHOLD
        }
        ridge = _linalg.VIF_RIDGE
        logger.warning(
            "win_factors %s (%s) : VIF > %.0f détecté %s, ridge renforcé à %.1f",
            window.label,
            population,
            _linalg.VIF_ALERT_THRESHOLD,
            offenders,
            ridge,
        )
    else:
        ridge = _linalg.DEFAULT_RIDGE
        logger.info(
            "win_factors %s (%s) : VIF max %.2f (sous le seuil %.0f)",
            window.label,
            population,
            max_vif,
            _linalg.VIF_ALERT_THRESHOLD,
        )

    beta = _fit_logistic_irls(x_rows, y, row_weights, ridge=ridge)
    feature_rows = [
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
    if auc is None:
        return feature_rows
    diagnostic_row = {
        "window_label": window.label,
        "population": population,
        "feature": "_auc_test",
        "coef": auc,
        "odds_ratio": auc,  # sans signification, juste pour satisfaire la contrainte NOT NULL
        "n": auc_n,
    }
    return [*feature_rows, diagnostic_row]


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
        # Le timeout par défaut du rôle applicatif est trop court pour la
        # requête d'agrégation (jointure team_agg × 2 sur des centaines de
        # milliers de games) — même piège déjà rencontré et corrigé dans
        # `stats.aggregate.refresh`. `enable_nestloop = off` contourne un
        # vrai bug de planification Postgres constaté le 20/07/2026 : le
        # filtre `n_roles = 5` porte sur une colonne dérivée d'un agrégat
        # (count(*) dans la CTE team_agg), que Postgres ne sait pas estimer
        # correctement (retombe sur une sélectivité par défaut ~0,5 % —
        # 292 lignes estimées sur ~58 000 réelles) ; sur cette sous-estimation
        # il choisit une Nested Loop pour l'auto-jointure de team_agg
        # (jusqu'à des milliards de comparaisons), 10+ min au lieu de ~3s en
        # Hash Join. `ANALYZE` ne corrige pas ce cas (pas de vraies stats
        # possibles sur une expression agrégée) — contournement stable tant
        # que la requête n'est pas restructurée pour l'éviter autrement.
        conn.execute("SET LOCAL statement_timeout = '5min'")
        conn.execute("SET LOCAL enable_nestloop = off")
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
