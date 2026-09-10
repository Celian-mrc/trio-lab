"""Construction des features de prédiction de winrate au DRAFT (jgl/mid/sup).

Contrairement à `synergy/win_factors.py`/`gold_factors.py` (état de la game à
15 min : gold, CC, vision...), les features ici sont toutes disponibles AU
DRAFT, avant que la partie commence : winrate individuel des champions,
synergie de duo/trio, delta de matchup par rôle, ET (2026-09-09, retour
utilisateur) tous les axes déjà matérialisés dans `agg_trio`/`score_trio`
pour `draft_suggestions.py` — gold@5/10/15 du trio, gold@15 d'ÉQUIPE, vision,
objectifs (drakes/âme/héraut/1re tour), CC par rôle, scaling, portée
théorique. Aucune donnée in-game réelle du match prédit lui-même.

Anti-fuite (« leave-one-patch-out ») : les baselines (`agg_champion`,
`agg_duo`, `agg_trio`, `agg_matchup`, `agg_trio_duration`) sont calculées
pour chaque match en EXCLUANT le patch de ce match, jamais sur l'historique
complet — sinon une combinaison à faible volume (parfois 1-2 games)
intégrerait en partie le résultat même de la partie qu'on essaie de
prédire. Sur une fenêtre à 3 patchs, une feature pour un match du patch P
vient donc uniquement des 2 AUTRES patchs de la fenêtre.

Conséquence assumée : les combinaisons qui n'existent que sur UN SEUL patch
de la fenêtre (sortie très récente, rework, pick exotique à 1 game) n'ont
aucune baseline "autre patch" disponible pour LEURS PROPRES matchs et sont
exclues (même principe que `win_factors`/`gold_factors` : cas complets
uniquement) — sauf `scaling`, qui a un seuil de données minimum bien plus
élevé par construction (≥3 tranches de durée à ≥3 games chacune, cf.
`synergy/compute.py::SCALING_MIN_*`) et serait presque toujours absent en
leave-one-patch-out si on exigeait sa présence : imputé à 0.0 (« pas de
signal de scaling disponible ») plutôt que de faire perdre la majorité des
lignes pour cette seule feature — seule exception à la politique
« cas complets uniquement » du reste du module.

Seuil de fiabilité minimum : ESSAYÉ puis ABANDONNÉ (retour utilisateur
2026-09-09/10). Exiger un nombre minimum de games (`MIN_GAMES`, repris de
`scores.DEFAULT_TIER_THRESHOLDS`, la borne "faible"/"moyen" déjà affichée
ailleurs sur le site) avant d'accepter une valeur, plutôt que `> 0` comme
avant — l'intuition étant qu'un combo à 2-3 games donne un winrate de 0 %
ou 100 %, du bruit traité à égalité avec un combo à 500 games. Mesuré :
une grille `MIN_GAMES` ∈ {1, 10, 20, 30, 50} sur le feature set 5 rôles
montre une dégradation MONOTONE de l'AUC à mesure que le seuil augmente
(0,553-0,555 à 1, jusqu'à 0,535-0,542 à 50), sur les 3 modèles sans
exception — pas de "sweet spot" au milieu. Le volume gagne systématiquement
contre la fiabilité individuelle : avec un signal déjà très faible partout,
plus d'observations (même bruitées) aident le modèle à moyenner ce bruit
plus que ce que le filtrage supprime en écartant les pires cas. Politique
`> 0` restaurée. Un lissage bayésien (tirer un combo peu fiable vers un
prior au lieu de l'exclure, comme `score_trio`/`score_duo` le font pour
l'affichage) reste une piste plus fine non testée, si le sujet est
reconsidéré.

Périmètre délibérément choisi au grain TRIO pour les nouveaux axes bruts
(gold/vision/objectifs/CC), pas dupliqué au grain duo : ce sont des stats
d'ÉQUIPE (même grain que `match_trio_stats`), les dupliquer par duo
ajouterait surtout de la colinéarité (un duo n'est qu'un sous-ensemble du
trio) plutôt qu'un vrai signal indépendant — contrairement à `duo_synergy`
(winrate), qui isole spécifiquement l'interaction de paire.
"""

