"""Lecture SQL de l'interface — aucune écriture, aucune logique de rendu.

Toutes les fonctions prennent une connexion psycopg SYNC : les routes FastAPI
sont des `def` exécutés en threadpool (pas de psycopg async, donc pas de piège
ProactorEventLoop sous Windows). La région, le patch (via `window_label`) et
les champions sont des paramètres de filtre, jamais des branches de code.
"""

from __future__ import annotations

from collections.abc import Sequence

import psycopg
from psycopg.rows import dict_row

from trio_lab.synergy import scores
from trio_lab.synergy.windows import patch_key

# Tris autorisés → colonne SQL (liste blanche, jamais interpolée depuis
# l'extérieur). La direction est un paramètre séparé (`SORT_DIRECTIONS`) ;
# NULLS LAST dans les deux sens, pour qu'un trio sans donnée ne squatte
# jamais le haut du classement (ni en croissant, ni en décroissant).
TRIO_SORTS = {
    "synergy": "synergy",
    "wr": "wr",
    "games": "games",
    "gold5": "gold_diff_5",
    "gold10": "gold_diff_10",
    "gold15": "gold_diff_15",
    "vision": "vision_score",
    "drakes": "drakes",
    "soul": "soul_rate",
    "herald": "herald_rate",
    "tower1": "first_tower_rate",
    "cc": "cc_time_s",
    "cc_blend": "cc_blended_pct",
    "scaling": "scaling",
}
DUO_SORTS = dict(TRIO_SORTS)  # score_duo porte les mêmes colonnes depuis 008/009/010
SORT_DIRECTIONS = {"asc": "ASC", "desc": "DESC"}
_STAT_COLUMNS_SQL = (
    "gold_diff_5, gold_diff_10, gold_diff_15, team_gold_diff_15, vision_score, drakes,"
    " soul_rate, herald_rate, first_tower_rate, cc_time_s,"
    " cc_theoretical_pct, cc_empirical_pct, cc_blended_pct, scaling"
)
# Phase 7 (duo généralisé) : les 3 premières restent sourcées sur
# match_trio_stats (`duo_match_rows`), les 7 suivantes sur match_role_stats
# (`duo_role_match_rows`) — cf. `TRIO_DUO_ROLES` plus bas pour distinguer les
# deux chemins côté web/app.py.
DUO_ROLES = (
    "jgl_mid",
    "jgl_sup",
    "mid_sup",
    "top_jgl",
    "top_mid",
    "top_bot",
    "top_sup",
    "jgl_bot",
    "mid_bot",
    "bot_sup",
)
_TIER_AT_LEAST = {
    "faible": ("faible", "moyen", "eleve"),
    "moyen": ("moyen", "eleve"),
    "eleve": ("eleve",),
}
PER_PAGE = 50


def available_windows(conn: psycopg.Connection) -> list[str]:
    """Étiquettes de fenêtre matérialisées, de la plus récente à la plus ancienne."""
    rows = conn.execute("SELECT DISTINCT window_label FROM score_trio").fetchall()
    return sorted((r[0] for r in rows), key=lambda lbl: patch_key(lbl.split("+")[0]), reverse=True)


def available_platforms(conn: psycopg.Connection, window: str) -> list[str]:
    """Plateformes présentes dans la fenêtre, la plus fournie d'abord (défaut UI)."""
    rows = conn.execute(
        "SELECT platform, sum(games) FROM score_trio WHERE window_label = %s"
        " GROUP BY platform ORDER BY sum(games) DESC",
        (window,),
    ).fetchall()
    return [r[0] for r in rows]


def _order_by_clause(
    sort: Sequence[str], direction: Sequence[str], sort_map: dict[str, str]
) -> str:
    """Clause ORDER BY multi-colonnes (tri façon tableur : plusieurs critères
    dans l'ordre donné). `sort`/`direction` déjà validés par l'appelant
    (whitelist `sort_map`/`SORT_DIRECTIONS`, jamais interpolés bruts)."""
    parts = [
        f"{sort_map[s]} {SORT_DIRECTIONS[d]} NULLS LAST"
        for s, d in zip(sort, direction, strict=True)
    ]
    return ", ".join(parts)


def _threshold_clauses(
    min_values: dict[str, float] | None,
    max_values: dict[str, float] | None,
    sort_map: dict[str, str],
    params: dict,
) -> list[str]:
    """Clauses `colonne >= min` / `colonne <= max` (filtre "au moins X" /
    "au plus X", combinables par colonne — ex. WR entre 45 et 55, retour
    utilisateur 2026-07-20), mêmes clés que le tri (whitelist `sort_map`,
    jamais interpolées brutes) — trouver les combos bons sur plusieurs axes
    à la fois (retour utilisateur, 2026-07-13) : un tri multi-colonnes ne
    suffit pas quand la 1re colonne est presque toujours unique (ex.
    synergie), un filtre par seuils si."""
    where = []
    for key, value in (min_values or {}).items():
        param_name = f"min_{key}"
        params[param_name] = value
        where.append(f"{sort_map[key]} >= %({param_name})s")
    for key, value in (max_values or {}).items():
        param_name = f"max_{key}"
        params[param_name] = value
        where.append(f"{sort_map[key]} <= %({param_name})s")
    return where


