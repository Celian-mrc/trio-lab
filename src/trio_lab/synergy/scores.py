"""Mathématiques des scores de synergie (Phase 3) — module pur, zéro I/O.

Définitions (PROJECT.md) :
- Synergy(combo) = WR(combo) − moyenne(WR individuels des membres) ;
- prédiction trio = moyenne des 3 synergies de duo — dérivation : si l'on
  prédit WR(trio) par la moyenne des WR des 3 duos, les baselines individuelles
  se compensent exactement et il reste la moyenne des synergies de duo ;
- lissage bayésien : un trio peu joué est tiré vers sa prédiction duo,
  `synergy = (n·raw + k·pred) / (n + k)` avec n = games effectifs (pondérés
  fenêtre) et k la force du prior en games-équivalents ;
- incertitude : intervalle de Wilson à 95 % sur le WR, tiers de fiabilité par
  seuils de games effectifs.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

# Force du prior duo→trio en games-équivalents (à recalibrer avec le volume).
DEFAULT_PRIOR_K = 200.0
# Seuils de games effectifs des tiers de fiabilité (faible < 50 ≤ moyen < 400 ≤ élevé).
DEFAULT_TIER_THRESHOLDS: tuple[float, float] = (50.0, 400.0)
# Quantile normal à 95 % bilatéral (Wilson).
Z_95 = 1.959963984540054


@dataclass(frozen=True)
class WeightedWR:
    """Winrate pondéré par la fenêtre multi-patchs."""

    wr: float
    games: int  # games bruts (affichage)
    games_eff: float  # games pondérés (poids de patch) — le « n » statistique


def weighted_wr(
    per_patch: Iterable[tuple[str, int, int]], weights: dict[str, float]
) -> WeightedWR | None:
    """Agrège des lignes `(patch, games, wins)` avec les poids de la fenêtre.

    Retourne None si aucun games effectif (patch absent de la fenêtre ou poids
    nuls après coupure de rework) — la combinaison n'est alors pas scorée.
    """
    games = 0
    games_eff = 0.0
    wins_eff = 0.0
    for patch, patch_games, patch_wins in per_patch:
        weight = weights.get(patch, 0.0)
        if weight <= 0.0:
            continue
        games += patch_games
        games_eff += weight * patch_games
        wins_eff += weight * patch_wins
    if games_eff <= 0.0:
        return None
    return WeightedWR(wr=wins_eff / games_eff, games=games, games_eff=games_eff)


def wilson_interval(wr: float, n: float, z: float = Z_95) -> tuple[float, float]:
    """Intervalle de Wilson sur une proportion (borné [0, 1], sain pour petits n)."""
    if n <= 0.0:
        return 0.0, 1.0
    denominator = 1.0 + z * z / n
    center = (wr + z * z / (2.0 * n)) / denominator
    margin = (z / denominator) * math.sqrt(wr * (1.0 - wr) / n + z * z / (4.0 * n * n))
    return max(0.0, center - margin), min(1.0, center + margin)


def normal_interval(p: float, se: float, z: float = Z_95) -> tuple[float, float]:
    """IC normal simple (approximation), borné [0, 1].

    Utilisé pour la baseline individuelle d'une synergie (moyenne de 2 ou 3 WR
    de champion, pas un comptage direct de victoires → Wilson ne s'applique
    pas tel quel) : le volume individuel d'un champion est presque toujours
    bien plus grand que celui du combo, l'approximation normale y est fiable.
    """
    margin = z * se
    return max(0.0, p - margin), min(1.0, p + margin)


def newcombe_interval(
    p1: float, l1: float, u1: float, p2: float, l2: float, u2: float
) -> tuple[float, float]:
    """IC de Newcombe (1998) pour une différence p1 − p2, à partir de 2 IC déjà
    calculés séparément pour p1 et p2 (méthode standard pour comparer 2
    proportions sans recalculer une variance jointe)."""
    diff = p1 - p2
    lo = diff - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    hi = diff + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return lo, hi


def reliability_tier(
    games_eff: float, thresholds: tuple[float, float] = DEFAULT_TIER_THRESHOLDS
) -> str:
    """Tier de fiabilité d'un score selon son volume effectif."""
    low, high = thresholds
    if games_eff < low:
        return "faible"
    if games_eff < high:
        return "moyen"
    return "eleve"


def synergy(combo_wr: float, member_wrs: Iterable[float]) -> float:
    """Synergy(combo) = WR(combo) − moyenne(WR individuels des membres)."""
    members = list(member_wrs)
    return combo_wr - sum(members) / len(members)