from __future__ import annotations

import logging

import psycopg

from trio_lab.synergy import scores

logger = logging.getLogger(__name__)

FEATURE_NAMES = (
    "us_avg_champion_wr",
    "them_avg_champion_wr",
    "us_avg_duo_synergy",
    "them_avg_duo_synergy",
    "us_trio_wr",
    "them_trio_wr",
    "matchup_delta_jgl",
    "matchup_delta_mid",
    "matchup_delta_sup",
    "us_trio_gold5",
    "them_trio_gold5",
    "us_trio_gold10",
    "them_trio_gold10",
    "us_trio_gold15",
    "them_trio_gold15",
    "us_team_gold15",
    "them_team_gold15",
    "us_trio_vision",
    "them_trio_vision",
    "us_trio_drakes",
    "them_trio_drakes",
    "us_trio_soul",
    "them_trio_soul",
    "us_trio_herald",
    "them_trio_herald",
    "us_trio_tower1",
    "them_trio_tower1",
    "us_trio_cc",
    "them_trio_cc",
    "us_jgl_cc",
    "them_jgl_cc",
    "us_mid_cc",
    "them_mid_cc",
    "us_sup_cc",
    "them_sup_cc",
    "us_avg_range",
    "them_avg_range",
    "us_trio_scaling",
    "them_trio_scaling",
)

# jgl/mid/sup (identité du projet) plutôt que les 5 rôles : mêmes rôles que
# `match_trio_stats`, cohérent avec le reste de `synergy/`. `top`/`bot`
# ajoutés (retour utilisateur 2026-09-10) pour `build_five_role_feature_table`
# uniquement — `agg_champion`/`agg_matchup` couvrent déjà ces 2 rôles, aucune
# nouvelle donnée à collecter.
_ROLE_OF = {"jgl": "JUNGLE", "mid": "MIDDLE", "sup": "UTILITY", "top": "TOP", "bot": "BOTTOM"}
_DUO_PAIRS = (("jgl_mid", "jgl", "mid"), ("jgl_sup", "jgl", "sup"), ("mid_sup", "mid", "sup"))

# Métrique -> (colonne somme, colonne n) dans `agg_trio` — une seule requête
# groupée récupère les 13 à la fois plutôt que 13 requêtes séparées.
_TRIO_STAT_COLUMNS = {
    "gold5": ("gold5_sum", "gold5_n"),
    "gold10": ("gold10_sum", "gold10_n"),
    "gold15": ("gold15_sum", "gold15_n"),
    "team_gold15": ("team_gold15_sum", "team_gold15_n"),
    "vision": ("vision_sum", "vision_n"),
    "drakes": ("drakes_sum", "drakes_n"),
    "soul": ("soul_sum", "soul_n"),
    "herald": ("herald_sum", "herald_n"),
    "tower1": ("tower1_sum", "tower1_n"),
    "cc": ("cc_sum", "cc_n"),
    "jgl_cc": ("jgl_cc_sum", "jgl_cc_n"),
    "mid_cc": ("mid_cc_sum", "mid_cc_n"),
    "sup_cc": ("sup_cc_sum", "sup_cc_n"),
}

_SCALING_MIN_BUCKET_GAMES = 3  # même seuil que synergy/compute.py::SCALING_MIN_BUCKET_GAMES
_SCALING_MIN_BUCKETS = 3  # même seuil que synergy/compute.py::SCALING_MIN_BUCKETS

AggKey = tuple  # (patch, ...) — juste pour la lisibilité des signatures
GamesWins = tuple[int, int]


def _fetch_match_rows(conn: psycopg.Connection, patches: list[str]) -> list[dict]:
    """Une ligne par (match, équipe) — les 2 perspectives d'un même match,
    comme `win_factors._fetch_rows` (même raison : le split train/test doit
    garder les 2 lignes d'un match ensemble, cf. `_is_test_match`)."""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT us.match_id, m.patch, us.win,
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
        return cur.fetchall()