def trio_tierlist(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    *,
    jgl_champion_id: int | None = None,
    mid_champion_id: int | None = None,
    sup_champion_id: int | None = None,
    min_games: int = 0,
    min_tier: str = "faible",
    min_values: dict[str, float] | None = None,  # ex. {"wr": .52, "cc": 4.0}
    max_values: dict[str, float] | None = None,  # ex. {"wr": .60, "gold15": 0.0}
    sort: Sequence[str] = ("synergy",),
    direction: Sequence[str] = ("desc",),
    page: int = 1,
) -> dict:
    """Une page de tier list des trios + le total pour la pagination.

    Un champion par rôle (jgl/mid/sup), indépendants et combinables : remplir
    les 3 cible un trio précis, n'en remplir qu'1 ou 2 filtre plus large.
    """
    order_clause = _order_by_clause(sort, direction, TRIO_SORTS)
    where = ["window_label = %(window)s", "platform = %(platform)s", "games >= %(min_games)s"]
    params: dict = {
        "window": window,
        "platform": platform,
        "min_games": min_games,
        "tiers": list(_TIER_AT_LEAST[min_tier]),
        "offset": (max(page, 1) - 1) * PER_PAGE,
        "per_page": PER_PAGE,
    }
    where.append("tier = ANY(%(tiers)s)")
    where.extend(_threshold_clauses(min_values, max_values, TRIO_SORTS, params))
    for role, champion_id in (
        ("jgl", jgl_champion_id),
        ("mid", mid_champion_id),
        ("sup", sup_champion_id),
    ):
        if champion_id is not None:
            params[f"{role}_champ"] = champion_id
            where.append(f"{role}_champion = %({role}_champ)s")
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"""
            SELECT jgl_champion, mid_champion, sup_champion, games, games_eff, wr,
                   synergy_raw, synergy_pred, synergy, ci_low, ci_high, tier,
                   {_STAT_COLUMNS_SQL},
                   count(*) OVER () AS total
            FROM score_trio
            WHERE {" AND ".join(where)}
            ORDER BY {order_clause}, games DESC, jgl_champion, mid_champion, sup_champion
            OFFSET %(offset)s LIMIT %(per_page)s
            """,
            params,
        ).fetchall()
    total = rows[0]["total"] if rows else 0
    for row in rows:
        row.pop("total", None)
    return {"rows": rows, "total": total, "page": max(page, 1), "per_page": PER_PAGE}


def duo_tierlist(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    roles: str | None,
    *,
    champ_a_id: int | None = None,
    champ_b_id: int | None = None,
    min_games: int = 0,
    min_tier: str = "faible",
    min_values: dict[str, float] | None = None,  # ex. {"wr": .52, "cc": 4.0}
    max_values: dict[str, float] | None = None,  # ex. {"wr": .60, "gold15": 0.0}
    sort: Sequence[str] = ("synergy",),
    direction: Sequence[str] = ("desc",),
    page: int = 1,
) -> dict:
    """Une page de tier list des duos d'un couple de rôles — ou, `roles=None`
    (retour utilisateur 2026-07-20), toutes les paires mélangées : chercher
    par seuil "peu importe les rôles" sans devoir en choisir une. `roles` est
    déjà renvoyée par ligne (`SELECT roles, ...` ci-dessous), donc l'affichage
    par combo reste correct même mélangé — seule la clause WHERE change.

    `champ_a_id`/`champ_b_id` : indépendants et combinables, dans l'ordre des
    rôles de `roles` (ex. jgl_mid → champ_a=jungle, champ_b=mid) — même
    principe que `trio_tierlist`. Non applicables si `roles` est None (quel
    rôle serait champ_a ?) — laissés à `None` par l'appelant dans ce cas.
    """
    if roles is not None and roles not in DUO_ROLES:
        raise ValueError(f"roles inconnu : {roles!r}")
    order_clause = _order_by_clause(sort, direction, DUO_SORTS)
    where = [
        "window_label = %(window)s",
        "platform = %(platform)s",
        "games >= %(min_games)s",
        "tier = ANY(%(tiers)s)",
    ]
    params: dict = {
        "window": window,
        "platform": platform,
        "min_games": min_games,
        "tiers": list(_TIER_AT_LEAST[min_tier]),
        "offset": (max(page, 1) - 1) * PER_PAGE,
        "per_page": PER_PAGE,
    }
    if roles is not None:
        where.append("roles = %(roles)s")
        params["roles"] = roles
    where.extend(_threshold_clauses(min_values, max_values, DUO_SORTS, params))
    for col, champion_id in (("champ_a", champ_a_id), ("champ_b", champ_b_id)):
        if champion_id is not None:
            params[col] = champion_id
            where.append(f"{col} = %({col})s")
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"""
            SELECT roles, champ_a, champ_b, games, games_eff, wr, synergy,
                   ci_low, ci_high, tier, {_STAT_COLUMNS_SQL},
                   count(*) OVER () AS total
            FROM score_duo
            WHERE {" AND ".join(where)}
            ORDER BY {order_clause}, games DESC, champ_a, champ_b
            OFFSET %(offset)s LIMIT %(per_page)s
            """,
            params,
        ).fetchall()
    total = rows[0].pop("total") if rows else 0
    for row in rows:
        row.pop("total", None)
    return {"rows": rows, "total": total, "page": max(page, 1), "per_page": PER_PAGE}


