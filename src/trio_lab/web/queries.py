"""Lecture SQL de l'interface — aucune écriture, aucune logique de rendu.

Toutes les fonctions prennent une connexion psycopg SYNC : les routes FastAPI
sont des `def` exécutés en threadpool (pas de psycopg async, donc pas de piège
ProactorEventLoop sous Windows). La région, le patch (via `window_label`) et
les champions sont des paramètres de filtre, jamais des branches de code.
"""

from __future__ import annotations

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
    "gold_diff_5, gold_diff_10, gold_diff_15, vision_score, drakes,"
    " soul_rate, herald_rate, first_tower_rate, cc_time_s,"
    " cc_theoretical_pct, cc_empirical_pct, cc_blended_pct, scaling"
)
DUO_ROLES = ("jgl_mid", "jgl_sup", "mid_sup")
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


def trio_tierlist(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    *,
    champion_id: int | None = None,
    role: str | None = None,  # 'jgl' | 'mid' | 'sup' | None = les trois
    min_games: int = 0,
    min_tier: str = "faible",
    sort: str = "synergy",
    direction: str = "desc",
    page: int = 1,
) -> dict:
    """Une page de tier list des trios + le total pour la pagination."""
    order_dir = SORT_DIRECTIONS[direction]
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
    if champion_id is not None:
        params["champ"] = champion_id
        if role in ("jgl", "mid", "sup"):
            where.append(f"{role}_champion = %(champ)s")
        else:
            where.append(
                "(jgl_champion = %(champ)s OR mid_champion = %(champ)s OR sup_champion = %(champ)s)"
            )
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"""
            SELECT jgl_champion, mid_champion, sup_champion, games, games_eff, wr,
                   synergy_raw, synergy_pred, synergy, ci_low, ci_high, tier,
                   {_STAT_COLUMNS_SQL},
                   count(*) OVER () AS total
            FROM score_trio
            WHERE {" AND ".join(where)}
            ORDER BY {TRIO_SORTS[sort]} {order_dir} NULLS LAST,
                     games DESC, jgl_champion, mid_champion, sup_champion
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
    roles: str,
    *,
    min_games: int = 0,
    min_tier: str = "faible",
    sort: str = "synergy",
    direction: str = "desc",
    page: int = 1,
) -> dict:
    """Une page de tier list des duos d'un couple de rôles."""
    if roles not in DUO_ROLES:
        raise ValueError(f"roles inconnu : {roles!r}")
    order_dir = SORT_DIRECTIONS[direction]
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"""
            SELECT roles, champ_a, champ_b, games, games_eff, wr, synergy,
                   ci_low, ci_high, tier, {_STAT_COLUMNS_SQL},
                   count(*) OVER () AS total
            FROM score_duo
            WHERE window_label = %(window)s AND platform = %(platform)s
              AND roles = %(roles)s AND games >= %(min_games)s AND tier = ANY(%(tiers)s)
            ORDER BY {DUO_SORTS[sort]} {order_dir} NULLS LAST, games DESC, champ_a, champ_b
            OFFSET %(offset)s LIMIT %(per_page)s
            """,
            {
                "window": window,
                "platform": platform,
                "roles": roles,
                "min_games": min_games,
                "tiers": list(_TIER_AT_LEAST[min_tier]),
                "offset": (max(page, 1) - 1) * PER_PAGE,
                "per_page": PER_PAGE,
            },
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
                   synergy_raw, synergy_pred, synergy, ci_low, ci_high, tier,
                   cc_theoretical_pct, cc_empirical_pct, cc_blended_pct, scaling
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


def trio_counters(
    conn: psycopg.Connection, window: str, platform: str, jgl: int, mid: int, sup: int
) -> list[dict]:
    """Tous les matchups du trio, du pire (delta le plus négatif) au meilleur."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT enemy_role, enemy_champion, games, games_eff, wr, delta_raw, delta, tier
            FROM score_trio_vs_champion
            WHERE window_label = %s AND platform = %s
              AND jgl_champion = %s AND mid_champion = %s AND sup_champion = %s
            ORDER BY delta ASC, games DESC
            """,
            (window, platform, jgl, mid, sup),
        ).fetchall()


def trio_allies(
    conn: psycopg.Connection, window: str, platform: str, jgl: int, mid: int, sup: int, limit: int
) -> list[dict]:
    """Meilleurs alliés Top/ADC du trio, du plus fort uplift au plus faible."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT ally_role, ally_champion, games, games_eff, wr, uplift_raw, uplift, tier
            FROM score_trio_with_ally
            WHERE window_label = %s AND platform = %s
              AND jgl_champion = %s AND mid_champion = %s AND sup_champion = %s
            ORDER BY uplift DESC, games DESC
            LIMIT %s
            """,
            (window, platform, jgl, mid, sup, limit),
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


# roles (score_duo/match_trio_stats) → colonnes match_trio_stats des 2 rôles
# fixés du duo (liste blanche, jamais interpolée depuis l'extérieur).
_DUO_ROLE_COLUMNS = {
    "jgl_mid": ("jgl_champion", "mid_champion"),
    "jgl_sup": ("jgl_champion", "sup_champion"),
    "mid_sup": ("mid_champion", "sup_champion"),
}


def duo_score(
    conn: psycopg.Connection, window: str, platform: str, roles: str, champ_a: int, champ_b: int
) -> dict | None:
    """La ligne score_duo d'un duo, ou None si non scoré sur cette fenêtre."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT roles, champ_a, champ_b, games, games_eff, wr, synergy,
                   ci_low, ci_high, tier, {_STAT_COLUMNS_SQL}
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