def _fetch_agg_champion(conn: psycopg.Connection, patches: list[str]) -> dict:
    """Clé (patch, role, champion_id) — `role` au format Riot (JUNGLE...),
    sommé sur toutes les plateformes (pas de branche par région, CLAUDE.md #6,
    même logique que `gold_factors._BASELINE_SQL`)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT patch, role, champion_id, sum(games), sum(wins)
            FROM agg_champion WHERE patch = ANY(%s)
            GROUP BY patch, role, champion_id
            """,
            (patches,),
        )
        return {(patch, role, champ): (games, wins) for patch, role, champ, games, wins in cur}


def _fetch_agg_duo(conn: psycopg.Connection, patches: list[str]) -> dict:
    """Clé (patch, roles, champ_a, champ_b) — `roles` ∈ jgl_mid/jgl_sup/mid_sup,
    même orientation champ_a/champ_b que `stats.aggregate` (cf. commentaire
    module)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT patch, roles, champ_a, champ_b, sum(games), sum(wins)
            FROM agg_duo WHERE patch = ANY(%s)
            GROUP BY patch, roles, champ_a, champ_b
            """,
            (patches,),
        )
        return {(patch, roles, a, b): (games, wins) for patch, roles, a, b, games, wins in cur}


def _fetch_agg_trio(conn: psycopg.Connection, patches: list[str]) -> dict:
    """Clé (patch, jgl, mid, sup) -> (games, wins)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT patch, jgl_champion, mid_champion, sup_champion, sum(games), sum(wins)
            FROM agg_trio WHERE patch = ANY(%s)
            GROUP BY patch, jgl_champion, mid_champion, sup_champion
            """,
            (patches,),
        )
        return {(patch, jgl, mid, sup): (games, wins) for patch, jgl, mid, sup, games, wins in cur}


def _fetch_agg_matchup(conn: psycopg.Connection, patches: list[str]) -> dict:
    """Clé (patch, role, champ_a, champ_b) — `wins` = victoires DE champ_a,
    table symétrique (les 2 sens existent), cf. `stats.aggregate._MATCHUP_SQL`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT patch, role, champ_a, champ_b, sum(games), sum(wins)
            FROM agg_matchup WHERE patch = ANY(%s)
            GROUP BY patch, role, champ_a, champ_b
            """,
            (patches,),
        )
        return {(patch, role, a, b): (games, wins) for patch, role, a, b, games, wins in cur}


def _fetch_agg_trio_stats(conn: psycopg.Connection, patches: list[str]) -> dict:
    """Clé (patch, jgl, mid, sup) -> {métrique: (sum, n)}, pour les 13 axes de
    `_TRIO_STAT_COLUMNS` — une seule requête groupée (13 `sum()`) plutôt que
    13 aller-retours réseau séparés."""
    # COALESCE(..., 0) : sum() renvoie NULL (pas 0) quand TOUTES les valeurs
    # sous-jacentes d'un groupe sont nulles — plusieurs colonnes de
    # `match_trio_stats` sont nullables (`herald_taken`, `gold_diff_5`...,
    # cf. migration 001), trouvé en pratique via un crash `Decimal + None`
    # sur un combo où un axe entier n'avait jamais de donnée exploitable.
    sum_exprs = ", ".join(
        f"coalesce(sum({sum_col}), 0), coalesce(sum({n_col}), 0)"
        for sum_col, n_col in _TRIO_STAT_COLUMNS.values()
    )
    metric_names = list(_TRIO_STAT_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT patch, jgl_champion, mid_champion, sup_champion, {sum_exprs}
            FROM agg_trio WHERE patch = ANY(%s)
            GROUP BY patch, jgl_champion, mid_champion, sup_champion
            """,  # noqa: S608 — colonnes fixes de `_TRIO_STAT_COLUMNS`, pas d'entrée utilisateur
            (patches,),
        )
        result: dict = {}
        for row in cur:
            patch, jgl, mid, sup, *sums = row
            stats = {
                metric_names[i]: (sums[2 * i], sums[2 * i + 1]) for i in range(len(metric_names))
            }
            result[(patch, jgl, mid, sup)] = stats
        return result


def _fetch_range_theoretical(conn: psycopg.Connection) -> dict[int, float]:
    """Clé champion_id -> score de portée théorique — statique (propriété du
    champion, pas dérivée de résultats de matchs), aucun risque de fuite,
    donc pas de leave-one-patch-out ici. Score BRUT (pas normalisé 0-100
    comme dans `score_trio.range_theoretical_pct`) : une transformation
    monotone ne change rien à ce qu'un modèle peut en apprendre."""
    with conn.cursor() as cur:
        cur.execute("SELECT champion_id, score FROM champion_range_theoretical")
        return dict(cur.fetchall())


def _fetch_agg_trio_duration(conn: psycopg.Connection, patches: list[str]) -> dict:
    """Clé (jgl, mid, sup) -> {duration_bucket: [(patch, games, wins), ...]} —
    même structure que `synergy/compute.py::_load_duration_agg` (grouped by
    bucket), pour recalculer la pente de scaling en excluant un patch."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT patch, jgl_champion, mid_champion, sup_champion, duration_bucket,
                   sum(games), sum(wins)
            FROM agg_trio_duration WHERE patch = ANY(%s)
            GROUP BY patch, jgl_champion, mid_champion, sup_champion, duration_bucket
            """,
            (patches,),
        )
        result: dict[tuple, dict[int, list[tuple[str, int, int]]]] = {}
        for patch, jgl, mid, sup, bucket, games, wins in cur:
            result.setdefault((jgl, mid, sup), {}).setdefault(bucket, []).append(
                (patch, games, wins)
            )
        return result


