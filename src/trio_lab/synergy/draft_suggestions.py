"""Compositions à 5 suggérées par archétype + leurs contres 1v1 (Phase 9,
retour utilisateur 2026-07-25 : suppression du simulateur pick-par-pick,
remplacé par des compositions complètes indépendantes de l'adversaire).

Partie d'un DUO (2 champions précis, `score_duo` — bien plus de données
qu'un trio à 3 champions précis, cf. abandon du counter trio en Phase 4,
CLAUDE.md), puis étend rôle par rôle SANS ordre fixe : à chaque étape, le
(rôle, champion) qui maximise un score composé (Σ poids × z-score) mêlant
synergie ET stats d'archétype (scaling/CC/gold@15/drakes) — `synergy` est un
axe pondéré parmi les autres, pas un cas à part (retour utilisateur
2026-07-25 : "un champion d'un duo fait forcément partie d'un autre duo",
l'archétype doit peser sur CHAQUE champion ajouté, pas seulement le duo de
départ). Plusieurs duos de départ sont essayés et complétés en entier ; la
composition FINIE retenue est celle au meilleur score sur ses 10 VRAIES
paires, pas forcément celle du duo de départ le mieux classé isolément
(retour utilisateur : "est-ce possible de ne pas se baser sur un duo de
départ ?").

Les contres restent toujours du 1v1 PAR RÔLE (`score_matchup`) — jamais un
contre de la draft entière, combinatoirement intraitable (Phase 4 ❌
abandonnée le 2026-07-19). Rôle au meilleur delta disponible = contre
PRIMAIRE (jusqu'à `COUNTER_PRIMARY_PICKS` champions) ; les
`COUNTER_SECONDARY_ROLES` rôles suivants (si notable) : 1 champion chacun.

Module autonome (aucune dépendance à `trio_lab.web`, y compris ses requêtes
SQL déjà écrites côté `web/queries.py`) : utilisé aussi bien par le service
collector 24/24 (`refresh`, matérialise `draft_suggestion(_counter)` pour
`platform="all"`, la région par défaut — retour utilisateur : "je voulais
garder les drafts proposées sans avoir à cliquer") que par `web/app.py` en
calcul à la demande (autres régions, "Compose à partir de tes champions").
Toutes les fonctions travaillent sur des `champion_id` bruts — la résolution
nom/icône reste une préoccupation de rendu, côté `web/app.py`.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import math
from collections.abc import Callable, Iterable

import psycopg
from psycopg.rows import dict_row

from trio_lab import config, db
from trio_lab.synergy.windows import PatchWindow, make_window

logger = logging.getLogger(__name__)

# Les 5 rôles courts (mêmes codes que l'UI /draft) et leurs 10 paires
# possibles — dupliqué de web/app.py à dessein (constantes statiques, pas de
# logique) pour que ce module n'importe jamais `trio_lab.web` : le service
# collector (qui l'utilise via `refresh`) ne doit rien savoir de FastAPI.
DRAFT_ROLES = ("top", "jgl", "mid", "bot", "sup")
DUO_ROLE_KEYS: dict[str, tuple[str, str]] = {
    "jgl_mid": ("jgl", "mid"),
    "jgl_sup": ("jgl", "sup"),
    "mid_sup": ("mid", "sup"),
    "top_jgl": ("top", "jgl"),
    "top_mid": ("top", "mid"),
    "top_bot": ("top", "bot"),
    "top_sup": ("top", "sup"),
    "jgl_bot": ("jgl", "bot"),
    "mid_bot": ("mid", "bot"),
    "bot_sup": ("bot", "sup"),
}
_ROLES_BY_PAIR = {frozenset(v): k for k, v in DUO_ROLE_KEYS.items()}
DRAFT_ROLE_TO_TEAM_POSITION = {
    "top": "TOP",
    "jgl": "JUNGLE",
    "mid": "MIDDLE",
    "bot": "BOTTOM",
    "sup": "UTILITY",
}
_TIER_AT_LEAST = {
    "faible": ("faible", "moyen", "eleve"),
    "moyen": ("moyen", "eleve"),
    "eleve": ("eleve",),
}
_STAT_COLUMNS_SQL = "scaling, cc_blended_pct, gold_diff_15, drakes"

# 4 profils de poids ("archétypes", poids arbitraires mais justifiés, pas de
# test statistique dessus). "synergy" pesé comme les autres axes (100 % pour
# "Meilleure synergie", 30 % pour les 3 profils pondérés — le reste sur
# scaling/CC/gold/drakes, poids d'origine × 0,7, pour ne pas sacrifier la
# synergie pure au profit du profil). Validé sur données réelles avant de
# figer (cf. docs/ROADMAP.md).
ARCHETYPE_STAT_COLUMNS = {
    "synergy": "synergy",
    "scaling": "scaling",
    "cc": "cc_blended_pct",
    "gold": "gold_diff_15",
    "drakes": "drakes",
}
ARCHETYPES: dict[str, dict] = {
    "synergy": {"label": "Meilleure synergie", "weights": {"synergy": 1.0}},
    "scaling": {
        "label": "Scaling / fin de partie",
        "weights": {
            "synergy": 0.30,
            "scaling": 0.385,
            "cc": 0.14,
            "gold": 0.07,
            "drakes": 0.105,
        },
    },
    "early": {
        "label": "Avantage early / lane",
        "weights": {
            "synergy": 0.30,
            "scaling": 0.0,
            "cc": 0.175,
            "gold": 0.315,
            "drakes": 0.21,
        },
    },
    "objectives": {
        "label": "Contrôle des objectifs",
        "weights": {
            "synergy": 0.30,
            "scaling": 0.07,
            "cc": 0.245,
            "gold": 0.07,
            "drakes": 0.315,
        },
    },
}
SEED_SHORTLIST = 8  # duos de départ essayés par profil avant d'abandonner
# Pool de duos candidats : sans plafond explicite, une lecture paginée par
# défaut limiterait aux 50 meilleurs en synergie BRUTE, ce qui empêcherait
# les archétypes non-synergie de considérer un duo hors de ce top-50 (bug
# préexistant côté web, découvert et corrigé le 2026-07-25). 10 000 :
# confortablement au-dessus des ~5867 duos "eleve" actuels.
POOL_SIZE = 10_000
# "eleve" (games_eff ≥ 400), pas "moyen" : un duo a bien plus de volume qu'un
# trio (2 champions précis, pas 3), on peut se permettre d'être exigeant.
MIN_TIER = "eleve"

# Seuils des conseils de jeu — repères arbitraires, pas de test statistique
# dessus, calibrés au niveau DUO (2 champions) sur la vraie distribution
# prod (score_duo fiable, n=25 173).
ADVICE_SCALING_NOTABLE = 0.03
ADVICE_CC_NOTABLE = 40.0
ADVICE_GOLD15_NOTABLE = 350.0

# Contres 1v1 : mêmes seuils que l'ancienne sécurité blind pick (retour
# utilisateur 2026-07-19), vérifiés sur données réelles (16.14+16.13,
# platform=all) : 945 lignes score_matchup passent les 2 filtres.
NOTABLE_COUNTER_DELTA = 0.03
COUNTER_MIN_GAMES_EFF = 50.0
COUNTER_PRIMARY_PICKS = 3
COUNTER_SECONDARY_ROLES = 2


# --- Accès SQL minimal (score_duo/score_matchup) ---
#
# Volontairement PAS `trio_lab.web.queries` (déjà écrites, mêmes requêtes) :
# ce module doit rester importable par le collector sans jamais tirer de
# dépendance web/FastAPI. Duplication limitée à ces 4 fonctions, chacune une
# simple lecture sans pagination/tri paramétrable (contrairement à leurs
# équivalents web, taillés pour l'affichage d'une tier list).


def _duo_pool(conn: psycopg.Connection, window: str, platform: str, min_tier: str) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT roles, champ_a, champ_b, games, games_eff, wr, synergy, tier,
                   {_STAT_COLUMNS_SQL}
            FROM score_duo
            WHERE window_label = %(window)s AND platform = %(platform)s
              AND tier = ANY(%(tiers)s)
            ORDER BY synergy DESC
            LIMIT %(limit)s
            """,
            {
                "window": window,
                "platform": platform,
                "tiers": list(_TIER_AT_LEAST[min_tier]),
                "limit": POOL_SIZE,
            },
        ).fetchall()