def trio_prediction(duo_synergies: Iterable[float]) -> float:
    """Prédiction de la synergie trio = moyenne des synergies de duo disponibles.

    Avec moins de 3 duos observés, la moyenne porte sur les duos présents ;
    sans aucun duo, prior neutre 0 (le lissage tire alors vers « pas de
    synergie », le comportement prudent).
    """
    synergies = list(duo_synergies)
    if not synergies:
        return 0.0
    return sum(synergies) / len(synergies)


ALL_PLATFORMS = "all"


def add_combined_platform(
    mapping: dict[tuple, list[tuple[str, int, int]]], label: str = ALL_PLATFORMS
) -> None:
    """Ajoute à `mapping` des entrées « toutes plateformes » (sommes par patch).

    Les clés sont `(platform, *reste)` et les lignes `(patch, games, wins)` :
    additives entre plateformes, donc la vue combinée est exacte — c'est une
    matérialisation de lecture, la plateforme reste une colonne de données.
    Mutation en place ; les entrées déjà étiquetées `label` sont ignorées
    (idempotent).
    """
    combined: dict[tuple, dict[str, list[int]]] = {}
    for (platform, *rest), rows in mapping.items():
        if platform == label:
            continue
        acc = combined.setdefault((label, *rest), {})
        for patch, games, wins in rows:
            cell = acc.setdefault(patch, [0, 0])
            cell[0] += games
            cell[1] += wins
    for key, per_patch in combined.items():
        mapping[key] = [(patch, g, w) for patch, (g, w) in per_patch.items()]


def smooth(raw: float, games_eff: float, prediction: float, k: float = DEFAULT_PRIOR_K) -> float:
    """Lissage bayésien du score trio vers sa prédiction duo.

    `k` = poids du prior en games-équivalents : à n = k, moitié-moitié ;
    n = 0 → prédiction pure ; n ≫ k → score brut.
    """
    if k < 0:
        raise ValueError(f"k négatif : {k}")
    return (games_eff * raw + k * prediction) / (games_eff + k)


# t de Student critique à 95 % bilatéral, par degré de liberté (nb de points
# − 2). Table embarquée plutôt qu'une dépendance (scipy) : les tranches de
# durée vont de 3 (SCALING_MIN_BUCKETS) à 8 au maximum (5 à 40 min par pas de
# 5), donc df ∈ [1, 6] toujours.
_SLOPE_T_CRITICAL: dict[int, float] = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
}


@dataclass(frozen=True)
class WeightedSlope:
    """Pente d'une régression linéaire pondérée + IC à 95 % sur cette pente."""

    slope: float
    ci_low: float
    ci_high: float


def weighted_slope_ci(points: Iterable[tuple[float, float, float]]) -> WeightedSlope | None:
    """Pente d'une régression linéaire pondérée y ~ x (moindres carrés) + IC.

    `points` = (x, y, poids). Utilisé pour le score de scaling (WR ~ tranche
    de durée) : pas de dépendance lourde (numpy/scipy, CLAUDE.md), la formule
    fermée suffit pour une régression à une variable. L'IC vient de
    l'erreur-type de la pente (variance résiduelle pondérée / dispersion des
    x), loi de Student vu le peu de points — jamais l'approximation normale.
    `None` si < 3 points, poids nuls, x constant, ou plus de degrés de liberté
    que la table `_SLOPE_T_CRITICAL` n'en couvre (ne devrait pas arriver, cf.
    borne des tranches ci-dessus).
    """
    pts = list(points)
    n = len(pts)
    if n < 3:
        return None
    w_sum = sum(w for _, _, w in pts)
    if w_sum <= 0.0:
        return None
    x_mean = sum(w * x for x, _, w in pts) / w_sum
    y_mean = sum(w * y for _, y, w in pts) / w_sum
    num = sum(w * (x - x_mean) * (y - y_mean) for x, y, w in pts)
    den = sum(w * (x - x_mean) ** 2 for x, _, w in pts)
    if den <= 0.0:
        return None
    slope = num / den
    t = _SLOPE_T_CRITICAL.get(n - 2)
    if t is None:
        return None
    intercept = y_mean - slope * x_mean
    rss = sum(w * (y - (intercept + slope * x)) ** 2 for x, y, w in pts)
    se = math.sqrt(rss / (n - 2) / den)
    margin = t * se
    return WeightedSlope(slope, slope - margin, slope + margin)