def _sum_excluding(
    table: dict, key_tail: tuple, patches: list[str], exclude_patch: str
) -> GamesWins:
    """Somme `games`/`wins` (ou toute paire sum/n) sur tous les patchs de la
    fenêtre SAUF `exclude_patch`."""
    total_a = total_b = 0
    for patch in patches:
        if patch == exclude_patch:
            continue
        a, b = table.get((patch, *key_tail), (0, 0))
        total_a += a
        total_b += b
    return total_a, total_b


# Politique `> 0` (pas de seuil de fiabilité minimum) — cf. docstring module
# pour l'essai à `MIN_GAMES` fixe, abandonné après mesure (grille 2026-09-10) :
# n'importe quelle valeur non nulle est acceptée, le volume compte plus que
# la fiabilité individuelle sur ces données.
def _wr(games: int, wins: int) -> float | None:
    return wins / games if games > 0 else None


def _avg(total: float, n: int) -> float | None:
    return total / n if n > 0 else None


def _champion_wr(
    agg_champion: dict, patches: list[str], exclude_patch: str, role: str, champ: int
) -> float | None:
    games, wins = _sum_excluding(agg_champion, (_ROLE_OF[role], champ), patches, exclude_patch)
    return _wr(games, wins)


def _duo_synergy(
    agg_duo: dict,
    agg_champion: dict,
    patches: list[str],
    exclude_patch: str,
    roles: str,
    role_a: str,
    role_b: str,
    champ_a: int,
    champ_b: int,
) -> float | None:
    """wr(duo) − moyenne(wr(champ_a), wr(champ_b)) — même définition que
    `score_duo.synergy_raw` mais sans lissage bayésien (cf. docstring module)."""
    games, wins = _sum_excluding(agg_duo, (roles, champ_a, champ_b), patches, exclude_patch)
    duo_wr = _wr(games, wins)
    wr_a = _champion_wr(agg_champion, patches, exclude_patch, role_a, champ_a)
    wr_b = _champion_wr(agg_champion, patches, exclude_patch, role_b, champ_b)
    if duo_wr is None or wr_a is None or wr_b is None:
        return None
    return duo_wr - (wr_a + wr_b) / 2.0


def _trio_wr(
    agg_trio: dict, patches: list[str], exclude_patch: str, jgl: int, mid: int, sup: int
) -> float | None:
    games, wins = _sum_excluding(agg_trio, (jgl, mid, sup), patches, exclude_patch)
    return _wr(games, wins)