def trio_score(
    conn: psycopg.Connection, window: str, platform: str, jgl: int, mid: int, sup: int
) -> dict | None:
    """La ligne score_trio d'un trio, ou None si non scoré sur cette fenêtre."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT jgl_champion, mid_champion, sup_champion, games, games_eff, wr,
                   synergy_raw, synergy_pred, synergy, synergy_ci_low, synergy_ci_high,
                   ci_low, ci_high, tier,
                   cc_theoretical_pct, cc_empirical_pct, cc_blended_pct,
                   scaling, scaling_ci_low, scaling_ci_high
            FROM score_trio
            WHERE window_label = %s AND platform = %s
              AND jgl_champion = %s AND mid_champion = %s AND sup_champion = %s
            """,
            (window, platform, jgl, mid, sup),
        ).fetchone()


def member_wr(
    conn: psycopg.Connection,
    patches: list[str],
    platform: str,
    role: str,
    champion_id: int,
    weights: dict[str, float],
) -> float | None:
    """WR individuel pondéré fenêtre d'un champion dans un rôle — la baseline
    utilisée pour la synergie (WR(combo) − moyenne des WR individuels), mais
    jamais matérialisée telle quelle : recalculée en lecture depuis agg_champion
    (même mécanique que `synergy.compute.member_wr`, moins la matérialisation).
    `platform='all'` agrège toutes les régions.
    """
    with conn.cursor() as cur:
        rows = cur.execute(
            """
            SELECT patch, sum(games) AS games, sum(wins) AS wins
            FROM agg_champion
            WHERE patch = ANY(%(patches)s) AND role = %(role)s AND champion_id = %(champ)s
              AND (%(platform)s = 'all' OR platform = %(platform)s)
            GROUP BY patch
            """,
            {"patches": patches, "role": role, "champ": champion_id, "platform": platform},
        ).fetchall()
    result = scores.weighted_wr(rows, weights)
    return result.wr if result else None


def champion_baseline(
    conn: psycopg.Connection,
    patches: list[str],
    platform: str,
    role: str,
    champion_id: int,
    weights: dict[str, float],
) -> dict | None:
    """WR + games pondérés d'un champion dans un rôle — la fiche complète
    (contrairement à `member_wr`, qui ne renvoie que le WR pour les pages
    trio/duo). `None` si aucun games effectif sur la fenêtre."""
    with conn.cursor() as cur:
        rows = cur.execute(
            """
            SELECT patch, sum(games) AS games, sum(wins) AS wins
            FROM agg_champion
            WHERE patch = ANY(%(patches)s) AND role = %(role)s AND champion_id = %(champ)s
              AND (%(platform)s = 'all' OR platform = %(platform)s)
            GROUP BY patch
            """,
            {"patches": patches, "role": role, "champ": champion_id, "platform": platform},
        ).fetchall()
    result = scores.weighted_wr(rows, weights)
    if result is None:
        return None
    return {"wr": result.wr, "games": result.games, "games_eff": result.games_eff}


# (roles de score_duo, rôle du partenaire) accessibles depuis chaque rôle
# fixé — ex. depuis 'jgl' : meilleurs mids (via 'jgl_mid') et meilleurs
# supports (via 'jgl_sup').
CHAMPION_PARTNER_GROUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "jgl": (("jgl_mid", "mid"), ("jgl_sup", "sup")),
    "mid": (("jgl_mid", "jgl"), ("mid_sup", "sup")),
    "sup": (("jgl_sup", "jgl"), ("mid_sup", "mid")),
}


