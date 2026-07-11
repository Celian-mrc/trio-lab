"""Lecture SQL de l'interface — aucune écriture, aucune logique de rendu.

Toutes les fonctions prennent une connexion psycopg SYNC : les routes FastAPI
sont des `def` exécutés en threadpool (pas de psycopg async, donc pas de piège
ProactorEventLoop sous Windows). La région, le patch (via `window_label`) et
les champions sont des paramètres de filtre, jamais des branches de code.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from trio_lab.synergy.windows import patch_key

# Tris autorisés → clause SQL (liste blanche, jamais interpolé depuis
# l'extérieur). Les stats matérialisées (007) trient NULLS LAST : un trio sans
# donnée ne squatte pas le haut du classement.
TRIO_SORTS = {
    "synergy": "synergy DESC",
    "wr": "wr DESC",
    "games": "games DESC",
    "gold5": "gold_diff_5 DESC NULLS LAST",
    "gold10": "gold_diff_10 DESC NULLS LAST",
    "gold15": "gold_diff_15 DESC NULLS LAST",
    "vision": "vision_score DESC NULLS LAST",
    "drakes": "drakes DESC NULLS LAST",
    "soul": "soul_rate DESC NULLS LAST",
    "herald": "herald_rate DESC NULLS LAST",
    "tower1": "first_tower_rate DESC NULLS LAST",
    "cc": "cc_time_s DESC NULLS LAST",
    "cc_blend": "cc_blended_pct DESC NULLS LAST",
}
DUO_SORTS = dict(TRIO_SORTS)  # score_duo porte les mêmes colonnes depuis 008/009/010
_STAT_COLUMNS_SQL = (
    "gold_diff_5, gold_diff_10, gold_diff_15, vision_score, drakes,"
    " soul_rate, herald_rate, first_tower_rate, cc_time_s,"
    " cc_theoretical_pct, cc_empirical_pct, cc_blended_pct"
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
    page: int = 1,
) -> dict:
    """Une page de tier list des trios + le total pour la pagination."""
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
            ORDER BY {TRIO_SORTS[sort]}, games DESC, jgl_champion, mid_champion, sup_champion
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
    page: int = 1,
) -> dict:
    """Une page de tier list des duos d'un couple de rôles."""
    if roles not in DUO_ROLES:
        raise ValueError(f"roles inconnu : {roles!r}")
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"""
            SELECT roles, champ_a, champ_b, games, games_eff, wr, synergy,
                   ci_low, ci_high, tier, {_STAT_COLUMNS_SQL},
                   count(*) OVER () AS total
            FROM score_duo
            WHERE window_label = %(window)s AND platform = %(platform)s
              AND roles = %(roles)s AND games >= %(min_games)s AND tier = ANY(%(tiers)s)
            ORDER BY {DUO_SORTS[sort]}, games DESC, champ_a, champ_b
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
                   cc_theoretical_pct, cc_empirical_pct, cc_blended_pct
            FROM score_trio
            WHERE window_label = %s AND platform = %s
              AND jgl_champion = %s AND mid_champion = %s AND sup_champion = %s
            """,
            (window, platform, jgl, mid, sup),
        ).fetchone()


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