def _matchup_delta(
    agg_matchup: dict,
    agg_champion: dict,
    patches: list[str],
    exclude_patch: str,
    role: str,
    my_champ: int,
    enemy_champ: int,
) -> float | None:
    """wr(my_champ vs enemy_champ, même rôle) − wr baseline de my_champ dans ce
    rôle — même définition que `score_matchup.delta_raw`, sans lissage."""
    riot_role = _ROLE_OF[role]
    games, wins = _sum_excluding(
        agg_matchup, (riot_role, my_champ, enemy_champ), patches, exclude_patch
    )
    matchup_wr = _wr(games, wins)
    baseline_wr = _champion_wr(agg_champion, patches, exclude_patch, role, my_champ)
    if matchup_wr is None or baseline_wr is None:
        return None
    return matchup_wr - baseline_wr


def _trio_stat(
    agg_trio_stats: dict,
    patches: list[str],
    exclude_patch: str,
    jgl: int,
    mid: int,
    sup: int,
    metric: str,
) -> float | None:
    """Moyenne leave-one-patch-out d'un axe `_TRIO_STAT_COLUMNS` (gold@X,
    vision, objectifs, CC...) pour un trio donné."""
    total = n = 0
    for patch in patches:
        if patch == exclude_patch:
            continue
        stats = agg_trio_stats.get((patch, jgl, mid, sup))
        if stats is None:
            continue
        t, count = stats[metric]
        total += t
        n += count
    return _avg(total, n)


def _avg_range(range_theoretical: dict[int, float], jgl: int, mid: int, sup: int) -> float | None:
    scores_ = [range_theoretical.get(c) for c in (jgl, mid, sup)]
    return sum(scores_) / 3.0 if all(s is not None for s in scores_) else None


def _scaling_slope_excluding(
    by_bucket: dict[int, list[tuple[str, int, int]]] | None,
    patches: list[str],
    exclude_patch: str,
) -> float | None:
    """Pente WR ~ tranche de durée en excluant `exclude_patch`, réutilisant les
    fonctions pures de `synergy/scores.py` (poids uniforme 1.0 sur les patchs
    inclus — pas de pondération de récence ici, contrairement à
    `PatchWindow.weights_for`, qui n'a pas de sens pour un leave-one-out).
    `None` si sous le seuil minimum (cf. docstring module : imputé à 0.0 par
    l'appelant, pas traité comme les autres axes "cas complets uniquement")."""
    if not by_bucket:
        return None
    weights = {p: 1.0 for p in patches if p != exclude_patch}
    points: list[tuple[float, float, float]] = []
    for bucket, rows in by_bucket.items():
        rows_excl = [(p, g, w) for p, g, w in rows if p != exclude_patch]
        if sum(g for _, g, _ in rows_excl) < _SCALING_MIN_BUCKET_GAMES:
            continue
        wr = scores.weighted_wr(rows_excl, weights)
        if wr is None:
            continue
        points.append((bucket / 5.0, wr.wr, wr.games_eff))
    if len(points) < _SCALING_MIN_BUCKETS:
        return None
    slope = scores.weighted_slope_ci(points)
    return slope.slope if slope else None


