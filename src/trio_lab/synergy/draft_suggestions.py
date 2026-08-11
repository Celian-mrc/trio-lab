"""Compositions à 5 suggérées par archétype + leurs contres 1v1 (Phase 9,
retour utilisateur 2026-07-25 : suppression du simulateur pick-par-pick,
remplacé par des compositions complètes indépendantes de l'adversaire).

Partie d'un DUO (2 champions précis, `score_duo` — bien plus de données
qu'un trio à 3 champions précis, cf. abandon du counter trio en Phase 4,
CLAUDE.md), puis étend rôle par rôle SANS ordre fixe : à chaque étape, le
(rôle, champion) qui maximise un score composé (Σ poids × z-score) mêlant
synergie ET stats d'archétype (scaling/CC/gold@15/drakes/âme) — `synergy` est un
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
_STAT_COLUMNS_SQL = (
    "scaling, cc_blended_pct, gold_diff_15, drakes, soul_rate, range_theoretical_pct"
)

# 6 profils de poids ("archétypes", poids arbitraires mais justifiés, pas de
# test statistique dessus). "synergy" pesé comme les autres axes (100 % pour
# "Meilleure synergie" et "Meilleur winrate", chacun sur un seul axe brut,
# 30 % pour les 4 profils pondérés — le reste sur scaling/CC/gold/drakes/
# âme/portée, poids d'origine × 0,7, pour ne pas sacrifier la synergie pure
# au profit du profil). Validé sur données réelles avant de figer (cf.
# docs/ROADMAP.md).
ARCHETYPE_STAT_COLUMNS = {
    "synergy": "synergy",
    "scaling": "scaling",
    "cc": "cc_blended_pct",
    "gold": "gold_diff_15",
    "drakes": "drakes",
    "soul": "soul_rate",
    # Retour utilisateur 2026-07-28 : "compos poke avec de la range" — score
    # de portée théorique (`rangeref/`), 100% théorique (aucune stat Riot ne
    # mesure la distance de poke réelle en jeu, contrairement au CC).
    "range": "range_theoretical_pct",
    # Retour utilisateur 2026-07-28 ("le chemin inverse... la meilleure compo
    # avec le meilleur winrate") : `wr` était déjà sélectionné dans
    # `_duo_pool`/`_best_partners` (colonne toujours présente, pas dans
    # `_STAT_COLUMNS_SQL`) mais jamais utilisable comme axe de pondération —
    # aucun changement SQL nécessaire pour l'exposer ici.
    "wr": "wr",
}
ARCHETYPES: dict[str, dict] = {
    "synergy": {"label": "Meilleure synergie", "weights": {"synergy": 1.0}},
    # Retour utilisateur 2026-07-28 ("le chemin inverse... la meilleure compo
    # avec le meilleur winrate") : même construction que "Meilleure
    # synergie" (poids 1.0 sur un seul axe = tri par valeur brute, le
    # z-score étant une transformation affine qui préserve l'ordre) —
    # winrate MOYEN sur les 10 vraies paires, pas une prédiction du draft
    # entier (aucun modèle de ce type dans le projet, cf. win_factors.py qui
    # prédit à partir du gold d'ÉQUIPE, pas d'une composition).
    "winrate": {"label": "Meilleur winrate", "weights": {"wr": 1.0}},
    "scaling": {
        "label": "Scaling / fin de partie",
        "weights": {
            "synergy": 0.30,
            "scaling": 0.385,
            "cc": 0.14,
            "gold": 0.07,
            # `soul` (taux d'obtention de l'âme, retour utilisateur
            # 2026-07-27) plutôt que `drakes` (taux brut sur toute la
            # partie, sans lien avec "fin de partie") : l'âme suppose 4
            # drakes non-elder cumulés, signal propre de fermeture de game
            # longue — poids repris tel quel de `drakes` (0.105), substitué
            # sans nouveau calibrage.
            "soul": 0.105,
        },
    },
    "early": {
        "label": "Avantage early / lane",
        "weights": {
            "synergy": 0.30,
            "scaling": 0.0,
            "cc": 0.175,
            # `drakes` abaissé (retour utilisateur 2026-07-27) : c'est un
            # taux calculé sur TOUTE la partie (aucune coupure temporelle),
            # pas un signal spécifiquement précoce — pesait presque autant
            # que dans "Contrôle des objectifs" (0.21 vs 0.315), diluant la
            # distinction entre les 2 profils. Ramené au niveau "axe
            # secondaire" (0.07, même niveau que gold/scaling dans les
            # profils où ils ne sont pas l'identité) ; le delta (0.14)
            # reporté sur `gold` (0.315 → 0.455), l'axe qui EST l'identité
            # "early" (gold@15).
            "gold": 0.455,
            "drakes": 0.07,
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
    "range": {
        "label": "Poke / zone",
        "weights": {
            "synergy": 0.30,
            # Identité de l'archétype (retour utilisateur 2026-07-28) : score
            # de portée théorique dominant, `scaling` à 0 (le poke n'est pas
            # une identité "fin de partie" — le chip damage à distance
            # s'exerce tôt/milieu de partie, le mélanger avec le scaling
            # diluerait l'identité, même raisonnement que "early"). Abaissé
            # de 0.40 à 0.34 (retour utilisateur 2026-07-28 : "40% ça me
            # paraît un peu trop élevé") — contrairement aux autres axes
            # (gold/drakes/cc, mesurés en jeu), `range_theoretical_pct` est
            # 100% théorique, jamais recalé par le comportement réel des
            # joueurs (cf. rangeref/score.py) ; un poids dominant mais moins
            # écrasant que "early" (gold à 0.455) reflète cette confiance
            # moindre. Delta (0.06) reporté également sur cc/gold/drakes.
            "range": 0.34,
            "cc": 0.12,  # peel pour protéger les champions à distance
            "gold": 0.12,  # une guerre de poke gagnée se traduit en gold
            "drakes": 0.12,  # contrôle de zone autour des objectifs
            "scaling": 0.0,
        },
    },
}
# Poids par défaut de "Contre cette équipe" (`propose_counter_draft`, retour
# utilisateur 2026-08-11) : matchup 40 % (l'identité du mode — contrer les
# picks scoutés), synergy 30 % (mêmes proportions que les 4 profils
# pondérés d'ARCHETYPES, pour que les contres restent des picks qui
# fonctionnent ENSEMBLE, pas juste 5 counters isolés), 30 % restants
# répartis à égalité sur scaling/cc/gold/drakes (pas d'identité de phase de
# jeu particulière ici, contrairement aux profils ci-dessus). "matchup"
# n'est PAS une clé d'ARCHETYPE_STAT_COLUMNS : extrait à part avant de
# passer le reste à `propose_for_weights`, cf. `propose_counter_draft`.
DEFAULT_COUNTER_WEIGHTS = {
    "matchup": 0.40,
    "synergy": 0.30,
    "scaling": 0.075,
    "cc": 0.075,
    "gold": 0.075,
    "drakes": 0.075,
}
SEED_SHORTLIST = 8  # duos de départ essayés par profil avant d'abandonner
# 3 propositions par archétype sur "Compositions suggérées" (retour
# utilisateur 2026-07-27) : rang 0 = meilleur score, rang 1 = 2e meilleur
# score suffisamment DIFFÉRENT du rang 0, rang 2 = la plus FIABLE (games_eff
# MINIMUM sur ses 10 vraies paires — pas seulement le duo de départ, retour
# utilisateur 2026-07-28 — et pas le score) parmi les candidats restants
# encore suffisamment différents des rangs 0/1. "Suffisamment différent" =
# au plus DIVERSITY_MAX_SHARED_CHAMPIONS champions communs
# (peu importe le rôle) — 3 sur 5 tolère un changement d'1-2 champions,
# exclut les quasi-doublons (4-5 champions identiques) que produirait sinon
# un pur classement par score (des seeds différents convergent souvent vers
# la même fin de complétion gloutonne).
DIVERSITY_MAX_SHARED_CHAMPIONS = 3
# Pool de duos candidats : sans plafond explicite, une lecture paginée par
# défaut limiterait aux 50 meilleurs en synergie BRUTE, ce qui empêcherait
# les archétypes non-synergie de considérer un duo hors de ce top-50 (bug
# préexistant côté web, découvert et corrigé le 2026-07-25). 10 000 :
# confortablement au-dessus des ~5867 duos "eleve" actuels.
POOL_SIZE = 10_000
# Seuil de fiabilité par défaut pour "Compositions suggérées" (jamais choisi
# par l'utilisateur, précalculé) — anciennement le tier "eleve" (games_eff ≥
# 400). Retour utilisateur 2026-07-28 : "Compose à partir de tes champions"
# et "Personnalise tes poids" laissent désormais l'utilisateur choisir un
# NOMBRE de games réel (`min_games`, même unité que le filtre `/tierlist` et
# `/duos` — plus lisible qu'un tier faible/moyen/élevé) plutôt qu'un tier
# fixe. Un duo a bien plus de volume qu'un trio (2 champions précis, pas 3),
# on peut se permettre d'être exigeant par défaut.
MIN_GAMES_DEFAULT = 400

# Seuils des conseils de jeu — repères arbitraires, pas de test statistique
# dessus, calibrés au niveau DUO (2 champions) sur la vraie distribution
# prod (score_duo fiable, n=25 173).
ADVICE_SCALING_NOTABLE = 0.03
ADVICE_CC_NOTABLE = 40.0
ADVICE_GOLD15_NOTABLE = 350.0
# Colonnes moyennées sur les 10 vraies paires d'un draft complet
# (`full_draft_stat_averages`) pour l'affichage : scaling/CC/gold servent
# aux conseils de jeu (`draft_advice`), wr/ci_low/ci_high au winrate +
# intervalle de confiance affiché à côté de la synergie totale (retour
# utilisateur 2026-07-27) — moyenne simple sur les 10 paires, pas une
# combinaison statistique rigoureuse des IC, même esprit que les autres
# stats affichées ici.
DISPLAY_STAT_COLUMNS = ("scaling", "cc_blended_pct", "gold_diff_15", "wr", "ci_low", "ci_high")

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


def _duo_pool(conn: psycopg.Connection, window: str, platform: str, min_games: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT roles, champ_a, champ_b, games, games_eff, wr, synergy, tier,
                   {_STAT_COLUMNS_SQL}
            FROM score_duo
            WHERE window_label = %(window)s AND platform = %(platform)s
              AND games >= %(min_games)s
            ORDER BY synergy DESC
            LIMIT %(limit)s
            """,
            {
                "window": window,
                "platform": platform,
                "min_games": min_games,
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
    min_games: int,
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
              AND {fixed_col} = %(champ)s AND games >= %(min_games)s
            ORDER BY synergy DESC, games DESC
            LIMIT %(limit)s
            """,
            {
                "window": window,
                "platform": platform,
                "roles": roles,
                "champ": champion_id,
                "limit": limit,
                "min_games": min_games,
            },
        ).fetchall()


def _duo_score(
    conn: psycopg.Connection, window: str, platform: str, roles: str, champ_a: int, champ_b: int
) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"""
            SELECT roles, champ_a, champ_b, games, games_eff, wr, ci_low, ci_high, synergy, tier,
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


def _load_matchup_by_role(
    conn: psycopg.Connection, window: str, platform: str, enemy: dict[str, int]
) -> dict[str, tuple[dict[int, float], tuple[float, float]]]:
    """Pour chaque rôle SCOUTÉ de `enemy` (rôle court → champion adverse,
    scouting partiel autorisé, 0 à 5 entrées) : les deltas `score_matchup`
    de tous les candidats connus contre ce pick, plus leurs (moyenne,
    écart-type) pour le z-score — calculé UNE FOIS par rôle ici, pas par
    candidat à chaque étape de `greedy_complete_draft`/`refine_draft`
    (mode "Contre cette équipe", retour utilisateur 2026-08-11).

    Rôle absent du résultat (scouting non fourni, ou moins de 2 lignes
    `score_matchup` pour un z-score fiable) : `_combined_score`/
    `_matchup_avg_z` ignorent simplement cet axe pour ce rôle, jamais
    d'exclusion du candidat."""
    by_role: dict[str, tuple[dict[int, float], tuple[float, float]]] = {}
    for role, enemy_champion_id in enemy.items():
        team_position = DRAFT_ROLE_TO_TEAM_POSITION[role]
        rows = _matchup_candidates(
            conn, window, platform, team_position, enemy_champion_id, POOL_SIZE
        )
        if len(rows) < 2:
            continue
        lookup = {r["candidate_champion"]: r["delta"] for r in rows}
        zstats = zscore_stats(rows, ("delta",))["delta"]
        by_role[role] = (lookup, zstats)
    return by_role


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
    conn: psycopg.Connection, window: str, platform: str, min_games: int = MIN_GAMES_DEFAULT
) -> tuple[list[dict], dict[str, tuple[float, float]]]:
    pool = _duo_pool(conn, window, platform, min_games)
    zstats = zscore_stats(pool, ARCHETYPE_STAT_COLUMNS.values())
    return pool, zstats