def champion_best_partners(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    roles: str,
    fixed_role: str,
    champion_id: int,
    limit: int,
    *,
    min_tier: str = "moyen",
) -> list[dict]:
    """Meilleurs partenaires d'un champion (rôle fixé) dans l'autre rôle du
    couple `roles` — ex. `fixed_role='jgl'`, `roles='jgl_mid'` → meilleurs mids.

    `score_duo` porte des colonnes génériques `champ_a`/`champ_b` (pas de
    colonnes par rôle comme `score_trio`/`match_trio_stats`) : `champ_a`
    correspond toujours au premier rôle de `roles` (cf. `compute.DUO_ROLES`).
    `min_tier` écarte les duos trop peu joués (défaut « moyen », ≥ 50
    games_eff) : sans plancher, un duo à 1-2 games avec une synergie extrême
    dominait le classement (retour utilisateur, 2026-07-12).
    """
    role_a, _role_b = roles.split("_")
    fixed_col, partner_col = (
        ("champ_a", "champ_b") if fixed_role == role_a else ("champ_b", "champ_a")
    )
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT {partner_col} AS partner_champion, games, games_eff, wr, synergy, tier
            FROM score_duo
            WHERE window_label = %(window)s AND platform = %(platform)s AND roles = %(roles)s
              AND {fixed_col} = %(champ)s AND tier = ANY(%(tiers)s)
            ORDER BY synergy DESC, games DESC
            LIMIT %(limit)s
            """,
            {
                "window": window,
                "platform": platform,
                "roles": roles,
                "champ": champion_id,
                "limit": limit,
                "tiers": list(_TIER_AT_LEAST[min_tier]),
            },
        ).fetchall()


def matchup_candidates(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    role: str,
    enemy_champion_id: int,
    limit: int,
) -> list[dict]:
    """Symétrique de `champion_best_partners` côté counter (simulateur de
    draft, Phase 8) : `champ_b` (l'ennemi) est fixé, on liste tous les
    `champ_a` candidats et leur delta — pas de filtre de fiabilité ici,
    l'appelant (app.py) grise plutôt que masque (retour utilisateur)."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT champ_a AS candidate_champion, games, games_eff, wr, delta, tier
            FROM score_matchup
            WHERE window_label = %(window)s AND platform = %(platform)s AND role = %(role)s
              AND champ_b = %(enemy)s
            ORDER BY delta DESC
            LIMIT %(limit)s
            """,
            {
                "window": window,
                "platform": platform,
                "role": role,
                "enemy": enemy_champion_id,
                "limit": limit,
            },
        ).fetchall()


def role_worst_matchups(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    role: str,
    *,
    min_games_eff: float,
    notable_delta: float,
) -> dict[int, dict]:
    """Exposition aux contres par champion pour un rôle — signal de risque
    « blind pick » (simulateur de draft, Phase 8, retour utilisateur : « un
    blind pick est un pick qui a peu de counter, ou dont les counters n'ont
    pas un énorme WR contre lui »). `worst_delta` = pire matchup connu
    (MIN(delta)) ; `notable_counters` = NOMBRE de matchups au moins aussi
    mauvais que `notable_delta` — un champion avec un seul pire cas sévère
    est un risque différent d'un champion avec dix contres modérés, ce que
    `worst_delta` seul ne distingue pas. Un seul aller-retour par rôle (pas
    par champion candidat), donc utilisable sur une grille complète."""
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT champ_a AS candidate_champion, min(delta) AS worst_delta,
                   count(*) FILTER (WHERE delta <= %(notable_delta)s) AS notable_counters
            FROM score_matchup
            WHERE window_label = %(window)s AND platform = %(platform)s AND role = %(role)s
              AND games_eff >= %(min_games_eff)s
            GROUP BY champ_a
            """,
            {
                "window": window,
                "platform": platform,
                "role": role,
                "min_games_eff": min_games_eff,
                "notable_delta": notable_delta,
            },
        ).fetchall()
    return {
        r["candidate_champion"]: {
            "worst_delta": r["worst_delta"],
            "notable_counters": r["notable_counters"],
        }
        for r in rows
    }


def champion_role_baseline_list(
    conn: psycopg.Connection, window: str, platform: str, role: str, limit: int
) -> list[dict]:
    """Champions triés par WR baseline dans un rôle (simulateur de draft,
    Phase 8) : repli quand aucun allié/ennemi n'est encore verrouillé (1er
    pick) — pas de synergie/counter à calculer, juste le WR individuel."""
    patches = window.split("+")
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT champion_id AS candidate_champion, sum(games) AS games,
                   sum(wins)::real / NULLIF(sum(games), 0) AS wr
            FROM agg_champion
            WHERE patch = ANY(%(patches)s) AND role = %(role)s
              AND (%(platform)s = 'all' OR platform = %(platform)s)
            GROUP BY champion_id
            HAVING sum(games) > 0
            ORDER BY wr DESC
            LIMIT %(limit)s
            """,
            {"patches": patches, "platform": platform, "role": role, "limit": limit},
        ).fetchall()


def champion_role_distribution(
    conn: psycopg.Connection, window: str, platform: str, *, min_games: int = 1
) -> list[dict]:
    """Répartition des games d'un champion entre rôles (Phase 8, détecteur de
    picks flex) — `agg_champion`, historique complet retenu (contrairement à
    `match_role_stats`, jeune). Sert à distinguer le rôle principal des rôles
    secondaires réellement joués, pas du bruit de troll pick isolé.

    `wins` (retour utilisateur 2026-07-20) : sert à afficher le WR réel du
    pick flex dans son rôle secondaire, pas seulement son profil de
    ressources — un pick qui "joue différemment" mais perd plus n'est pas
    le même signal qu'un pick qui gagne autant ou plus."""
    patches = window.split("+")
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT champion_id, role, sum(games) AS games, sum(wins) AS wins
            FROM agg_champion
            WHERE patch = ANY(%(patches)s)
              AND (%(platform)s = 'all' OR platform = %(platform)s)
            GROUP BY champion_id, role
            HAVING sum(games) >= %(min_games)s
            """,
            {"patches": patches, "platform": platform, "min_games": min_games},
        ).fetchall()


def role_resource_profile(
    conn: psycopg.Connection, window: str, platform: str, *, min_games: int
) -> list[dict]:
    """Profil ressources (gold@15, dégâts/gold) par (champion, rôle), depuis
    `match_role_stats` (Phase 8, détecteur de picks flex) — limité à la
    profondeur de cette table (déployée le 19/07/2026, pas d'historique
    avant). `gold_15`/`dmg_per_gold` ne nécessitent pas de normalisation par
    durée (contrairement à `cc_time_s`/`vision_score`), pas besoin de
    `game_duration_s` ici."""
    patches = window.split("+")
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT mrs.champion_id, mrs.role, count(*) AS n,
                   avg(mrs.gold_15) AS avg_gold_15, avg(mrs.dmg_per_gold) AS avg_dmg_per_gold
            FROM match_role_stats mrs
            JOIN matches m USING (match_id)
            WHERE m.patch = ANY(%(patches)s) AND mrs.gold_15 IS NOT NULL
              AND (%(platform)s = 'all' OR m.platform = %(platform)s)
            GROUP BY mrs.champion_id, mrs.role
            HAVING count(*) >= %(min_games)s
            """,
            {"patches": patches, "platform": platform, "min_games": min_games},
        ).fetchall()


