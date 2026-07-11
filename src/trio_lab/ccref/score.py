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
