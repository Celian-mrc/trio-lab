"""Score CC théorique par sort / champion / trio (Phase 2b) — module pur.

Formules (PROJECT.md) :

    score_sort     = poids(type_cc) × durée(s) × coef_zone × coef_fiabilité
                     × coef_disponibilité × coef_repositionnement × coef_conditionnel
    score_champion = agrégation des sorts du kit
    score_trio     = Σ score_champion des 3 membres

Règles actées en relecture (2026-07-11) :
- slow : poids 0.3 × (%slow / 100) ;
- **CC durs simultanés d'un même sort** (knock-up + stun d'Alistar Q…) : ils ne
  s'additionnent pas — seule la contribution la plus forte du sort compte ;
  les slows du sort s'ajoutent, eux (ils prolongent la gêne) ;
- conditionnel (collision, charge, marques…) : coefficient réducteur.

Les poids sont la config à recalibrer après la validation par corrélation
avec le `timeCCingOthers` empirique (`python -m trio_lab.ccref.validate`).
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from pathlib import Path

from trio_lab.ccref.build import FROZEN_PATH
from trio_lab.synergy.scores import smooth

logger = logging.getLogger(__name__)

# Poids par type de CC (PROJECT.md § Score CC théorique).
WEIGHTS: dict[str, float] = {
    "airborne": 1.0,
    "suppression": 1.0,
    "stun": 0.9,
    "charm": 0.9,
    "fear": 0.9,
    "taunt": 0.9,
    "sleep": 0.9,
    "root": 0.7,
    "silence": 0.5,
    "ground": 0.5,
    "blind": 0.5,
    "polymorph": 0.5,
    "slow": 0.3,  # multiplié par %slow/100
}
COEF_ZONE = 1.5  # multi-cibles
COEF_POINT_CLICK = 1.2  # non esquivable par déplacement (déf. relecture)
COEF_ULTIMATE = 0.5  # disponibilité réduite
COEF_REPOSITIONNEMENT = 1.15  # airborne déplaçant la cible
COEF_CONDITIONNEL = 0.7  # CC sous condition (collision, charge, marques…)

# Coef_frequence : bonus pour les CC de base réapplicables souvent (Ashe P
# on-hit, Garen Q, Udyr E…). Calibré le 2026-07-12 sur la médiane des cooldowns
# extraits du wiki (429 sorts CC, médiane 12 s) : un cooldown ≤ la médiane
# donne un bonus proportionnel, plafonné pour ne pas écraser le reste du
# barème déjà validé contre l'empirique. Volontairement limité aux sorts de
# BASE (jamais les ultimates : déjà pénalisées par COEF_ULTIMATE, et leur
# cooldown extrait du wiki est plus sujet à erreur d'extraction — un sort à
# charges (Caitlyn W, Rumble E…) utilise `|recharge=` plutôt que `|cooldown=`,
# géré en amont dans `ccref.parse`).
COOLDOWN_REFERENCE_S = 12.0
COEF_FREQUENCE_CAP = 1.5

# Types dont les contributions ne se cumulent PAS au sein d'un même sort.
HARD_CC_TYPES = frozenset(WEIGHTS) - {"slow"}


def load_reference(path: Path = FROZEN_PATH) -> list[dict[str, str]]:
    """Charge la référence gelée (lignes CSV, commentaires ignorés)."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(line for line in fh if not line.startswith("#")))


def row_contribution(row: dict[str, str]) -> float:
    """Contribution d'une ligne (un type de CC d'un sort) au score.

    Une ligne sans durée (rare : slows « tant que dans la zone » non chiffrés)
    contribue 0 — signalé par l'appelant.
    """
    duration = row["duree_s"].strip()
    if not duration:
        return 0.0
    weight = WEIGHTS[row["type_cc"]]
    value = weight * float(duration)
    if row["type_cc"] == "slow":
        pct = row["pct_slow"].strip()
        if not pct:
            return 0.0
        value *= float(pct) / 100.0
    if row["zone"] == "multi":
        value *= COEF_ZONE
    if row["fiabilite"] == "point_click":
        value *= COEF_POINT_CLICK
    if row["disponibilite"] == "ultimate":
        value *= COEF_ULTIMATE
    if row["repositionnement"].strip() == "1":
        value *= COEF_REPOSITIONNEMENT
    if row["conditionnel"].strip() == "1":
        value *= COEF_CONDITIONNEL
    cooldown = row.get("cooldown_s", "").strip()
    if cooldown and row["disponibilite"] == "base":
        value *= min(COEF_FREQUENCE_CAP, max(1.0, COOLDOWN_REFERENCE_S / float(cooldown)))
    return value