def _best_partners(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    roles: str,
    fixed_role: str,
    champion_id: int,
    limit: int,
    min_tier: str,
) -> list[dict]:
    role_a, _role_b = roles.split("_")
    fixed_col, partner_col = (
        ("champ_a", "champ_b") if fixed_role == role_a else ("champ_b", "champ_a")
    )
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT {partner_col} AS partner_champion, games, games_eff, wr, synergy, tier,
                   {_STAT_COLUMNS_SQL}
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


def _duo_score(
    conn: psycopg.Connection, window: str, platform: str, roles: str, champ_a: int, champ_b: int
) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT roles, champ_a, champ_b, games, games_eff, wr, synergy, tier,
                   {_STAT_COLUMNS_SQL}
            FROM score_duo
            WHERE window_label = %s AND platform = %s AND roles = %s
              AND champ_a = %s AND champ_b = %s
            """,
            (window, platform, roles, champ_a, champ_b),
        ).fetchone()


def _matchup_candidates(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    role: str,
    enemy_champion_id: int,
    limit: int,
) -> list[dict]:
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


def _matchup_beats(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    role: str,
    our_champion_id: int,
    limit: int,
) -> list[dict]:
    """Symétrique de `_matchup_candidates` : `champ_a` (notre champion) fixé,
    liste les `champ_b` qu'il bat le mieux (delta DESC) — sert aux points
    FORTS d'une composition (`draft_strengths`), pas ses points faibles."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            """
            SELECT champ_b AS candidate_champion, games, games_eff, wr, delta, tier
            FROM score_matchup
            WHERE window_label = %(window)s AND platform = %(platform)s AND role = %(role)s
              AND champ_a = %(champ)s
            ORDER BY delta DESC
            LIMIT %(limit)s
            """,
            {
                "window": window,
                "platform": platform,
                "role": role,
                "champ": our_champion_id,
                "limit": limit,
            },
        ).fetchall()


# --- Moteur de construction d'une composition ---


def zscore_stats(rows: list[dict], columns: Iterable[str]) -> dict[str, tuple[float, float]]:
    """(moyenne, écart-type) par colonne, sur les lignes où la valeur existe
    (`None` exclu du calcul, pas traité comme 0 — sinon un duo sans donnée de
    scaling semblerait "moyen" sur cet axe au lieu d'être exclu du
    classement pondéré)."""
    stats: dict[str, tuple[float, float]] = {}
    for col in columns:
        vals = [r[col] for r in rows if r.get(col) is not None]
        if len(vals) < 2:
            stats[col] = (0.0, 1.0)
            continue
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        stats[col] = (mean, math.sqrt(var) or 1.0)
    return stats


def pool_and_zstats(
    conn: psycopg.Connection, window: str, platform: str
) -> tuple[list[dict], dict[str, tuple[float, float]]]:
    pool = _duo_pool(conn, window, platform, MIN_TIER)
    zstats = zscore_stats(pool, ARCHETYPE_STAT_COLUMNS.values())
    return pool, zstats


def archetype_seed_order(
    pool: list[dict], weights: dict[str, float], zstats: dict[str, tuple[float, float]]
) -> list[dict]:
    """Ordonne `pool` (duos fiables, 10 paires confondues) pour un archétype
    donné : score = Σ poids × z-score(stat), en excluant les duos sans
    donnée sur un axe pondéré. "Meilleure synergie" (`weights =
    {"synergy": 1.0}`) reproduit le tri par synergie brute (z-score d'un
    seul axe = transformation affine, l'ordre est inchangé)."""
    scored: list[tuple[dict, float]] = []
    for row in pool:
        total = 0.0
        skip = False
        for axis, weight in weights.items():
            if weight == 0:
                continue
            col = ARCHETYPE_STAT_COLUMNS[axis]
            value = row.get(col)
            if value is None:
                skip = True
                break
            mean, std = zstats[col]
            total += weight * (value - mean) / std
        if not skip:
            scored.append((row, total))
    scored.sort(key=lambda rs: -rs[1])
    return [row for row, _ in scored]


def _sum_synergy(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    anchors: list[tuple[str, str, int]],
    min_tier: str,
    stat_columns: Iterable[str] = (),
) -> dict[int, dict]:
    """Σ synergie d'un candidat contre chaque ancrage (rôle, champion déjà
    posé) de `anchors` — ne garde que les candidats couverts par TOUS les
    ancrages (fiabilité ≥ `min_tier` pour chacun). `stat_columns` : en plus
    de la Σ synergie, moyenne de chaque colonne sur les paires nouvellement
    formées où elle est renseignée. Retourne, par candidat couvert :
    `{"synergy_sum": ..., "stats": {col: moyenne}}`."""
    totals: dict[int, float] = {}
    coverage: dict[int, int] = {}
    stat_totals: dict[int, dict[str, float]] = {}
    stat_counts: dict[int, dict[str, int]] = {}
    for roles, fixed_role, champion_id in anchors:
        partners = _best_partners(
            conn, window, platform, roles, fixed_role, champion_id, 500, min_tier
        )
        for row in partners:
            cid = row["partner_champion"]
            totals[cid] = totals.get(cid, 0.0) + row["synergy"]
            coverage[cid] = coverage.get(cid, 0) + 1
            for col in stat_columns:
                value = row.get(col)
                if value is None:
                    continue
                stat_totals.setdefault(cid, {})
                stat_totals[cid][col] = stat_totals[cid].get(col, 0.0) + value
                stat_counts.setdefault(cid, {})
                stat_counts[cid][col] = stat_counts[cid].get(col, 0) + 1
    n = len(anchors)
    result: dict[int, dict] = {}
    for cid, total in totals.items():
        if coverage[cid] != n:
            continue
        stats = {
            col: stat_totals[cid][col] / stat_counts[cid][col] for col in stat_counts.get(cid, {})
        }
        result[cid] = {"synergy_sum": total, "stats": stats}
    return result


def greedy_complete_draft(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    placed: dict[str, int],
    total: float,
    min_tier: str,
    weights: dict[str, float],
    zstats: dict[str, tuple[float, float]],
) -> tuple[dict[str, int], float] | None:
    """Étend un état de départ PARTIEL (`placed`, 1 à 4 rôles déjà posés,
    `total` = Σ synergie déjà couverte par ces paires) en un draft à 5 rôles
    : à chaque étape, ajoute le (rôle, champion) qui maximise le score
    pondéré de l'archétype (Σ poids × z-score(moyenne des NOUVELLES paires
    formées avec tout ce qui est déjà posé)), `synergy` étant un axe parmi
    les autres — un champion touche jusqu'à 4 paires sur un draft à 5,
    l'archétype doit peser sur chacune, pas seulement sur le point de départ.
    `None` si un rôle ne peut pas être complété avec une couverture fiable
    complète. Générique sur la taille de `placed` (1 à 5) : réutilisé aussi
    bien pour étendre un duo de départ auto-suggéré que des champions
    choisis à la main."""
    placed = dict(placed)  # ne jamais muter le dict de l'appelant
    remaining = [r for r in DRAFT_ROLES if r not in placed]
    stat_columns = [
        col
        for axis, col in ARCHETYPE_STAT_COLUMNS.items()
        if col != "synergy" and weights.get(axis, 0)
    ]
    while remaining:
        best: tuple[str, int, float, float] | None = None  # role, cid, synergy_sum, combined_z
        for role in remaining:
            anchors = [
                (_ROLES_BY_PAIR[frozenset({role, placed_role})], placed_role, placed_champ)
                for placed_role, placed_champ in placed.items()
            ]
            scores = _sum_synergy(conn, window, platform, anchors, min_tier, stat_columns)
            if not scores:
                continue
            n_anchors = len(anchors)
            role_best: tuple[int, float, float] | None = None  # cid, synergy_sum, combined_z
            for cid, data in scores.items():
                combined = 0.0
                skip = False
                for axis, weight in weights.items():
                    if weight == 0:
                        continue
                    col = ARCHETYPE_STAT_COLUMNS[axis]
                    value = (
                        data["synergy_sum"] / n_anchors
                        if col == "synergy"
                        else data["stats"].get(col)
                    )
                    if value is None:
                        skip = True
                        break
                    mean, std = zstats[col]
                    combined += weight * (value - mean) / std
                if skip:
                    continue
                if role_best is None or combined > role_best[2]:
                    role_best = (cid, data["synergy_sum"], combined)
            if role_best is None:
                continue
            cid, synergy_sum, combined = role_best
            if best is None or combined > best[3]:
                best = (role, cid, synergy_sum, combined)
        if best is None:
            return None
        role, cid, synergy_sum, _ = best
        placed[role] = cid
        total += synergy_sum
        remaining.remove(role)
    return placed, total


def full_draft_stat_averages(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    placed: dict[str, int],
    columns: Iterable[str],
) -> dict[str, float] | None:
    """Moyenne de chaque colonne de `columns` sur les 10 VRAIES paires d'un
    draft complet `placed`. `None` si une paire manque ou une valeur sur un
    axe demandé — jamais bloquant côté appelant (juste "pas de conseils"),
    peut arriver pour un draft parti de champions choisis à la main dont une
    paire n'a aucune donnée."""
    totals: dict[str, float] = {col: 0.0 for col in columns}
    for role_x, role_y in itertools.combinations(DRAFT_ROLES, 2):
        roles_str = _ROLES_BY_PAIR[frozenset({role_x, role_y})]
        role_a, role_b = DUO_ROLE_KEYS[roles_str]
        row = _duo_score(conn, window, platform, roles_str, placed[role_a], placed[role_b])
        if row is None:
            return None
        for col in totals:
            value = row.get(col)
            if value is None:
                return None
            totals[col] += value
    n_pairs = len(DRAFT_ROLES) * (len(DRAFT_ROLES) - 1) // 2
    return {col: total / n_pairs for col, total in totals.items()}


def full_draft_score(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    placed: dict[str, int],
    weights: dict[str, float],
    zstats: dict[str, tuple[float, float]],
) -> float | None:
    """Score composite (Σ poids × z-score) sur les 10 VRAIES paires d'un
    draft complet `placed` — sert à comparer plusieurs compositions
    candidates FINIES entre elles pour un même archétype, plutôt que de
    garder la première qui complète."""
    columns = [ARCHETYPE_STAT_COLUMNS[axis] for axis, weight in weights.items() if weight]
    averages = full_draft_stat_averages(conn, window, platform, placed, columns)
    if averages is None:
        return None
    combined = 0.0
    for axis, weight in weights.items():
        if not weight:
            continue
        col = ARCHETYPE_STAT_COLUMNS[axis]
        mean, std = zstats[col]
        combined += weight * (averages[col] - mean) / std
    return combined


def seed_from_champions(
    conn: psycopg.Connection, window: str, platform: str, picks: dict[str, int]
) -> tuple[dict[str, int], float, list[dict]]:
    """`picks` : 1 à 5 (rôle → champion) choisis à la main. Calcule le total
    de synergie déjà couvert par les paires FORMÉES ENTRE CES CHAMPIONS
    (`score_duo`) et le détail de fiabilité de chacune — JAMAIS bloquant :
    une paire jamais jouée ensemble contribue 0 à la synergie et affiche une
    fiabilité `tier=None`, elle n'empêche pas de continuer (contrairement
    aux rôles que le système complète ensuite, filtrés par `MIN_TIER`).
    Retourne `(placed, total_synergy, seed_pairs)` — `seed_pairs` : une
    entrée par paire déjà posée, `{role_a, role_b, champ_a, champ_b,
    synergy, games, tier}` (synergy/tier `None`, games 0 si jamais jouée
    ensemble)."""
    pairs: list[dict] = []
    total = 0.0
    for role_x, role_y in itertools.combinations(picks, 2):
        roles_str = _ROLES_BY_PAIR[frozenset({role_x, role_y})]
        role_a, role_b = DUO_ROLE_KEYS[roles_str]
        row = _duo_score(conn, window, platform, roles_str, picks[role_a], picks[role_b])
        pairs.append(
            {
                "role_a": role_a,
                "role_b": role_b,
                "champ_a": picks[role_a],
                "champ_b": picks[role_b],
                "synergy": row["synergy"] if row else None,
                "games": row["games"] if row else 0,
                "tier": row["tier"] if row else None,
            }
        )
        if row is not None:
            total += row["synergy"]
    return dict(picks), total, pairs


def draft_advice(
    scaling: float | None, cc_blended_pct: float | None, gold_diff_15: float | None
) -> list[str]:
    """Conseils de jeu dérivés des stats moyennes du draft COMPLET (jamais un
    nouveau calcul, juste traduit en phrase). Jamais plus d'1 conseil par
    thème (pacing/CC/économie), aucun si le signal est trop proche de zéro
    pour être notable."""
    tips: list[str] = []
    if scaling is not None and scaling > ADVICE_SCALING_NOTABLE:
        tips.append(
            "Composition qui monte en puissance avec la durée de la game : évitez les "
            "combats forcés tôt, cherchez à faire durer."
        )
    elif scaling is not None and scaling < -ADVICE_SCALING_NOTABLE:
        tips.append(
            "Composition plus forte tôt que tard : cherchez à conclure avant que "
            "l'adversaire ne monte en puissance."
        )
    if cc_blended_pct is not None and cc_blended_pct >= ADVICE_CC_NOTABLE:
        tips.append(
            "Bon profil de contrôle de foule : cherchez à engager les combats groupés "
            "plutôt qu'à les éviter."
        )
    if gold_diff_15 is not None and gold_diff_15 > ADVICE_GOLD15_NOTABLE:
        tips.append(
            "Avantage économique attendu tôt en lane : jouez agressif dans les 15 "
            "premières minutes."
        )
    elif gold_diff_15 is not None and gold_diff_15 < -ADVICE_GOLD15_NOTABLE:
        tips.append(
            "Léger déficit économique attendu : jouez prudent en lane, cherchez votre "
            "impact ailleurs (jungle, objectifs)."
        )
    return tips


def _rank_matchup_picks(
    placed: dict[str, int], fetch: Callable[[str, int], list[dict]]
) -> dict | None:
    """Commun à `draft_counters` (points faibles) et `draft_strengths`
    (points forts) : classe les 5 rôles par delta décroissant à partir de
    `fetch(role, champion_id) -> lignes score_matchup` (l'appelant choisit
    le sens du 1v1 — `_matchup_candidates` ou `_matchup_beats`). Le rôle au
    MEILLEUR delta devient PRIMAIRE (jusqu'à `COUNTER_PRIMARY_PICKS`
    champions) ; les `COUNTER_SECONDARY_ROLES` rôles suivants (si notable) :
    1 champion chacun. `None` si rien de notable nulle part."""
    by_role: dict[str, list[dict]] = {}
    for role in DRAFT_ROLES:
        candidates = fetch(role, placed[role])
        notable = [
            c
            for c in candidates
            if c["delta"] >= NOTABLE_COUNTER_DELTA and c["games_eff"] >= COUNTER_MIN_GAMES_EFF
        ]
        if notable:
            by_role[role] = notable
    if not by_role:
        return None
    ranked_roles = sorted(by_role, key=lambda r: -by_role[r][0]["delta"])

    def _fmt(c: dict) -> dict:
        # games_eff sert seulement au filtre ci-dessus (COUNTER_MIN_GAMES_EFF),
        # jamais affiché — pas stocké dans le résultat/la table matérialisée.
        return {"champion_id": c["candidate_champion"], "delta": c["delta"]}

    primary_role = ranked_roles[0]
    return {
        "primary": {
            "role": primary_role,
            "against_champion": placed[primary_role],
            "picks": [_fmt(c) for c in by_role[primary_role][:COUNTER_PRIMARY_PICKS]],
        },
        "secondary": [
            {
                "role": role,
                "against_champion": placed[role],
                **_fmt(by_role[role][0]),
            }
            for role in ranked_roles[1 : 1 + COUNTER_SECONDARY_ROLES]
        ],
    }


def draft_counters(
    conn: psycopg.Connection, window: str, platform: str, placed: dict[str, int]
) -> dict | None:
    """Points FAIBLES 1v1 (`score_matchup`) d'une composition COMPLÈTE à 5 —
    jamais de counter trio/5v5 en bloc (combinatoirement intraitable,
    abandonné en Phase 4, cf. CLAUDE.md) : uniquement des deltas 1v1 par
    rôle. `None` si rien de notable nulle part (composition solide de
    partout)."""
    return _rank_matchup_picks(
        placed,
        lambda role, champ: _matchup_candidates(
            conn, window, platform, DRAFT_ROLE_TO_TEAM_POSITION[role], champ, 50
        ),
    )


def draft_strengths(
    conn: psycopg.Connection, window: str, platform: str, placed: dict[str, int]
) -> dict | None:
    """Points FORTS 1v1 (`score_matchup`) d'une composition COMPLÈTE à 5 —
    symétrique de `draft_counters` (retour utilisateur 2026-07-26 : "en plus
    du contre, contre qui cette composition est forte ?") : les champions
    adverses que CETTE composition bat le mieux, même critère de
    fiabilité/notabilité. `None` si rien de notable nulle part."""
    return _rank_matchup_picks(
        placed,
        lambda role, champ: _matchup_beats(
            conn, window, platform, DRAFT_ROLE_TO_TEAM_POSITION[role], champ, 50
        ),
    )


def propose_drafts(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    pool: list[dict],
    zstats: dict[str, tuple[float, float]],
) -> list[dict]:
    """Une composition par archétype de `ARCHETYPES` (jamais un archétype
    sans composition silencieusement omis : il n'apparaît juste pas). Essaie
    TOUS les duos de départ du short-list (pas seulement le premier qui
    complète) et garde la composition FINIE dont le score archétype sur ses
    10 vraies paires (`full_draft_score`) est le meilleur.

    Retourne, par archétype réussi : `{archetype, label, members (dict
    rôle→champion_id), total_synergy, seed_pairs (1 entrée, le duo de
    départ), advice_stats (moyennes scaling/cc/gold sur les 10 vraies
    paires, ou None), counters (points faibles), strengths (points forts)}`
    — brut (`champion_id`, pas de nom/icône), même forme que lue depuis
    `draft_suggestion(_counter)` matérialisées par `refresh`, pour que le
    rendu (`web/app.py`) traite les 2 sources de façon identique."""
    results: list[dict] = []
    for key, archetype in ARCHETYPES.items():
        weights = archetype["weights"]
        seeds = archetype_seed_order(pool, weights, zstats)
        candidates: list[tuple[float, dict, dict, float]] = []
        for seed_row in seeds[:SEED_SHORTLIST]:
            role_a, role_b = DUO_ROLE_KEYS[seed_row["roles"]]
            seed_placed = {role_a: seed_row["champ_a"], role_b: seed_row["champ_b"]}
            completed = greedy_complete_draft(
                conn, window, platform, seed_placed, seed_row["synergy"], MIN_TIER, weights, zstats
            )
            if completed is None:
                continue
            placed, total = completed
            score = full_draft_score(conn, window, platform, placed, weights, zstats)
            if score is None:
                continue
            candidates.append((score, seed_row, placed, total))
        if not candidates:
            continue
        _, seed_row, placed, total = max(candidates, key=lambda c: c[0])
        role_a, role_b = DUO_ROLE_KEYS[seed_row["roles"]]
        results.append(
            {
                "archetype": key,
                "label": archetype["label"],
                "members": placed,
                "total_synergy": total,
                "seed_pairs": [
                    {
                        "role_a": role_a,
                        "role_b": role_b,
                        "champ_a": seed_row["champ_a"],
                        "champ_b": seed_row["champ_b"],
                        "synergy": seed_row["synergy"],
                        "games": seed_row["games"],
                        "tier": seed_row["tier"],
                    }
                ],
                "advice_stats": full_draft_stat_averages(
                    conn, window, platform, placed, ("scaling", "cc_blended_pct", "gold_diff_15")
                ),
                "counters": draft_counters(conn, window, platform, placed),
                "strengths": draft_strengths(conn, window, platform, placed),
            }
        )
    return results


# --- Matérialisation (service 24/24) ---

_INSERT_SUGGESTION_SQL = """
    INSERT INTO draft_suggestion
        (window_label, platform, archetype, label,
         top_champion, jgl_champion, mid_champion, bot_champion, sup_champion,
         total_synergy, seed_roles, seed_champ_a, seed_champ_b, seed_synergy,
         seed_games, seed_tier, advice_scaling, advice_cc, advice_gold15)
    VALUES
        (%(window_label)s, %(platform)s, %(archetype)s, %(label)s,
         %(top_champion)s, %(jgl_champion)s, %(mid_champion)s, %(bot_champion)s, %(sup_champion)s,
         %(total_synergy)s, %(seed_roles)s, %(seed_champ_a)s, %(seed_champ_b)s, %(seed_synergy)s,
         %(seed_games)s, %(seed_tier)s, %(advice_scaling)s, %(advice_cc)s, %(advice_gold15)s)
"""
_INSERT_COUNTER_SQL = """
    INSERT INTO draft_suggestion_counter
        (window_label, platform, archetype, direction, kind, rank, role,
         against_champion, champion_id, delta)
    VALUES
        (%(window_label)s, %(platform)s, %(archetype)s, %(direction)s, %(kind)s, %(rank)s,
         %(role)s, %(against_champion)s, %(champion_id)s, %(delta)s)
"""


def _write_matchup_picks(
    cur, window: PatchWindow, platform: str, archetype: str, direction: str, picks: dict | None
) -> None:
    """Écrit un bloc primaire/secondaire (`draft_counters`/`draft_strengths`)
    dans `draft_suggestion_counter` — `direction` distingue points
    faibles/forts, même schéma pour les 2 (migration 034). No-op si `picks`
    est `None` (rien de notable, cf. `_rank_matchup_picks`)."""
    if picks is None:
        return
    primary = picks["primary"]
    for rank, pick in enumerate(primary["picks"]):
        cur.execute(
            _INSERT_COUNTER_SQL,
            {
                "window_label": window.label,
                "platform": platform,
                "archetype": archetype,
                "direction": direction,
                "kind": "primary",
                "rank": rank,
                "role": primary["role"],
                "against_champion": primary["against_champion"],
                "champion_id": pick["champion_id"],
                "delta": pick["delta"],
            },
        )
    for rank, sec in enumerate(picks["secondary"]):
        cur.execute(
            _INSERT_COUNTER_SQL,
            {
                "window_label": window.label,
                "platform": platform,
                "archetype": archetype,
                "direction": direction,
                "kind": "secondary",
                "rank": rank,
                "role": sec["role"],
                "against_champion": sec["against_champion"],
                "champion_id": sec["champion_id"],
                "delta": sec["delta"],
            },
        )


def refresh(window: PatchWindow, platform: str, *, dsn: str | None = None) -> int:
    """Matérialise les compositions suggérées de `platform` dans
    `draft_suggestion(_counter)`. Retourne le nombre d'archétypes écrits
    (0 à 4). DELETE + INSERT (même raisonnement que
    `resilience`/`win_factors`/`gold_factors`)."""
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        pool, zstats = pool_and_zstats(conn, window.label, platform)
        drafts = propose_drafts(conn, window.label, platform, pool, zstats)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "DELETE FROM draft_suggestion WHERE window_label = %s AND platform = %s",
                (window.label, platform),
            )
            for d in drafts:
                seed = d["seed_pairs"][0]
                stats = d["advice_stats"] or {}
                cur.execute(
                    _INSERT_SUGGESTION_SQL,
                    {
                        "window_label": window.label,
                        "platform": platform,
                        "archetype": d["archetype"],
                        "label": d["label"],
                        "top_champion": d["members"]["top"],
                        "jgl_champion": d["members"]["jgl"],
                        "mid_champion": d["members"]["mid"],
                        "bot_champion": d["members"]["bot"],
                        "sup_champion": d["members"]["sup"],
                        "total_synergy": d["total_synergy"],
                        "seed_roles": _ROLES_BY_PAIR[frozenset({seed["role_a"], seed["role_b"]})],
                        "seed_champ_a": seed["champ_a"],
                        "seed_champ_b": seed["champ_b"],
                        "seed_synergy": seed["synergy"],
                        "seed_games": seed["games"],
                        "seed_tier": seed["tier"],
                        "advice_scaling": stats.get("scaling"),
                        "advice_cc": stats.get("cc_blended_pct"),
                        "advice_gold15": stats.get("gold_diff_15"),
                    },
                )
                _write_matchup_picks(
                    cur, window, platform, d["archetype"], "weakness", d["counters"]
                )
                _write_matchup_picks(
                    cur, window, platform, d["archetype"], "strength", d["strengths"]
                )
    logger.info(
        "draft_suggestions %s/%s rafraîchies : %d archétype(s)", window.label, platform, len(drafts)
    )
    return len(drafts)


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.synergy.draft_suggestions", description=__doc__)
    parser.add_argument(
        "--patches", required=True, help="fenêtre, du plus récent au plus ancien, ex. 16.14,16.13"
    )
    parser.add_argument("--platform", default="all")
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    window = make_window([p.strip() for p in args.patches.split(",") if p.strip()])
    refresh(window, args.platform)


if __name__ == "__main__":
    main()