def _row_features(
    row: dict,
    agg_champion: dict,
    agg_duo: dict,
    agg_trio: dict,
    agg_matchup: dict,
    agg_trio_stats: dict,
    range_theoretical: dict[int, float],
    agg_trio_duration: dict,
    patches: list[str],
) -> list[float] | None:
    """Une ligne de features pour un (match, équipe), ou `None` si une seule
    baseline REQUISE manque (`scaling` est la seule exception, imputée)."""
    patch = row["patch"]
    us = {"jgl": row["us_jgl"], "mid": row["us_mid"], "sup": row["us_sup"]}
    them = {"jgl": row["them_jgl"], "mid": row["them_mid"], "sup": row["them_sup"]}

    def avg_champion_wr(side: dict) -> float | None:
        wrs = [
            _champion_wr(agg_champion, patches, patch, r, side[r]) for r in ("jgl", "mid", "sup")
        ]
        return sum(wrs) / 3.0 if all(w is not None for w in wrs) else None

    def avg_duo_synergy(side: dict) -> float | None:
        synergies = [
            _duo_synergy(agg_duo, agg_champion, patches, patch, roles, ra, rb, side[ra], side[rb])
            for roles, ra, rb in _DUO_PAIRS
        ]
        return sum(synergies) / 3.0 if all(s is not None for s in synergies) else None

    def trio_stat(side: dict, metric: str) -> float | None:
        return _trio_stat(
            agg_trio_stats, patches, patch, side["jgl"], side["mid"], side["sup"], metric
        )

    required: list[float | None] = [
        avg_champion_wr(us),
        avg_champion_wr(them),
        avg_duo_synergy(us),
        avg_duo_synergy(them),
        _trio_wr(agg_trio, patches, patch, us["jgl"], us["mid"], us["sup"]),
        _trio_wr(agg_trio, patches, patch, them["jgl"], them["mid"], them["sup"]),
        *[
            _matchup_delta(agg_matchup, agg_champion, patches, patch, r, us[r], them[r])
            for r in ("jgl", "mid", "sup")
        ],
    ]
    for metric in _TRIO_STAT_COLUMNS:
        required.append(trio_stat(us, metric))
        required.append(trio_stat(them, metric))
    required.append(_avg_range(range_theoretical, us["jgl"], us["mid"], us["sup"]))
    required.append(_avg_range(range_theoretical, them["jgl"], them["mid"], them["sup"]))

    if any(v is None for v in required):
        return None

    # scaling : seule feature imputée à 0.0 plutôt que de faire tomber la ligne
    # (cf. docstring module — seuil de données minimum trop élevé en
    # leave-one-patch-out pour exiger sa présence comme les autres axes).
    us_scaling = _scaling_slope_excluding(
        agg_trio_duration.get((us["jgl"], us["mid"], us["sup"])), patches, patch
    )
    them_scaling = _scaling_slope_excluding(
        agg_trio_duration.get((them["jgl"], them["mid"], them["sup"])), patches, patch
    )
    required.append(0.0 if us_scaling is None else us_scaling)
    required.append(0.0 if them_scaling is None else them_scaling)

    return required