def role_resource_baseline(conn: psycopg.Connection, window: str, platform: str) -> dict[str, dict]:
    """Moyennes de référence par rôle (tous champions confondus), même source
    que `role_resource_profile` — le point de comparaison du détecteur de
    picks flex (Phase 8)."""
    patches = window.split("+")
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT mrs.role, avg(mrs.gold_15) AS avg_gold_15,
                   avg(mrs.dmg_per_gold) AS avg_dmg_per_gold
            FROM match_role_stats mrs
            JOIN matches m USING (match_id)
            WHERE m.patch = ANY(%(patches)s) AND mrs.gold_15 IS NOT NULL
              AND (%(platform)s = 'all' OR m.platform = %(platform)s)
            GROUP BY mrs.role
            """,
            {"patches": patches, "platform": platform},
        ).fetchall()
    return {r["role"]: r for r in rows}


def win_factors(conn: psycopg.Connection, window: str, population: str) -> list[dict]:
    """Coefficients de la régression logistique multi-variables (Phase 8,
    `synergy.win_factors`) — pas de dimension `platform` : l'analyse porte
    sur toutes les régions combinées (question globale, pas régionale).
    Liste vide si `synergy.win_factors` n'a jamais tourné pour cette fenêtre
    (rafraîchissement manuel, pas dans le cycle service)."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT feature, coef, odds_ratio, n FROM score_win_factors"
            " WHERE window_label = %s AND population = %s",
            (window, population),
        ).fetchall()