def archetype_seed_order(
    pool: list[dict],
    weights: dict[str, float],
    zstats: dict[str, tuple[float, float]],
    *,
    matchup_weight: float = 0.0,
    matchup_by_role: dict[str, tuple[dict[int, float], tuple[float, float]]] | None = None,
) -> list[dict]:
    """Ordonne `pool` (duos fiables, 10 paires confondues) pour un archétype
    donné : score = Σ poids × z-score(stat), en excluant les duos sans
    donnée sur un axe pondéré. "Meilleure synergie" (`weights =
    {"synergy": 1.0}`) reproduit le tri par synergie brute (z-score d'un
    seul axe = transformation affine, l'ordre est inchangé).

    `matchup_weight`/`matchup_by_role` (mode "Contre cette équipe") : un duo
    de départ occupe 2 rôles — le score matchup ajouté est la somme des
    deltas (z-scorés par rôle) des DEUX champions du duo sur les rôles
    scoutés, chacun ignoré individuellement si son rôle n'est pas scouté
    (jamais d'exclusion, cf. `_combined_score`)."""
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
        if skip:
            continue
        if matchup_weight and matchup_by_role:
            role_a, role_b = DUO_ROLE_KEYS[row["roles"]]
            for role, champ in ((role_a, row["champ_a"]), (role_b, row["champ_b"])):
                entry = matchup_by_role.get(role)
                if entry is None:
                    continue
                lookup, (mean, std) = entry
                delta = lookup.get(champ)
                if delta is not None:
                    total += matchup_weight * (delta - mean) / std
        scored.append((row, total))
    scored.sort(key=lambda rs: -rs[1])
    return [row for row, _ in scored]