def build_feature_table(
    conn: psycopg.Connection, patches: list[str]
) -> tuple[list[list[float]], list[int], list[str]]:
    """Construit `(X, y, match_ids)` pour la fenêtre `patches` : `X` (features,
    ordre `FEATURE_NAMES`), `y` (1 = victoire), `match_ids` (pour le split
    déterministe train/test, cf. `ml.train._is_test_match`). Lignes incomplètes
    (baseline requise manquante, cf. docstring module) silencieusement
    exclues."""
    match_rows = _fetch_match_rows(conn, patches)
    agg_champion = _fetch_agg_champion(conn, patches)
    agg_duo = _fetch_agg_duo(conn, patches)
    agg_trio = _fetch_agg_trio(conn, patches)
    agg_matchup = _fetch_agg_matchup(conn, patches)
    agg_trio_stats = _fetch_agg_trio_stats(conn, patches)
    range_theoretical = _fetch_range_theoretical(conn)
    agg_trio_duration = _fetch_agg_trio_duration(conn, patches)

    X: list[list[float]] = []
    y: list[int] = []
    match_ids: list[str] = []
    dropped = 0
    for row in match_rows:
        features = _row_features(
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
        if features is None:
            dropped += 1
            continue
        X.append(features)
        y.append(1 if row["win"] else 0)
        match_ids.append(row["match_id"])

    logger.info(
        "features fenêtre %s : %d lignes complètes, %d exclues (baseline manquante)",
        "+".join(patches),
        len(X),
        dropped,
    )
    return X, y, match_ids


# Sous-ensemble réduit (retour utilisateur 2026-09-09, après classement
# d'importance sur le feature set complet) : exactement les 9 features du
# v1 — winrate champion/duo/trio + les 3 deltas de matchup, qui dominent
# nettement le classement (`logistic_regression` ET `random_forest`
# convergent sur ce sous-ensemble) — le bloc gold/vision/objectifs/CC/
# scaling/range ajouté ensuite contribue très peu par comparaison. Moins de
# features REQUISES simultanément = moins de lignes perdues pour
# incomplétude — testé séparément de `build_feature_table` (fetch plus
# léger aussi : pas besoin de `agg_trio_stats`/`range_theoretical`/
# `agg_trio_duration` ici).
REDUCED_FEATURE_NAMES = (
    "us_avg_champion_wr",
    "them_avg_champion_wr",
    "us_avg_duo_synergy",
    "them_avg_duo_synergy",
    "us_trio_wr",
    "them_trio_wr",
    "matchup_delta_jgl",
    "matchup_delta_mid",
    "matchup_delta_sup",
)


def _row_features_reduced(
    row: dict,
    agg_champion: dict,
    agg_duo: dict,
    agg_trio: dict,
    agg_matchup: dict,
    patches: list[str],
) -> list[float] | None:
    patch = row["patch"]
    us = {"jgl": row["us_jgl"], "mid": row["us_mid"], "sup": row["us_sup"]}
    them = {"jgl": row["them_jgl"], "mid": row["them_mid"], "sup": row["them_sup"]}

    def avg_champion_wr(side: dict) -> float | None:
        wrs = [
            _champion_wr(agg_champion, patches, patch, r, side[r]) for r in ("jgl", "mid", "sup")
        ]
        return sum(wrs) / 3.0 if all(w is not None for w in wrs) else None

    def avg_duo_synergy(side: dict) -> float | None:
        synergies = [
            _duo_synergy(agg_duo, agg_champion, patches, patch, roles, ra, rb, side[ra], side[rb])
            for roles, ra, rb in _DUO_PAIRS
        ]
        return sum(synergies) / 3.0 if all(s is not None for s in synergies) else None

    values: list[float | None] = [
        avg_champion_wr(us),
        avg_champion_wr(them),
        avg_duo_synergy(us),
        avg_duo_synergy(them),
        _trio_wr(agg_trio, patches, patch, us["jgl"], us["mid"], us["sup"]),
        _trio_wr(agg_trio, patches, patch, them["jgl"], them["mid"], them["sup"]),
        *[
            _matchup_delta(agg_matchup, agg_champion, patches, patch, r, us[r], them[r])
            for r in ("jgl", "mid", "sup")
        ],
    ]
    if any(v is None for v in values):
        return None
    return values


def build_reduced_feature_table(
    conn: psycopg.Connection, patches: list[str]
) -> tuple[list[list[float]], list[int], list[str]]:
    """Comme `build_feature_table`, mais sur `REDUCED_FEATURE_NAMES`
    uniquement (9 features au lieu de 39, cf. commentaire ci-dessus)."""
    match_rows = _fetch_match_rows(conn, patches)
    agg_champion = _fetch_agg_champion(conn, patches)
    agg_duo = _fetch_agg_duo(conn, patches)
    agg_trio = _fetch_agg_trio(conn, patches)
    agg_matchup = _fetch_agg_matchup(conn, patches)

    X: list[list[float]] = []
    y: list[int] = []
    match_ids: list[str] = []
    dropped = 0
    for row in match_rows:
        row_features = _row_features_reduced(
            row, agg_champion, agg_duo, agg_trio, agg_matchup, patches
        )
        if row_features is None:
            dropped += 1
            continue
        X.append(row_features)
        y.append(1 if row["win"] else 0)
        match_ids.append(row["match_id"])

    logger.info(
        "features réduites fenêtre %s : %d lignes complètes, %d exclues (baseline manquante)",
        "+".join(patches),
        len(X),
        dropped,
    )
    return X, y, match_ids


# Extension à 5 rôles (retour utilisateur 2026-09-10) : `REDUCED_FEATURE_NAMES`
# (les 9 features qui dominaient le classement d'importance) + les 2 lanes
# ignorées jusqu'ici (top/adc) — `agg_champion`/`agg_matchup` couvrent déjà
# TOP/BOTTOM, aucune nouvelle collecte. `matchup_delta_top`/`matchup_delta_bot`
# suivent exactement le même principe que jgl/mid/sup (déjà les features les
# plus importantes du classement) ; `us_top_wr`/`us_bot_wr` (et `them_`)
# séparés plutôt que fondus dans `avg_champion_wr` (qui reste jgl/mid/sup
# pour rester comparable au reste de la série d'expériences).
FIVE_ROLE_FEATURE_NAMES = (
    *REDUCED_FEATURE_NAMES,
    "matchup_delta_top",
    "matchup_delta_bot",
    "us_top_wr",
    "them_top_wr",
    "us_bot_wr",
    "them_bot_wr",
)


def _fetch_match_rows_5roles(conn: psycopg.Connection, patches: list[str]) -> list[dict]:
    """Comme `_fetch_match_rows`, plus les champions top/adc de chaque équipe
    (`match_participants`, qui couvre les 5 rôles — `match_trio_stats` ne
    couvre que jgl/mid/sup par construction)."""
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT us.match_id, m.patch, us.win,
                   us.jgl_champion AS us_jgl, us.mid_champion AS us_mid,
                   us.sup_champion AS us_sup,
                   them.jgl_champion AS them_jgl, them.mid_champion AS them_mid,
                   them.sup_champion AS them_sup,
                   us_top.champion_id AS us_top, us_bot.champion_id AS us_bot,
                   them_top.champion_id AS them_top, them_bot.champion_id AS them_bot
            FROM match_trio_stats us
            JOIN match_trio_stats them
                ON them.match_id = us.match_id AND them.team_id <> us.team_id
            JOIN matches m ON m.match_id = us.match_id AND m.patch = ANY(%(patches)s)
            JOIN match_participants us_top
                ON us_top.match_id = us.match_id AND us_top.team_id = us.team_id
               AND us_top.role = 'TOP'
            JOIN match_participants us_bot
                ON us_bot.match_id = us.match_id AND us_bot.team_id = us.team_id
               AND us_bot.role = 'BOTTOM'
            JOIN match_participants them_top
                ON them_top.match_id = us.match_id AND them_top.team_id = them.team_id
               AND them_top.role = 'TOP'
            JOIN match_participants them_bot
                ON them_bot.match_id = us.match_id AND them_bot.team_id = them.team_id
               AND them_bot.role = 'BOTTOM'
            """,
            {"patches": patches},
        )
        return cur.fetchall()


def _row_features_5roles(
    row: dict,
    agg_champion: dict,
    agg_duo: dict,
    agg_trio: dict,
    agg_matchup: dict,
    patches: list[str],
) -> list[float] | None:
    base = _row_features_reduced(row, agg_champion, agg_duo, agg_trio, agg_matchup, patches)
    if base is None:
        return None
    patch = row["patch"]
    extra: list[float | None] = [
        _matchup_delta(
            agg_matchup, agg_champion, patches, patch, "top", row["us_top"], row["them_top"]
        ),
        _matchup_delta(
            agg_matchup, agg_champion, patches, patch, "bot", row["us_bot"], row["them_bot"]
        ),
        _champion_wr(agg_champion, patches, patch, "top", row["us_top"]),
        _champion_wr(agg_champion, patches, patch, "top", row["them_top"]),
        _champion_wr(agg_champion, patches, patch, "bot", row["us_bot"]),
        _champion_wr(agg_champion, patches, patch, "bot", row["them_bot"]),
    ]
    if any(v is None for v in extra):
        return None
    return base + extra


def build_five_role_feature_table(
    conn: psycopg.Connection, patches: list[str]
) -> tuple[list[list[float]], list[int], list[str]]:
    """Comme `build_reduced_feature_table`, mais sur `FIVE_ROLE_FEATURE_NAMES`
    (15 features : les 9 de `REDUCED_FEATURE_NAMES` + top/adc)."""
    match_rows = _fetch_match_rows_5roles(conn, patches)
    agg_champion = _fetch_agg_champion(conn, patches)
    agg_duo = _fetch_agg_duo(conn, patches)
    agg_trio = _fetch_agg_trio(conn, patches)
    agg_matchup = _fetch_agg_matchup(conn, patches)

    X: list[list[float]] = []
    y: list[int] = []
    match_ids: list[str] = []
    dropped = 0
    for row in match_rows:
        row_features = _row_features_5roles(
            row, agg_champion, agg_duo, agg_trio, agg_matchup, patches
        )
        if row_features is None:
            dropped += 1
            continue
        X.append(row_features)
        y.append(1 if row["win"] else 0)
        match_ids.append(row["match_id"])

    logger.info(
        "features 5 rôles fenêtre %s : %d lignes complètes, %d exclues (baseline manquante)",
        "+".join(patches),
        len(X),
        dropped,
    )
    return X, y, match_ids
