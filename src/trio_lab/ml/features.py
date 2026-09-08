"""Construction des features de prédiction de winrate au DRAFT (jgl/mid/sup).

Contrairement à `synergy/win_factors.py`/`gold_factors.py` (état de la game à
15 min : gold, CC, vision...), les features ici sont toutes disponibles AU
DRAFT, avant que la partie commence : winrate individuel des champions,
synergie de duo/trio, delta de matchup par rôle. Aucune donnée in-game.

Anti-fuite (« leave-one-patch-out ») : les baselines (`agg_champion`,
`agg_duo`, `agg_trio`, `agg_matchup`) sont calculées pour chaque match en
EXCLUANT le patch de ce match, jamais sur l'historique complet — sinon une
combinaison à faible volume (parfois 1-2 games) intégrerait en partie le
résultat même de la partie qu'on essaie de prédire. Sur une fenêtre à 3
patchs, une feature pour un match du patch P vient donc uniquement des 2
AUTRES patchs de la fenêtre.

Conséquence assumée : les combinaisons qui n'existent que sur UN SEUL patch
de la fenêtre (sortie très récente, rework, pick exotique à 1 game) n'ont
aucune baseline "autre patch" disponible pour LEURS PROPRES matchs et sont
exclues (même principe que `win_factors`/`gold_factors` : cas complets
uniquement). Mesuré sur la fenêtre 16.17+16.16+16.15 (2026-09) : ça ne
concerne qu'environ 7 % des matchs au niveau trio (174 654 combos sur 1 seul
patch, mais seulement ~207k games sur ~2,78M trio-instances) — la grande
majorité des games réelles se joue sur des combinaisons vues sur les 3
patchs.

Pas de lissage bayésien (contrairement à `score_trio`/`score_duo`, pensés
pour l'AFFICHAGE humain) : un modèle appris peut apprendre lui-même à peu
faire confiance à un winrate bruité sur peu de games — mais ça reste une
vraie limite du v1 (features individuellement bruitées sur les combos à
faible volume), notée pour un v2 éventuel.
"""

from __future__ import annotations

import logging

import psycopg

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
)

# jgl/mid/sup (identité du projet) plutôt que les 5 rôles : mêmes rôles que
# `match_trio_stats`, cohérent avec le reste de `synergy/`.
_ROLE_OF = {"jgl": "JUNGLE", "mid": "MIDDLE", "sup": "UTILITY"}
_DUO_PAIRS = (("jgl_mid", "jgl", "mid"), ("jgl_sup", "jgl", "sup"), ("mid_sup", "mid", "sup"))

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
    """Clé (patch, jgl, mid, sup)."""
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


def _sum_excluding(
    table: dict, key_tail: tuple, patches: list[str], exclude_patch: str
) -> GamesWins:
    """Somme `games`/`wins` sur tous les patchs de la fenêtre SAUF `exclude_patch`."""
    games = wins = 0
    for patch in patches:
        if patch == exclude_patch:
            continue
        g, w = table.get((patch, *key_tail), (0, 0))
        games += g
        wins += w
    return games, wins


def _wr(games: int, wins: int) -> float | None:
    return wins / games if games > 0 else None


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


def _row_features(
    row: dict,
    agg_champion: dict,
    agg_duo: dict,
    agg_trio: dict,
    agg_matchup: dict,
    patches: list[str],
) -> list[float] | None:
    """Une ligne de features pour un (match, équipe), ou `None` si une seule
    baseline manque (cas incomplet, jamais imputé — cf. docstring module)."""
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

    us_avg_champion_wr = avg_champion_wr(us)
    them_avg_champion_wr = avg_champion_wr(them)
    us_avg_duo_synergy = avg_duo_synergy(us)
    them_avg_duo_synergy = avg_duo_synergy(them)
    us_trio_wr = _trio_wr(agg_trio, patches, patch, us["jgl"], us["mid"], us["sup"])
    them_trio_wr = _trio_wr(agg_trio, patches, patch, them["jgl"], them["mid"], them["sup"])
    matchup_deltas = [
        _matchup_delta(agg_matchup, agg_champion, patches, patch, r, us[r], them[r])
        for r in ("jgl", "mid", "sup")
    ]

    values = [
        us_avg_champion_wr,
        them_avg_champion_wr,
        us_avg_duo_synergy,
        them_avg_duo_synergy,
        us_trio_wr,
        them_trio_wr,
        *matchup_deltas,
    ]
    if any(v is None for v in values):
        return None
    return values


def build_feature_table(
    conn: psycopg.Connection, patches: list[str]
) -> tuple[list[list[float]], list[int], list[str]]:
    """Construit `(X, y, match_ids)` pour la fenêtre `patches` : `X` (features,
    ordre `FEATURE_NAMES`), `y` (1 = victoire), `match_ids` (pour le split
    déterministe train/test, cf. `ml.train._is_test_match`). Lignes incomplètes
    (baseline manquante, cf. docstring module) silencieusement exclues."""
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
        features = _row_features(row, agg_champion, agg_duo, agg_trio, agg_matchup, patches)
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