def _sum_synergy(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    anchors: list[tuple[str, str, int]],
    min_games: int,
    stat_columns: Iterable[str] = (),
    *,
    matchup_lookup: dict[int, float] | None = None,
) -> dict[int, dict]:
    """Σ synergie d'un candidat contre chaque ancrage (rôle, champion déjà
    posé) de `anchors` — ne garde que les candidats couverts par TOUS les
    ancrages (fiabilité ≥ `min_games` pour chacun). `stat_columns` : en plus
    de la Σ synergie, moyenne de chaque colonne sur les paires nouvellement
    formées où elle est renseignée. `matchup_lookup` (mode "Contre cette
    équipe", retour utilisateur 2026-08-11) : delta `score_matchup` du
    candidat contre le pick adverse du RÔLE EN COURS (pas un ancrage — le
    matchup ne dépend que du rôle qu'on remplit, pas de ce qui est déjà
    posé) ; absent du dict ou lookup `None` → `matchup_delta` reste `None`,
    l'axe est ignoré pour ce candidat SANS l'exclure (cf. `_combined_score`).
    Retourne, par candidat couvert : `{"synergy_sum": ..., "stats": {col:
    moyenne}, "matchup_delta": ...}`."""
    totals: dict[int, float] = {}
    coverage: dict[int, int] = {}
    stat_totals: dict[int, dict[str, float]] = {}
    stat_counts: dict[int, dict[str, int]] = {}
    for roles, fixed_role, champion_id in anchors:
        partners = _best_partners(
            conn, window, platform, roles, fixed_role, champion_id, 500, min_games
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
        entry = {"synergy_sum": total, "stats": stats}
        if matchup_lookup is not None:
            entry["matchup_delta"] = matchup_lookup.get(cid)
        result[cid] = entry
    return result


def _combined_score(
    data: dict,
    n_anchors: int,
    weights: dict[str, float],
    zstats: dict[str, tuple[float, float]],
    *,
    matchup_weight: float = 0.0,
    matchup_zstats: tuple[float, float] | None = None,
) -> float | None:
    """Score composé (Σ poids × z-score) d'un candidat face à `n_anchors`
    ancrages, à partir de `data` (une entrée de `_sum_synergy` :
    `{"synergy_sum": ..., "stats": {col: moyenne}}`) — factorisé entre
    `greedy_complete_draft` (compléter un rôle vide) et `refine_draft`
    (comparer le champion en place à un remplaçant potentiel). `None` si un
    axe de `weights` manque (candidat écarté du classement, jamais traité
    comme 0).

    `matchup_weight`/`matchup_zstats` (mode "Contre cette équipe") :
    AJOUTÉ au score s'il y a une donnée (`data["matchup_delta"]` non
    `None`), sinon simplement ignoré — contrairement aux axes de `weights`,
    l'absence de pick adverse scouté sur ce rôle n'exclut jamais le
    candidat (retour utilisateur 2026-08-11 : scouting souvent partiel)."""
    combined = 0.0
    for axis, weight in weights.items():
        if weight == 0:
            continue
        col = ARCHETYPE_STAT_COLUMNS[axis]
        value = data["synergy_sum"] / n_anchors if col == "synergy" else data["stats"].get(col)
        if value is None:
            return None
        mean, std = zstats[col]
        combined += weight * (value - mean) / std
    if matchup_weight and matchup_zstats is not None:
        delta = data.get("matchup_delta")
        if delta is not None:
            mean, std = matchup_zstats
            combined += matchup_weight * (delta - mean) / std
    return combined


def _stat_columns_for(weights: dict[str, float]) -> list[str]:
    return [
        col
        for axis, col in ARCHETYPE_STAT_COLUMNS.items()
        if col != "synergy" and weights.get(axis, 0)
    ]


def greedy_complete_draft(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    placed: dict[str, int],
    total: float,
    min_games: int,
    weights: dict[str, float],
    zstats: dict[str, tuple[float, float]],
    *,
    matchup_weight: float = 0.0,
    matchup_by_role: dict[str, tuple[dict[int, float], tuple[float, float]]] | None = None,
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
    choisis à la main.

    `matchup_weight`/`matchup_by_role` (mode "Contre cette équipe") :
    ajoute le delta `score_matchup` du candidat contre le pick adverse du
    RÔLE EN COURS de complétion (pas des ancrages déjà posés — le matchup
    ne dépend que du rôle qu'on remplit)."""
    placed = dict(placed)  # ne jamais muter le dict de l'appelant
    remaining = [r for r in DRAFT_ROLES if r not in placed]
    stat_columns = _stat_columns_for(weights)
    while remaining:
        best: tuple[str, int, float, float] | None = None  # role, cid, synergy_sum, combined_z
        for role in remaining:
            anchors = [
                (_ROLES_BY_PAIR[frozenset({role, placed_role})], placed_role, placed_champ)
                for placed_role, placed_champ in placed.items()
            ]
            role_matchup = matchup_by_role.get(role) if matchup_by_role else None
            role_lookup, role_zstats = role_matchup if role_matchup else (None, None)
            scores = _sum_synergy(
                conn,
                window,
                platform,
                anchors,
                min_games,
                stat_columns,
                matchup_lookup=role_lookup,
            )
            if not scores:
                continue
            n_anchors = len(anchors)
            role_best: tuple[int, float, float] | None = None  # cid, synergy_sum, combined_z
            for cid, data in scores.items():
                combined = _combined_score(
                    data,
                    n_anchors,
                    weights,
                    zstats,
                    matchup_weight=matchup_weight,
                    matchup_zstats=role_zstats,
                )
                if combined is None:
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


def _champion_set(placed: dict[str, int]) -> frozenset[int]:
    return frozenset(placed.values())


def _is_diverse_enough(placed: dict[str, int], already_selected: list[dict[str, int]]) -> bool:
    """`placed` diffère assez de CHAQUE composition déjà retenue (au plus
    `DIVERSITY_MAX_SHARED_CHAMPIONS` champions en commun, peu importe le
    rôle) — cf. commentaire sur `DIVERSITY_MAX_SHARED_CHAMPIONS`."""
    candidate = _champion_set(placed)
    return all(
        len(candidate & _champion_set(other)) <= DIVERSITY_MAX_SHARED_CHAMPIONS
        for other in already_selected
    )


def _min_games_eff(
    conn: psycopg.Connection, window: str, platform: str, placed: dict[str, int]
) -> float:
    """`games_eff` MINIMUM sur les 10 VRAIES paires d'un draft complet —
    signal de fiabilité de la composition ENTIÈRE (retour utilisateur
    2026-07-28 : "pourquoi pas tous les duos plutôt que juste le premier ?"),
    pas seulement son duo de départ (l'ancien signal — le duo de départ
    n'est qu'un artefact de l'algorithme, jamais montré à l'utilisateur
    depuis le retour du 2026-07-27). Le MINIMUM, pas la moyenne : une
    composition n'est fiable que si CHACUNE de ses paires l'est — une seule
    paire peu jouée ne doit jamais être masquée par 9 autres solides."""
    return min(
        _duo_score(conn, window, platform, roles_str, placed[role_a], placed[role_b])["games_eff"]
        for role_x, role_y in itertools.combinations(DRAFT_ROLES, 2)
        for roles_str in (_ROLES_BY_PAIR[frozenset({role_x, role_y})],)
        for role_a, role_b in (DUO_ROLE_KEYS[roles_str],)
    )


def _all_pairs_reliable(
    conn: psycopg.Connection, window: str, platform: str, placed: dict[str, int], min_games: int
) -> bool:
    """Vérifie que les 10 VRAIES paires d'un draft complet `placed` ont
    toutes au moins `min_games` games — garde-fou après `refine_draft` : un
    remplacement en cascade (rôle B choisi en fonction du rôle A déjà
    remplacé) valide toujours la paire (A_new, B_new) au moment du choix de
    B, mais ne revalide jamais rétroactivement un rôle NON remplacé face à
    un remplacement survenu APRÈS lui dans le passage — cette fonction
    referme ce trou avant de livrer le résultat."""
    for role_x, role_y in itertools.combinations(DRAFT_ROLES, 2):
        roles_str = _ROLES_BY_PAIR[frozenset({role_x, role_y})]
        role_a, role_b = DUO_ROLE_KEYS[roles_str]
        row = _duo_score(conn, window, platform, roles_str, placed[role_a], placed[role_b])
        if row is None or row["games"] < min_games:
            return False
    return True


def refine_draft(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    placed: dict[str, int],
    total: float,
    min_games: int,
    weights: dict[str, float],
    zstats: dict[str, tuple[float, float]],
    *,
    locked_roles: frozenset[str] = frozenset(),
    matchup_weight: float = 0.0,
    matchup_by_role: dict[str, tuple[dict[int, float], tuple[float, float]]] | None = None,
) -> tuple[dict[str, int], float]:
    """UN SEUL passage de remplacement sur un draft COMPLET à 5 (retour
    utilisateur 2026-07-27 : "est-ce que le système essaie de remplacer le
    duo de base par un autre pour voir s'il n'y a pas une meilleure
    option ?"). Le glouton (`greedy_complete_draft`) ne revient jamais en
    arrière : un champion posé tôt (le duo de départ compris), bon en
    isolation, peut ne plus être optimal une fois les autres connus.

    Pour chaque rôle NON verrouillé (`locked_roles` — jamais les rôles
    choisis à la main par l'utilisateur, cf. `_manual_propose`), cherche le
    meilleur candidat compte tenu des 4 AUTRES champions actuels (déjà
    remplacés ou non plus tôt dans CE MÊME passage — les remplacements
    s'enchaînent, pas de retour au point de départ) et substitue SEULEMENT
    si ce candidat fait STRICTEMENT mieux que le champion en place sur le
    score composé de l'archétype. Toujours 1 passage (pas itéré jusqu'à
    convergence) : plusieurs duos de départ sont déjà essayés en amont
    (`propose_drafts`), le gain marginal d'itérer serait probablement
    faible face au coût (retour utilisateur, discussion).

    Un remplacement en cascade peut laisser un rôle NON remplacé désaccordé
    avec un remplacement survenu après lui (jamais revérifié après coup) —
    `_all_pairs_reliable` valide le résultat final ; en cas d'échec (rare),
    repli sur `placed`/`total` D'ORIGINE plutôt que de livrer une
    composition avec une paire non fiable."""
    original_placed, original_total = placed, total
    placed = dict(placed)
    stat_columns = _stat_columns_for(weights)
    changed = False
    for role in DRAFT_ROLES:
        if role in locked_roles:
            continue
        current_champ = placed[role]
        anchors = [
            (_ROLES_BY_PAIR[frozenset({role, other_role})], other_role, other_champ)
            for other_role, other_champ in placed.items()
            if other_role != role
        ]
        role_matchup = matchup_by_role.get(role) if matchup_by_role else None
        role_lookup, role_zstats = role_matchup if role_matchup else (None, None)
        scores = _sum_synergy(
            conn, window, platform, anchors, min_games, stat_columns, matchup_lookup=role_lookup
        )
        if not scores:
            continue
        n_anchors = len(anchors)
        current_data = scores.get(current_champ)
        current_combined = (
            _combined_score(
                current_data,
                n_anchors,
                weights,
                zstats,
                matchup_weight=matchup_weight,
                matchup_zstats=role_zstats,
            )
            if current_data is not None
            else None
        )
        best: tuple[int, float, float] | None = None  # cid, synergy_sum, combined
        for cid, data in scores.items():
            combined = _combined_score(
                data,
                n_anchors,
                weights,
                zstats,
                matchup_weight=matchup_weight,
                matchup_zstats=role_zstats,
            )
            if combined is None:
                continue
            if best is None or combined > best[2]:
                best = (cid, data["synergy_sum"], combined)
        if best is None:
            continue
        cid, synergy_sum, combined = best
        if cid == current_champ:
            continue
        if current_data is None or current_combined is None:
            # Le champion en place n'apparaît pas parmi les candidats fiables
            # retrouvés pour ce jeu d'ancrages (peut arriver après un
            # remplacement en cascade plus tôt dans ce passage) : comparer à
            # une base inconnue serait arbitraire, on laisse ce rôle tel
            # quel — `_all_pairs_reliable` traitera le cas si besoin.
            continue
        if combined <= current_combined:
            continue
        total += synergy_sum - current_data["synergy_sum"]
        placed[role] = cid
        changed = True
    if not changed:
        return original_placed, original_total
    if not _all_pairs_reliable(conn, window, platform, placed, min_games):
        return original_placed, original_total
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


def _matchup_avg_z(
    placed: dict[str, int],
    matchup_by_role: dict[str, tuple[dict[int, float], tuple[float, float]]],
) -> float | None:
    """Moyenne des z-scores de delta `score_matchup` sur les rôles SCOUTÉS
    (mode "Contre cette équipe") — moyenne de z-scores (pas de deltas bruts
    puis z-score global) : chaque rôle a sa propre distribution de deltas
    contre SON pick adverse, pas comparable directement entre rôles. `None`
    si aucun rôle scouté n'a de donnée pour le champion posé (scouting
    partiel, cf. `_combined_score`)."""
    zscores = []
    for role, (lookup, (mean, std)) in matchup_by_role.items():
        delta = lookup.get(placed.get(role))
        if delta is not None:
            zscores.append((delta - mean) / std)
    return sum(zscores) / len(zscores) if zscores else None


def full_draft_score(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    placed: dict[str, int],
    weights: dict[str, float],
    zstats: dict[str, tuple[float, float]],
    *,
    matchup_weight: float = 0.0,
    matchup_by_role: dict[str, tuple[dict[int, float], tuple[float, float]]] | None = None,
) -> float | None:
    """Score composite (Σ poids × z-score) sur les 10 VRAIES paires d'un
    draft complet `placed` — sert à comparer plusieurs compositions
    candidates FINIES entre elles pour un même archétype, plutôt que de
    garder la première qui complète.

    `matchup_weight`/`matchup_by_role` (mode "Contre cette équipe", retour
    utilisateur 2026-08-11) : ajoute `matchup_weight × _matchup_avg_z(...)`,
    simplement ignoré si aucun rôle scouté n'a de donnée exploitable."""
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
    if matchup_weight and matchup_by_role:
        avg_z = _matchup_avg_z(placed, matchup_by_role)
        if avg_z is not None:
            combined += matchup_weight * avg_z
    return combined


def seed_from_champions(
    conn: psycopg.Connection, window: str, platform: str, picks: dict[str, int]
) -> tuple[dict[str, int], float, list[dict]]:
    """`picks` : 1 à 5 (rôle → champion) choisis à la main. Calcule le total
    de synergie déjà couvert par les paires FORMÉES ENTRE CES CHAMPIONS
    (`score_duo`) et le détail de fiabilité de chacune — JAMAIS bloquant :
    une paire jamais jouée ensemble contribue 0 à la synergie et affiche une
    fiabilité `tier=None`, elle n'empêche pas de continuer (contrairement
    aux rôles que le système complète ensuite, filtrés par `min_games`).
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


def propose_for_weights(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    pool: list[dict],
    zstats: dict[str, tuple[float, float]],
    archetype_key: str,
    label: str,
    weights: dict[str, float],
    *,
    min_games: int = MIN_GAMES_DEFAULT,
    matchup_weight: float = 0.0,
    matchup_by_role: dict[str, tuple[dict[int, float], tuple[float, float]]] | None = None,
) -> list[dict]:
    """Jusqu'à 3 compositions pour UN jeu de poids donné (retour utilisateur
    2026-07-27 : "des boutons 1/2/3... 3 propositions par archétype" —
    jamais forcé à 3 si la diversité manque). Factorisée hors de
    `propose_drafts` (retour utilisateur 2026-07-28, poids personnalisés)
    pour être réutilisable aussi bien par les 6 archétypes fixes que par un
    jeu de poids choisi par l'utilisateur. Essaie TOUS les duos de départ
    du short-list (pas seulement le premier qui complète), calcule le score
    de chaque composition FINIE sur ses 10 vraies paires
    (`full_draft_score`), puis sélectionne :

    - rang 0 : le meilleur score.
    - rang 1 : le 2e meilleur score suffisamment DIFFÉRENT du rang 0
      (`_is_diverse_enough`) — des seeds différents convergent souvent vers
      la même fin de complétion gloutonne, un pur tri par score produirait
      sinon des quasi-doublons.
    - rang 2 : parmi les candidats restants encore suffisamment différents
      des rangs 0 et 1, celui dont le `games_eff` MINIMUM sur ses 10 vraies
      paires (`_min_games_eff`) est le plus élevé — pas le score, et pas
      seulement le duo de départ (retour utilisateur 2026-07-28 : le duo de
      départ n'est qu'un artefact de l'algorithme, jamais montré à
      l'utilisateur ; la fiabilité de la composition ENTIÈRE compte, bornée
      par sa paire la MOINS soutenue). "Une des trois avec une fiabilité
      très élevée, plus de games que les autres" (retour utilisateur
      2026-07-27). Les 3 candidats passent déjà tous le seuil `min_games`
      par construction ; cette 3e place privilégie le VOLUME de données
      plutôt que le score.

    `min_games` (retour utilisateur 2026-07-28 : "choisir le niveau de
    fiabilité... en choisissant un nombre de games") : seuil de games RÉELS
    minimum pour qu'un duo compte comme candidat fiable (seed, complétion,
    remplacement) — `MIN_GAMES_DEFAULT` pour les 6 archétypes fixes de
    "Compositions suggérées" (jamais choisi par l'utilisateur), une valeur
    choisie par l'utilisateur pour "Compose à partir de tes champions" et
    "Personnalise tes poids" (`web/app.py`).

    Chaque rang retenu reçoit ensuite SON PROPRE passage de remplacement
    (`refine_draft`, retour utilisateur 2026-07-27 : "est-ce que le système
    essaie de remplacer le duo de base par un autre ?") — rien n'est
    verrouillé, le duo de départ lui-même peut être remplacé. Si 2 rangs
    convergent vers exactement les mêmes 5 champions APRÈS ce raffinement
    (rare mais possible), le doublon est supprimé plutôt qu'affiché 2 fois.
    Si le raffinement touche l'un des 2 rôles du duo de départ, `seed_pairs`
    est recalculé sur l'état FINAL (jamais l'ancien duo devenu
    incomplet/inexact à afficher).

    Retourne une liste (0 à 3 entrées) : `{archetype, label, suggestion_rank
    (0-2), selection ("score"|"diverse"|"reliable"), weights, members (dict
    rôle→champion_id), total_synergy, seed_pairs (1 entrée, le duo de
    départ), advice_stats (moyennes scaling/cc/gold/wr/ci_low/ci_high sur
    les 10 vraies paires, `DISPLAY_STAT_COLUMNS`, ou None), counters (points
    faibles), strengths (points forts)}` — brut (`champion_id`, pas de
    nom/icône), même forme que lue depuis `draft_suggestion(_counter)`
    matérialisées par `refresh`, pour que le rendu (`web/app.py`) traite
    les 2 sources de façon identique.

    `matchup_weight`/`matchup_by_role` (mode "Contre cette équipe", retour
    utilisateur 2026-08-11) : simple passe-plat vers `archetype_seed_order`/
    `greedy_complete_draft`/`full_draft_score`/`refine_draft` — non `None`
    uniquement pour ce mode, `weights` ne contient jamais l'axe "matchup"
    lui-même (absent de `ARCHETYPE_STAT_COLUMNS`)."""
    seeds = archetype_seed_order(
        pool, weights, zstats, matchup_weight=matchup_weight, matchup_by_role=matchup_by_role
    )
    candidates: list[tuple[float, dict, dict, float]] = []
    for seed_row in seeds[:SEED_SHORTLIST]:
        role_a, role_b = DUO_ROLE_KEYS[seed_row["roles"]]
        seed_placed = {role_a: seed_row["champ_a"], role_b: seed_row["champ_b"]}
        completed = greedy_complete_draft(
            conn,
            window,
            platform,
            seed_placed,
            seed_row["synergy"],
            min_games,
            weights,
            zstats,
            matchup_weight=matchup_weight,
            matchup_by_role=matchup_by_role,
        )
        if completed is None:
            continue
        placed, total = completed
        score = full_draft_score(
            conn,
            window,
            platform,
            placed,
            weights,
            zstats,
            matchup_weight=matchup_weight,
            matchup_by_role=matchup_by_role,
        )
        if score is None:
            continue
        candidates.append((score, seed_row, placed, total))
    if not candidates:
        return []

    by_score = sorted(range(len(candidates)), key=lambda i: -candidates[i][0])
    selected_idx: list[int] = []
    selected_placed: list[dict[str, int]] = []
    selections: list[str] = []

    for i in by_score:
        if len(selected_idx) >= 2:
            break
        placed_i = candidates[i][2]
        if _is_diverse_enough(placed_i, selected_placed):
            selected_idx.append(i)
            selected_placed.append(placed_i)
            selections.append("score" if not selections else "diverse")

    if len(selected_idx) == 2:
        eligible = [
            i
            for i in by_score
            if i not in selected_idx and _is_diverse_enough(candidates[i][2], selected_placed)
        ]
        if eligible:
            best_reliable = max(
                eligible,
                key=lambda i: _min_games_eff(conn, window, platform, candidates[i][2]),
            )
            selected_idx.append(best_reliable)
            selected_placed.append(candidates[best_reliable][2])
            selections.append("reliable")

    results: list[dict] = []
    seen_final: list[frozenset[int]] = []
    rank = 0
    for i, selection in zip(selected_idx, selections, strict=True):
        _, seed_row, placed, total = candidates[i]
        placed, total = refine_draft(
            conn,
            window,
            platform,
            placed,
            total,
            min_games,
            weights,
            zstats,
            matchup_weight=matchup_weight,
            matchup_by_role=matchup_by_role,
        )
        final_set = _champion_set(placed)
        if final_set in seen_final:
            continue  # raffinement convergé vers un rang déjà retenu
        seen_final.append(final_set)
        role_a, role_b = DUO_ROLE_KEYS[seed_row["roles"]]
        final_seed = _duo_score(
            conn, window, platform, seed_row["roles"], placed[role_a], placed[role_b]
        )
        results.append(
            {
                "archetype": archetype_key,
                "label": label,
                "suggestion_rank": rank,
                "selection": selection,
                "weights": weights,
                "members": placed,
                "total_synergy": total,
                "seed_pairs": [
                    {
                        "role_a": role_a,
                        "role_b": role_b,
                        "champ_a": placed[role_a],
                        "champ_b": placed[role_b],
                        "synergy": final_seed["synergy"] if final_seed else None,
                        "games": final_seed["games"] if final_seed else 0,
                        "tier": final_seed["tier"] if final_seed else None,
                    }
                ],
                "advice_stats": full_draft_stat_averages(
                    conn, window, platform, placed, DISPLAY_STAT_COLUMNS
                ),
                "counters": draft_counters(conn, window, platform, placed),
                "strengths": draft_strengths(conn, window, platform, placed),
            }
        )
        rank += 1
    return results


def propose_drafts(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    pool: list[dict],
    zstats: dict[str, tuple[float, float]],
) -> list[dict]:
    """Une composition (jusqu'à 3 variantes, cf. `propose_for_weights`) par
    archétype de `ARCHETYPES` — jamais un archétype sans composition
    silencieusement omis : il n'apparaît juste pas. Retourne une liste
    PLATE (pas groupée par archétype)."""
    results: list[dict] = []
    for key, archetype in ARCHETYPES.items():
        results.extend(
            propose_for_weights(
                conn, window, platform, pool, zstats, key, archetype["label"], archetype["weights"]
            )
        )
    return results


def propose_counter_draft(
    conn: psycopg.Connection,
    window: str,
    platform: str,
    enemy: dict[str, int],
    *,
    weights: dict[str, float] | None = None,
    min_games: int = MIN_GAMES_DEFAULT,
) -> list[dict]:
    """Mode "Contre cette équipe" (retour utilisateur 2026-08-11 : "il ne
    faudrait pas juste prendre les counters à chaque rôle mais aussi que
    les champions counter synergisent bien entre eux"). Calculé À LA
    DEMANDE (pas matérialisé par le service 24/24, comme "Compose à partir
    de tes champions"/"Personnalise tes poids") : `enemy` est une saisie
    utilisateur, jamais connue à l'avance.

    `enemy` : 0 à 5 entrées (rôle court → champion_id adverse), scouting
    partiel autorisé — un rôle absent n'exclut aucun candidat, l'axe
    matchup est simplement ignoré pour ce rôle (cf. `_combined_score`,
    `_matchup_avg_z`). Avec `enemy` vide, dégénère proprement en un profil
    pondéré synergy(0.30) + scaling/cc/gold/drakes(0.075 chacun) — le poids
    matchup (0.40 par défaut) ne s'applique simplement nulle part.

    `weights` : `DEFAULT_COUNTER_WEIGHTS` si `None` — la clé `"matchup"` en
    est extraite ici (pas un axe `ARCHETYPE_STAT_COLUMNS`, `propose_for_weights`
    ne doit jamais la voir), le reste suit le même moteur que les 6
    archétypes fixes."""
    weights = dict(weights) if weights is not None else dict(DEFAULT_COUNTER_WEIGHTS)
    matchup_weight = weights.pop("matchup", 0.0)
    pool, zstats = pool_and_zstats(conn, window, platform, min_games)
    matchup_by_role = _load_matchup_by_role(conn, window, platform, enemy)
    return propose_for_weights(
        conn,
        window,
        platform,
        pool,
        zstats,
        "counter",
        "Contre cette équipe",
        weights,
        min_games=min_games,
        matchup_weight=matchup_weight,
        matchup_by_role=matchup_by_role,
    )


# --- Matérialisation (service 24/24) ---

_INSERT_SUGGESTION_SQL = """
    INSERT INTO draft_suggestion
        (window_label, platform, archetype, suggestion_rank, selection, label,
         top_champion, jgl_champion, mid_champion, bot_champion, sup_champion,
         total_synergy, seed_roles, seed_champ_a, seed_champ_b, seed_synergy,
         seed_games, seed_tier, advice_scaling, advice_cc, advice_gold15,
         wr, wr_ci_low, wr_ci_high)
    VALUES
        (%(window_label)s, %(platform)s, %(archetype)s, %(suggestion_rank)s, %(selection)s,
         %(label)s, %(top_champion)s, %(jgl_champion)s, %(mid_champion)s, %(bot_champion)s,
         %(sup_champion)s, %(total_synergy)s, %(seed_roles)s, %(seed_champ_a)s, %(seed_champ_b)s,
         %(seed_synergy)s, %(seed_games)s, %(seed_tier)s, %(advice_scaling)s, %(advice_cc)s,
         %(advice_gold15)s, %(wr)s, %(wr_ci_low)s, %(wr_ci_high)s)
"""
_INSERT_COUNTER_SQL = """
    INSERT INTO draft_suggestion_counter
        (window_label, platform, archetype, suggestion_rank, direction, kind, rank, role,
         against_champion, champion_id, delta)
    VALUES
        (%(window_label)s, %(platform)s, %(archetype)s, %(suggestion_rank)s, %(direction)s,
         %(kind)s, %(rank)s, %(role)s, %(against_champion)s, %(champion_id)s, %(delta)s)
"""


def _write_matchup_picks(
    cur,
    window: PatchWindow,
    platform: str,
    archetype: str,
    suggestion_rank: int,
    direction: str,
    picks: dict | None,
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
                "suggestion_rank": suggestion_rank,
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
                "suggestion_rank": suggestion_rank,
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
    `draft_suggestion(_counter)`. Retourne le nombre de compositions écrites
    (0 à 18 : jusqu'à 3 par archétype, cf. `propose_drafts`). DELETE +
    INSERT (même raisonnement que `resilience`/`win_factors`/`gold_factors`).

    Autocommit (retour utilisateur 2026-08-11) : `propose_drafts` fait de
    très nombreux allers-retours en lecture (seed shortlist × archétypes,
    ~30-47s mesuré par région) — sans autocommit, tout ça reste dans UNE
    seule transaction ouverte jusqu'à la fin, et une connexion qui
    n'atteint jamais son terme proprement (exception non prévue, process
    tué) peut rester "idle in transaction" et ralentir tout le reste
    (constaté en direct en prod, connexion bloquée 176s). `conn.transaction()`
    ci-dessous délimite explicitement l'écriture, comme `compute.py`."""
    with psycopg.connect(db.require_dsn(dsn), autocommit=True) as conn:
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
                        "suggestion_rank": d["suggestion_rank"],
                        "selection": d["selection"],
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
                        "wr": stats.get("wr"),
                        "wr_ci_low": stats.get("ci_low"),
                        "wr_ci_high": stats.get("ci_high"),
                    },
                )
                _write_matchup_picks(
                    cur,
                    window,
                    platform,
                    d["archetype"],
                    d["suggestion_rank"],
                    "weakness",
                    d["counters"],
                )
                _write_matchup_picks(
                    cur,
                    window,
                    platform,
                    d["archetype"],
                    d["suggestion_rank"],
                    "strength",
                    d["strengths"],
                )
    logger.info(
        "draft_suggestions %s/%s rafraîchies : %d composition(s)",
        window.label,
        platform,
        len(drafts),
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