def spell_score(rows: list[dict[str, str]]) -> float:
    """Score d'un sort : max des CC durs (simultanés non cumulés) + Σ des slows."""
    hard = [row_contribution(r) for r in rows if r["type_cc"] in HARD_CC_TYPES]
    slows = [row_contribution(r) for r in rows if r["type_cc"] == "slow"]
    return (max(hard) if hard else 0.0) + sum(slows)


def champion_scores(rows: list[dict[str, str]] | None = None) -> dict[str, float]:
    """Score CC théorique par champion (Σ des scores de ses sorts)."""
    if rows is None:
        rows = load_reference()
    by_spell: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_spell[(row["champion"], row["sort"])].append(row)
    skipped = [
        (r["champion"], r["sort"], r["type_cc"])
        for r in rows
        if row_contribution(r) == 0.0 and not r["duree_s"].strip()
    ]
    if skipped:
        logger.info("%d lignes sans durée ignorées (contribution 0) : %s", len(skipped), skipped)
    scores: dict[str, float] = defaultdict(float)
    for (champion, _spell), spell_rows in by_spell.items():
        scores[champion] += spell_score(spell_rows)
    return dict(scores)


def trio_score(champions: tuple[str, str, str], scores: dict[str, float] | None = None) -> float:
    """Score CC théorique d'un trio = Σ des scores de ses 3 membres."""
    if scores is None:
        scores = champion_scores()
    return sum(scores.get(name, 0.0) for name in champions)


# --- Normalisation 0-100 et mélange avec le CC empirique ---
#
# Calibré sur les données du 11/07/2026 (76 325 trios scorés, patch 16.13) :
# - plafond théorique = 3 × le score max d'un champion (Taliyah, 7.30) — aucun
#   trio réel ne peut l'atteindre (3 rôles distincts), c'est un repère fixe ;
# - plafond empirique = ~p99 des trios scorés — au-delà, les valeurs sont
#   surtout des artefacts de mesure Riot (cas Nocturne, cf. memory
#   phase2b-relecture-workflow) et sont plafonnées à 100 plutôt que de laisser
#   l'échelle entière se caler sur l'outlier maximal.
# Recalibré le 13/07/2026 : l'empirique (Σ timeCCingOthers) était cumulé sur
# toute la partie, donc mécaniquement gonflé par sa durée (Pearson +0.64 avec
# la durée, retour utilisateur) — passé en PAR MINUTE (cf. stats/aggregate.py)
# ; nouveau p99 ≈ 5,48 s/min sur 141 145 trios (patch 16.13), plafond arrondi
# à 6,0.
# À recalibrer si la distribution empirique dérive significativement.
EMPIRICAL_CEILING_S_PER_MIN = 6.0
BLEND_PRIOR_K = 200.0  # même force de lissage que la synergie (synergy.scores)


def theoretical_pct(
    raw_score: float, member_count: int = 3, scores: dict[str, float] | None = None
) -> float:
    """Score CC théorique d'une combinaison, normalisé sur 100 (repère :
    `member_count` × le max d'un champion — un plafond mathématique, jamais
    atteignable en pratique, aucun champion n'occupant plusieurs rôles)."""
    if scores is None:
        scores = champion_scores()
    ceiling = member_count * max(scores.values())
    return 100 * raw_score / ceiling if ceiling > 0 else 0.0


def empirical_pct(
    cc_time_s_per_min: float | None, ceiling: float = EMPIRICAL_CEILING_S_PER_MIN
) -> float | None:
    """CC empirique (Σ `timeCCingOthers` du trio, PAR MINUTE de game), normalisé
    sur 100 et plafonné : les valeurs extrêmes n'écrasent pas le reste de
    l'échelle."""
    if cc_time_s_per_min is None:
        return None
    return min(100 * cc_time_s_per_min / ceiling, 100.0)


def blended_pct(
    empirical: float | None, theoretical: float, games_eff: float, k: float = BLEND_PRIOR_K
) -> float:
    """Lissage bayésien empirique → théorique (même mécanique que la
    synergie, `synergy.scores.smooth`) : un trio peu joué (games_eff faible)
    est tiré vers le théorique, stable ; un trio très joué reste proche de
    l'empirique.

    Limite à connaître : ce lissage réduit mais n'élimine pas un biais
    structurel à haut volume — ce n'est pas du bruit qui se moyenne avec plus
    de games. Le cas connu (Nocturne, `timeCCingOthers` gonflé par son
    ultimate) est corrigé en amont, à l'ingestion (`ccref.reliability`), donc
    déjà absent de `empirical` ici ; ce lissage reste la protection pour un
    éventuel futur cas non encore identifié.
    """
    raw = empirical if empirical is not None else theoretical
    return smooth(raw, games_eff, theoretical, k)
