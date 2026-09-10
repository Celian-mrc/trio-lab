"""Embeddings de champion appris par factorisation matricielle (SVD), pas
à la main — approche "Wide & Deep" (retour utilisateur 2026-09-10) :
complète les features agrégées existantes plutôt que les remplacer, cf.
`FIVE_ROLE_FEATURE_NAMES`.

Deux sources possibles pour la matrice de co-occurrence, toutes deux
ramenées au même format générique `{(patch, min(i,j), max(i,j)): (games,
wins)}` avant SVD :
- `pair_table_from_agg_duo` : les 3 paires internes du trio (jgl_mid/
  jgl_sup/mid_sup, déjà utilisées pour `duo_synergy`) — v1, mesuré sans
  gain net (retour utilisateur 2026-09-10) : probablement parce que la
  source est déjà exploitée telle quelle ailleurs dans le modèle, rien de
  nouveau à découvrir par factorisation.
- `fetch_5role_pair_table` : co-occurrence de victoire entre 2 champions de
  la MÊME équipe sur les 5 rôles (`match_participants`, pas seulement les 3
  paires internes) — v2, teste si une source plus large (incluant top/adc,
  jamais vue ensemble dans `agg_duo`) capture un vrai signal de synergie.

Le winrate de chaque paire, centré sur 0,5, sert d'entrée à `TruncatedSVD` —
une vraie approche "champion co-occurrence in wins", pas une resynthèse
d'une feature déjà calculée.

Anti-fuite : même mécanique leave-one-patch-out que le reste du module —
UNE matrice (donc UN jeu d'embeddings) par patch exclu, pas une seule
matrice globale. Coût négligeable (SVD sur une matrice ~170×170, instantané),
calculé une fois par fenêtre plutôt que par ligne.
"""

from __future__ import annotations

import numpy as np
import psycopg
from sklearn.decomposition import TruncatedSVD

N_COMPONENTS = 8

# 3 paires internes du trio (mêmes que `features._DUO_PAIRS`, dupliqué ici
# pour ne pas créer de dépendance circulaire embeddings.py <-> features.py).
_DUO_PAIR_TYPES = ("jgl_mid", "jgl_sup", "mid_sup")

PairTable = dict[tuple[str, int, int], tuple[int, int]]


def pair_table_from_agg_duo(agg_duo: dict) -> PairTable:
    """Adapte `agg_duo` (clé (patch, roles, champ_a, champ_b)) vers le
    format générique (patch, min(a,b), max(a,b)) -> (games, wins), sommé
    sur les 3 types de paire internes du trio — source v1."""
    result: dict[tuple[str, int, int], list[int]] = {}
    for (patch, roles, champ_a, champ_b), (games, wins) in agg_duo.items():
        if roles not in _DUO_PAIR_TYPES:
            continue
        key = (patch, min(champ_a, champ_b), max(champ_a, champ_b))
        acc = result.setdefault(key, [0, 0])
        acc[0] += games
        acc[1] += wins
    return {k: (g, w) for k, (g, w) in result.items()}


def fetch_5role_pair_table(conn: psycopg.Connection, patches: list[str]) -> PairTable:
    """Co-occurrence de victoire entre 2 champions de la MÊME équipe, sur les
    5 rôles — source v2, pas seulement les 3 paires internes du trio comme
    `pair_table_from_agg_duo`. Auto-jointure sur (match_id, team_id),
    `champ_b > champ_a` pour ne compter chaque paire qu'une fois (10 paires
    par équipe/match, C(5,2)).

    `match_role_stats`, PAS `match_participants` (retour utilisateur
    2026-09-10, bug trouvé en prod) : `match_participants` ne retient qu'UN
    SEUL patch (`maintenance.PARTICIPANTS_KEEP = 1`), contrairement à
    `matches`/`match_role_stats` (3 patchs, `RAW_KEEP = 3`) — avec
    `match_participants`, le leave-one-patch-out d'un match du patch le
    plus récent (l'immense majorité des lignes) exclut le SEUL patch ayant
    des données, laissant une matrice de co-occurrence vide (embeddings
    constants à zéro, AUC exactement 0,5 en pratique). `match_role_stats`
    couvre les mêmes 5 rôles avec la même rétention à 3 patchs que le reste
    du pipeline."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.patch, pa.champion_id, pb.champion_id,
                   count(*), count(*) FILTER (WHERE pa.win)
            FROM match_role_stats pa
            JOIN match_role_stats pb
                ON pb.match_id = pa.match_id AND pb.team_id = pa.team_id
               AND pb.champion_id > pa.champion_id
            JOIN matches m ON m.match_id = pa.match_id AND m.patch = ANY(%(patches)s)
            GROUP BY m.patch, pa.champion_id, pb.champion_id
            """,
            {"patches": patches},
        )
        return {(patch, a, b): (games, wins) for patch, a, b, games, wins in cur}


def _sum_pair_stats_excluding(
    pair_table: PairTable, patches: list[str], exclude_patch: str
) -> dict[tuple[int, int], tuple[int, int]]:
    """Somme `games`/`wins` sur tous les patchs de la fenêtre SAUF
    `exclude_patch` — même anti-fuite que `features._sum_excluding`."""
    result: dict[tuple[int, int], list[int]] = {}
    for (patch, a, b), (games, wins) in pair_table.items():
        if patch == exclude_patch or patch not in patches:
            continue
        acc = result.setdefault((a, b), [0, 0])
        acc[0] += games
        acc[1] += wins
    return {k: (g, w) for k, (g, w) in result.items()}


def fit_embeddings(
    pair_table: PairTable, patches: list[str], exclude_patch: str, champion_ids: list[int]
) -> dict[int, np.ndarray]:
    """Une matrice de synergie -> SVD -> `{champion_id: vecteur de N_COMPONENTS
    dimensions}`, sur les champions de `champion_ids` uniquement, en excluant
    `exclude_patch` (même anti-fuite que le reste du module)."""
    pair_stats = _sum_pair_stats_excluding(pair_table, patches, exclude_patch)
    n = len(champion_ids)
    index_of = {c: i for i, c in enumerate(champion_ids)}
    matrix = np.zeros((n, n))
    for (a, b), (games, wins) in pair_stats.items():
        if a not in index_of or b not in index_of or games == 0:
            continue
        wr_centered = wins / games - 0.5
        ia, ib = index_of[a], index_of[b]
        matrix[ia, ib] = wr_centered
        matrix[ib, ia] = wr_centered

    n_components = min(N_COMPONENTS, max(1, n - 1))
    svd = TruncatedSVD(n_components=n_components, random_state=42)
    vectors = svd.fit_transform(matrix)
    if n_components < N_COMPONENTS:
        vectors = np.pad(vectors, ((0, 0), (0, N_COMPONENTS - n_components)))
    return {c: vectors[index_of[c]] for c in champion_ids}


def fit_embeddings_per_patch(
    pair_table: PairTable, patches: list[str]
) -> dict[str, dict[int, np.ndarray]]:
    """Un jeu d'embeddings par patch exclu (leave-one-patch-out, comme tout
    le reste du module) — calculé une fois pour la fenêtre entière, réutilisé
    pour chaque ligne de son propre patch."""
    champion_ids = sorted({c for (_, a, b) in pair_table for c in (a, b)})
    return {patch: fit_embeddings(pair_table, patches, patch, champion_ids) for patch in patches}