def gold_factors(conn: psycopg.Connection, window: str) -> list[dict]:
    """Coefficients du modèle "qu'est-ce qui construit l'avantage au gold"
    (Phase 8, `synergy.gold_factors`) — pas de dimension `platform`, même
    raisonnement que `win_factors`. Inclut les lignes de diagnostic
    `_r2_draft_only`/`_r2_full` (feature spéciale, `coef` porte le R²) :
    l'appelant les sépare des vraies features avant affichage. Liste vide si
    `synergy.gold_factors` n'a jamais tourné pour cette fenêtre."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT block, feature, coef, n FROM score_gold_factors WHERE window_label = %s",
            (window,),
        ).fetchall()


def champion_resilience(
    conn: psycopg.Connection, window: str, factor: str, *, role: str | None = None
) -> list[dict]:
    """Écarts avance/retard par champion (Phase 8, `synergy.resilience`) —
    pas de dimension `platform`, même raisonnement que `win_factors`/
    `gold_factors`. Liste vide si `synergy.resilience` n'a jamais tourné
    pour cette fenêtre (rafraîchissement manuel)."""
    with conn.cursor(row_factory=dict_row) as cur:
        query = (
            "SELECT role, champion_id, games_ahead, wins_ahead, games_behind, wins_behind"
            " FROM score_champion_resilience WHERE window_label = %(window)s"
            " AND factor = %(factor)s"
        )
        params: dict[str, str] = {"window": window, "factor": factor}
        if role is not None:
            query += " AND role = %(role)s"
            params["role"] = role
        return cur.execute(query, params).fetchall()


def cc_theoretical_scores(conn: psycopg.Connection) -> dict[int, float]:
    """Score CC théorique par champion, depuis la table matérialisée (010) —
    jamais le fichier gelé : le service web ne l'embarque pas (voir Dockerfile),
    contrairement au pipeline `synergy.compute` qui tourne côté collector."""
    rows = conn.execute("SELECT champion_id, score FROM champion_cc_theoretical").fetchall()
    return dict(rows)


def trio_duos(
    conn: psycopg.Connection, window: str, platform: str, jgl: int, mid: int, sup: int
) -> list[dict]:
    """Les scores des 3 duos internes du trio (ceux qui existent)."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT roles, champ_a, champ_b, games, games_eff, wr, synergy,
                   ci_low, ci_high, tier
            FROM score_duo
            WHERE window_label = %(window)s AND platform = %(platform)s
              AND ((roles = 'jgl_mid' AND champ_a = %(jgl)s AND champ_b = %(mid)s)
                OR (roles = 'jgl_sup' AND champ_a = %(jgl)s AND champ_b = %(sup)s)
                OR (roles = 'mid_sup' AND champ_a = %(mid)s AND champ_b = %(sup)s))
            ORDER BY roles
            """,
            {"window": window, "platform": platform, "jgl": jgl, "mid": mid, "sup": sup},
        ).fetchall()


def window_freshness(conn: psycopg.Connection, window: str) -> dict:
    """Volume de matchs de la fenêtre + horodatage du dernier match collecté.

    `matches` de la fenêtre = matchs bruts des patchs qui la composent
    (`window.split("+")`) ; c'est le volume réel derrière les scores affichés,
    pas `games_eff` (pondéré) ni le nombre de combinaisons scorées.
    """
    patches = window.split("+")
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT count(*) AS matches, max(collected_at) AS last_collected_at"
            " FROM matches WHERE patch = ANY(%s)",
            (patches,),
        ).fetchone()


def collection_status(conn: psycopg.Connection) -> dict:
    """État de la collecte pour le monitoring (`/api/status`, Phase 6)."""
    with conn.cursor(row_factory=dict_row) as cur:
        per_day = cur.execute(
            """
            SELECT to_char(collected_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
                   platform, count(*) AS matches
            FROM matches
            WHERE collected_at > now() - interval '7 days'
            GROUP BY 1, 2 ORDER BY 1 DESC, 2
            """
        ).fetchall()
        per_patch = cur.execute(
            "SELECT patch, count(*) AS matches FROM matches GROUP BY patch ORDER BY patch"
        ).fetchall()
        journal = cur.execute(
            "SELECT status, count(*) AS entries FROM match_fetch_journal GROUP BY status"
        ).fetchall()
        totals = cur.execute(
            "SELECT count(*) AS total_matches, max(collected_at) AS last_collected_at FROM matches"
        ).fetchone()
    return {
        "total_matches": totals["total_matches"],
        "last_collected_at": totals["last_collected_at"],
        "matches_per_day": per_day,
        "matches_per_patch": per_patch,
        "journal": {row["status"]: row["entries"] for row in journal},
    }


def collector_gaps(
    conn: psycopg.Connection, *, lookback_hours: int = 48, threshold_minutes: int = 3
) -> list[dict]:
    """Trous de collecte détectés sur `matches.collected_at` (dashboard `/admin`).

    Un trou > `threshold_minutes` entre deux matchs consécutifs, tous plateformes
    confondues, indique un arrêt du collecteur (crash, redéploiement, incident
    réseau) — les pauses normales entre plateformes font ~60-90s (constaté en
    session le 16/07/2026)."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            WITH ordered AS (
                SELECT collected_at, LAG(collected_at) OVER (ORDER BY collected_at) AS prev
                FROM matches WHERE collected_at > now() - (%s || ' hours')::interval
            )
            SELECT prev AS gap_start, collected_at AS gap_end,
                   extract(epoch FROM (collected_at - prev))::int AS gap_seconds
            FROM ordered
            WHERE collected_at - prev > (%s || ' minutes')::interval
            ORDER BY collected_at DESC
            """,
            (lookback_hours, threshold_minutes),
        ).fetchall()


def table_sizes(conn: psycopg.Connection, *, limit: int = 12) -> list[dict]:
    """Tailles des plus grosses tables du schéma courant (dashboard `/admin`).

    `current_schema()` plutôt qu'un nom en dur : le rôle applicatif n'a accès
    qu'à son propre schéma (`trio_lab` en prod, `trio_lab_test` en test) —
    un nom fixe casse en base de test avec un `permission denied` (vécu)."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT relname AS table_name,
                   pg_total_relation_size(
                       quote_ident(schemaname) || '.' || quote_ident(relname)
                   ) AS bytes
            FROM pg_stat_user_tables
            WHERE schemaname = current_schema()
            ORDER BY bytes DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()


def trio_match_rows(
    conn: psycopg.Connection,
    patches: list[str],
    platform: str | None,
    jgl: int,
    mid: int,
    sup: int,
) -> list[dict]:
    """Lignes match_trio_stats du trio sur les patchs de la fenêtre.

    Enrichies de `patch` et `game_duration_s` pour `summary.summarize` (les
    poids de fenêtre et le profil de tempo). `platform=None` = toutes les
    régions. Volume par trio modeste : l'agrégation se fait en Python.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT m.patch, m.game_duration_s, t.*
            FROM match_trio_stats t
            JOIN matches m USING (match_id)
            WHERE m.patch = ANY(%(patches)s)
              AND (%(platform)s::text IS NULL OR m.platform = %(platform)s)
              AND t.jgl_champion = %(jgl)s AND t.mid_champion = %(mid)s
              AND t.sup_champion = %(sup)s
            """,
            {"patches": patches, "platform": platform, "jgl": jgl, "mid": mid, "sup": sup},
        ).fetchall()


_CHAMPION_ROLE_COLUMNS = {"jgl": "jgl_champion", "mid": "mid_champion", "sup": "sup_champion"}


def champion_match_rows(
    conn: psycopg.Connection,
    patches: list[str],
    platform: str | None,
    role: str,
    champion_id: int,
) -> list[dict]:
    """Lignes match_trio_stats des games où ce champion occupe ce rôle, quels
    que soient les 2 autres membres — mêmes stats d'équipe que la page duo,
    filtrées sur un seul rôle au lieu de deux (cf. `duo_match_rows`)."""
    col = _CHAMPION_ROLE_COLUMNS[role]
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT m.patch, m.game_duration_s, t.*
            FROM match_trio_stats t
            JOIN matches m USING (match_id)
            WHERE m.patch = ANY(%(patches)s)
              AND (%(platform)s::text IS NULL OR m.platform = %(platform)s)
              AND t.{col} = %(champ)s
            """,
            {"patches": patches, "platform": platform, "champ": champion_id},
        ).fetchall()


# roles (score_duo/match_trio_stats) → colonnes match_trio_stats des 2 rôles
# fixés du duo (liste blanche, jamais interpolée depuis l'extérieur).
_DUO_ROLE_COLUMNS = {
    "jgl_mid": ("jgl_champion", "mid_champion"),
    "jgl_sup": ("jgl_champion", "sup_champion"),
    "mid_sup": ("mid_champion", "sup_champion"),
}
# Les 3 paires internes au trio jgl/mid/sup : source match_trio_stats, notion
# de « 3e membre libre » (best_trios). Les 7 autres (Phase 7) n'ont pas de
# notion de trio équivalente et sourcent match_role_stats — cf.
# `duo_role_match_rows`, utilisée par `web/app.py._duo_detail` pour brancher.
TRIO_DUO_ROLES = frozenset(_DUO_ROLE_COLUMNS)


def duo_score(
    conn: psycopg.Connection, window: str, platform: str, roles: str, champ_a: int, champ_b: int
) -> dict | None:
    """La ligne score_duo d'un duo, ou None si non scoré sur cette fenêtre."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT roles, champ_a, champ_b, games, games_eff, wr, synergy,
                   synergy_ci_low, synergy_ci_high, ci_low, ci_high, tier,
                   scaling_ci_low, scaling_ci_high, {_STAT_COLUMNS_SQL}
            FROM score_duo
            WHERE window_label = %s AND platform = %s AND roles = %s
              AND champ_a = %s AND champ_b = %s
            """,
            (window, platform, roles, champ_a, champ_b),
        ).fetchone()


def duo_match_rows(
    conn: psycopg.Connection,
    patches: list[str],
    platform: str | None,
    roles: str,
    champ_a: int,
    champ_b: int,
) -> list[dict]:
    """Lignes match_trio_stats du duo (les 2 rôles fixés, le 3e libre — les
    stats du duo sont les stats d'équipe des parties où il apparaît, quel que
    soit le 3e membre, cf. `_DUO_SQL` d'aggregate.py)."""
    if roles not in _DUO_ROLE_COLUMNS:
        raise ValueError(f"roles inconnu : {roles!r}")
    col_a, col_b = _DUO_ROLE_COLUMNS[roles]
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT m.patch, m.game_duration_s, t.*
            FROM match_trio_stats t
            JOIN matches m USING (match_id)
            WHERE m.patch = ANY(%(patches)s)
              AND (%(platform)s::text IS NULL OR m.platform = %(platform)s)
              AND t.{col_a} = %(champ_a)s AND t.{col_b} = %(champ_b)s
            """,
            {"patches": patches, "platform": platform, "champ_a": champ_a, "champ_b": champ_b},
        ).fetchall()


def duo_role_match_rows(
    conn: psycopg.Connection,
    patches: list[str],
    platform: str | None,
    role_a: str,
    role_b: str,
    champ_a: int,
    champ_b: int,
) -> list[dict]:
    """Équivalent de `duo_match_rows` pour une paire de rôles hors trio
    jgl/mid/sup (Phase 7, duo généralisé) : source match_role_stats (5 rôles)
    au lieu de match_trio_stats, qui ne connaît que jgl/mid/sup.

    Gold : vrai diff DE LA PAIRE (auto-jointure avec l'équipe adverse, mêmes
    2 rôles), pas le gold_diff_X du trio complet — plus précis, permis par le
    grain par-rôle de match_role_stats. Colonnes CC/dmg-par-gold/KP aliasées
    en champ_a/b_* génériques : `summary.summarize` les traite déjà (cf.
    summary.py, `_MEAN_KEYS`/`_PER_MINUTE_KEYS`/`_RATIO_KEYS`).

    Objectifs (grubs/herald/drakes/âme/nashor/tours/plaques) et CS jungle à
    15 min : stats d'ÉQUIPE déjà calculées dans match_trio_stats (mêmes
    valeurs quelle que soit la paire de rôles regardée) — récupérées par
    jointure sur (match_id, team_id), pas dupliquées ici. Part de dégâts :
    somme exacte des 2 membres ÷ dégâts totaux de l'équipe (dérivés en
    sommant les 5 lignes match_role_stats de ce match/équipe). First blood :
    OR exact (un seul événement, aucun risque de double-comptage) — mais le
    kill participation reste INDIVIDUEL par membre (cf. `role_kill_participation`
    dans `stats/extract.py` : le combiner en OR demanderait de revérifier
    l'appartenance des 2 pids à chaque kill, pas de sommer 2 ratios).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT m.patch, m.game_duration_s, ra.win,
                   (ra.gold_5 + rb.gold_5) - (ea.gold_5 + eb.gold_5) AS gold_diff_5,
                   (ra.gold_10 + rb.gold_10) - (ea.gold_10 + eb.gold_10) AS gold_diff_10,
                   (ra.gold_15 + rb.gold_15) - (ea.gold_15 + eb.gold_15) AS gold_diff_15,
                   (ra.gold_20 + rb.gold_20) - (ea.gold_20 + eb.gold_20) AS gold_diff_20,
                   (ra.gold_25 + rb.gold_25) - (ea.gold_25 + eb.gold_25) AS gold_diff_25,
                   (ra.gold_30 + rb.gold_30) - (ea.gold_30 + eb.gold_30) AS gold_diff_30,
                   (ra.gold_35 + rb.gold_35) - (ea.gold_35 + eb.gold_35) AS gold_diff_35,
                   ra.cc_time_s AS champ_a_cc_time_s, rb.cc_time_s AS champ_b_cc_time_s,
                   ra.dmg_per_gold AS champ_a_dmg_per_gold, rb.dmg_per_gold AS champ_b_dmg_per_gold,
                   ra.kp_pre15 AS champ_a_kp_pre15, rb.kp_pre15 AS champ_b_kp_pre15,
                   ra.vision_score + rb.vision_score AS vision_score,
                   ra.wards_placed + rb.wards_placed AS wards_placed,
                   ra.wards_killed + rb.wards_killed AS wards_killed,
                   (ra.damage + rb.damage)::real / NULLIF((
                       SELECT sum(rt.damage) FROM match_role_stats rt
                       WHERE rt.match_id = ra.match_id AND rt.team_id = ra.team_id
                   ), 0) AS damage_share,
                   (ra.first_blood OR rb.first_blood) AS first_blood_trio,
                   mt.grubs_taken, mt.herald_taken, mt.drakes_taken, mt.soul_taken,
                   mt.nashor_first, mt.nashor_first_s, mt.first_tower, mt.towers_destroyed,
                   mt.plates_taken, mt.jgl_cs_diff_15
            FROM match_role_stats ra
            JOIN match_role_stats rb
                ON rb.match_id = ra.match_id AND rb.team_id = ra.team_id AND rb.role = %(role_b)s
            JOIN matches m ON m.match_id = ra.match_id
            JOIN match_trio_stats mt ON mt.match_id = ra.match_id AND mt.team_id = ra.team_id
            JOIN match_role_stats ea
                ON ea.match_id = ra.match_id AND ea.team_id <> ra.team_id AND ea.role = %(role_a)s
            JOIN match_role_stats eb
                ON eb.match_id = ra.match_id AND eb.team_id = ea.team_id AND eb.role = %(role_b)s
            WHERE ra.role = %(role_a)s AND ra.champion_id = %(champ_a)s
              AND rb.champion_id = %(champ_b)s
              AND m.patch = ANY(%(patches)s)
              AND (%(platform)s::text IS NULL OR m.platform = %(platform)s)
            """,
            {
                "patches": patches,
                "platform": platform,
                "role_a": role_a,
                "role_b": role_b,
                "champ_a": champ_a,
                "champ_b": champ_b,
            },
        ).fetchall()


def duo_best_trios(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    roles: str,
    champ_a: int,
    champ_b: int,
    limit: int,
) -> list[dict]:
    """Meilleurs trios formés à partir de ce duo (3e rôle libre), triés par synergie."""
    if roles not in _DUO_ROLE_COLUMNS:
        raise ValueError(f"roles inconnu : {roles!r}")
    col_a, col_b = _DUO_ROLE_COLUMNS[roles]
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT jgl_champion, mid_champion, sup_champion, games, games_eff, wr, synergy, tier
            FROM score_trio
            WHERE window_label = %(window)s AND platform = %(platform)s
              AND {col_a} = %(champ_a)s AND {col_b} = %(champ_b)s
            ORDER BY synergy DESC, games DESC
            LIMIT %(limit)s
            """,
            {
                "window": window,
                "platform": platform,
                "champ_a": champ_a,
                "champ_b": champ_b,
                "limit": limit,
            },
        ).fetchall()
