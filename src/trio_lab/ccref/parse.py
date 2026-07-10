"""Parsing pur du wikitext du wiki LoL → entrées du brouillon cc_reference.

Deux étages :
1. `parse_sources` : page "Types of Crowd Control/Sources" → liste
   (champion, sort, type_cc, repositionnement) ;
2. `parse_ability_fields` + `extract_cc_properties` : template de données d'un
   sort → durée / %slow / zone / fiabilité / disponibilité, extraits par
   heuristiques depuis la prose de la description.

Les heuristiques sont assumées : chaque incertitude alimente `note_relecture`
du brouillon, la **relecture humaine avant gel est obligatoire** (règle 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Sections de la page Sources retenues → type_cc interne (poids PROJECT.md).
# Ignorés sciemment : Berserk, Cripple, Disarm, Disrupt, Kinematics, Knockdown,
# Nearsight, Stasis (hors table de poids du score CC).
SECTION_TO_CC: dict[str, str] = {
    "Airborne": "airborne",
    "Stun": "stun",
    "Root": "root",
    "Silence": "silence",
    "Ground": "ground",
    "Blind": "blind",
    "Slow": "slow",
    "Suppression": "suppression",
    "Drowsy & Sleep": "sleep",
    "Sleep": "sleep",
    "Charm": "charm",
    "Fear / Flee": "fear",
    "Fear": "fear",
    "Taunt": "taunt",
    "Polymorph": "polymorph",
    # Sous-titres `;` constatés sur la page (2026-07-10) :
    # les slows sont listés sous « Movement speed reductions » (section Slow),
    # la suspension (maintien en l'air, même famille non-cleansable que les
    # knock-ups) sous la section Stun.
    "Movement speed reductions": "slow",
    "Suspension": "airborne",
}

# Sous-sections d'Airborne qui déplacent la cible (coef_repositionnement).
# Knock up = « sur place » (PROJECT.md) → pas de repositionnement.
DISPLACING_SUBSECTIONS = frozenset({"Knock aside", "Knock back", "Pull"})

# {{cai|<sort>|<champion>|<affichage?>}} ou {{ai|...}} — param1 = page de
# données du sort (slot Q/W/E/R/I ou nom), param2 = champion.
_TEMPLATE_RE = re.compile(r"\{\{c?ai\|([^|}]+)\|([^|}]+)(?:\|[^}]*)?\}\}")
_HEADER_RE = re.compile(r"^(={3,4})\s*(.+?)\s*\1\s*$")
# {{tip|X}} → X ; {{tip|X|Affichage}} → Affichage.
_TIP_RE = re.compile(r"\{\{tip\|([^|}]+)(?:\|([^}]+))?\}\}")

_FIELD_RES = {
    name: re.compile(rf"^\|\s*{name}\s*=\s*(.*?)\s*$", re.MULTILINE)
    for name in ("skill", "targeting", "description", "description2", "description3")
}
_LEVELING_RE = re.compile(r"^\|\s*leveling\d*\s*=\s*(.*)$", re.MULTILINE)
# {{st|Nom de stat|expression}} — l'expression peut contenir des templates
# imbriqués : on capture le nom et les ~100 caractères suivants pour les nombres.
_STAT_RE = re.compile(r"\{\{st\|([^|{}]+)\|")

# Mots-clés de la prose signalant chaque type de CC (fenêtre de recherche de durée).
_CC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "airborne": ("knock", "pull", "airborne", "hoist", "toss", "drag"),
    "stun": ("stun",),
    "root": ("root", "snare", "immobiliz"),
    "charm": ("charm",),
    "fear": ("fear", "flee", "terrif"),
    "taunt": ("taunt",),
    "sleep": ("sleep", "drowsy"),
    "silence": ("silenc",),
    "ground": ("ground",),
    "blind": ("blind",),
    "polymorph": ("polymorph",),
    "suppression": ("suppress",),
    "slow": ("slow",),
}

# « for {{fd|0.65 seconds}} » (unité DANS le template), « for 1 second »,
# « for {{ap|1 to 2}} seconds »… L'unité peut suivre l'expression ou être
# incluse dedans : validé a posteriori dans `extract_cc_properties`.
_DURATION_RE = re.compile(r"for\s+(\{\{[^{}]+\}\}|[\d.]+)(\s*seconds?)?", re.IGNORECASE)
# « slowing them by {{ap|20% to 40%}} », « by 30% »…
_SLOW_RE = re.compile(r"slow[^.]{0,90}?by\s+(\{\{[^{}]+\}\}|\d+(?:\.\d+)?\s*%)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

# Marqueurs de prose suggérant un CC multi-cibles (heuristique, à relire).
_AREA_MARKERS = (
    "enemies hit",
    "all enemies",
    "enemies within",
    "enemies in ",
    "enemies inside",
    "enemies struck",
    "each enemy",
    "nearby enemies",
    "enemies around",
    "enemy champions hit",
)


@dataclass(frozen=True)
class SourceEntry:
    """Un sort listé dans la page Sources pour un type de CC donné."""

    champion: str
    ability_ref: str  # slot (Q/W/E/R/I) ou nom du sort — cible du template de données
    cc_type: str
    displaces: bool  # repositionnement (airborne : knock back/aside/pull)


@dataclass
class CCProperties:
    """Propriétés extraites de la description d'un sort pour un type de CC."""

    duration_s: float | None = None
    slow_pct: float | None = None
    area: bool = False
    notes: list[str] = field(default_factory=list)


def _clean_header(raw: str) -> str:
    """'{{tip|Fear|Fear / Flee}}' → 'Fear / Flee' ; retire ancres et fichiers."""
    text = _TIP_RE.sub(lambda m: m.group(2) or m.group(1), raw)
    text = re.sub(r"\{\{anchor\|[^}]*\}\}|\[\[File:[^\]]*\]\]", "", text)
    return text.strip()


def parse_sources(wikitext: str) -> list[SourceEntry]:
    """Entrées (champion, sort, type_cc, repositionnement) de la page Sources.

    Gère les sections `===`/`====` et les sous-titres `;{{tip|X}}` (Forced
    Action). Les mentions « (to non-champions) » sont exclues : ce CC ne
    s'applique pas aux champions. Dédoublonné ; un sort listé à la fois en
    knock up et knock back garde `displaces=True`.
    """
    entries: dict[tuple[str, str, str], SourceEntry] = {}
    cc_type: str | None = None
    displaces = False

    for line in wikitext.splitlines():
        header = _HEADER_RE.match(line)
        if header:
            level, title = header.group(1), _clean_header(header.group(2))
            if level == "===":
                cc_type = SECTION_TO_CC.get(title)
                displaces = False
                if title == "Airborne":
                    # Le repositionnement est décidé par sous-section (====).
                    displaces = True  # défaut prudent, précisé par les ====
            else:  # ==== : sous-sections d'Airborne
                displaces = title in DISPLACING_SUBSECTIONS
            continue
        if line.startswith(";"):
            cc_type = SECTION_TO_CC.get(_clean_header(line[1:]))
            displaces = False
            continue
        if cc_type is None:
            continue

        matches = list(_TEMPLATE_RE.finditer(line))
        for i, m in enumerate(matches):
            segment_end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            if "non-champion" in line[m.end() : segment_end]:
                continue
            ability_ref, champion = m.group(1).strip(), m.group(2).strip()
            key = (champion, ability_ref, cc_type)
            previous = entries.get(key)
            entries[key] = SourceEntry(
                champion,
                ability_ref,
                cc_type,
                displaces or (previous.displaces if previous else False),
            )
    return list(entries.values())


def parse_ability_fields(wikitext: str) -> dict[str, object]:
    """Champs utiles d'un template `Data <Champion>/<Sort>`.

    `leveling` = liste `(nom de stat, expression)` extraite des champs
    `leveling`/`leveling2`/… — fallback des durées absentes de la prose
    (ex. « Disable Duration », « Slow Duration »).
    """
    fields = {}
    for name, pattern in _FIELD_RES.items():
        match = pattern.search(wikitext)
        if match:
            fields[name] = match.group(1)
    description = " ".join(
        fields.get(k, "") for k in ("description", "description2", "description3")
    ).strip()
    leveling: list[tuple[str, str]] = []
    for line_match in _LEVELING_RE.finditer(wikitext):
        line = line_match.group(1)
        for stat in _STAT_RE.finditer(line):
            leveling.append((stat.group(1).strip(), line[stat.end() : stat.end() + 100]))
    return {
        "skill": fields.get("skill", ""),
        "targeting": fields.get("targeting", ""),
        "description": description,
        "leveling": leveling,
    }


def _max_number(expr: str) -> float | None:
    numbers = [float(n) for n in _NUMBER_RE.findall(expr)]
    return max(numbers) if numbers else None


def extract_cc_properties(
    description: str,
    cc_type: str,
    leveling: list[tuple[str, str]] = (),
) -> CCProperties:
    """Durée / %slow / zone extraits de la prose, avec notes pour la relecture.

    Durée : premier motif « for X second(s) » dans les 140 caractères suivant un
    mot-clé du type de CC ; si l'expression est un barème ({{ap|1 to 2}}), on
    retient le **max** (valeur au rang max) et on le note. Fallback : stat de
    leveling dont le nom contient « duration ».
    """
    props = CCProperties()
    lowered = description.lower()

    window_start = None
    for keyword in _CC_KEYWORDS[cc_type]:
        pos = lowered.find(keyword)
        if pos != -1 and (window_start is None or pos < window_start):
            window_start = pos
    if window_start is None:
        props.notes.append(f"mot-clé {cc_type} introuvable dans la description")
    else:
        window = description[window_start : window_start + 140]
        duration = None
        for candidate in _DURATION_RE.finditer(window):
            # L'unité doit suivre l'expression ou figurer dedans ({{fd|0.65 seconds}}) —
            # sinon « for 3 enemies » matcherait un faux positif.
            if candidate.group(2) or "second" in candidate.group(1):
                duration = candidate
                break
        if duration:
            expr = duration.group(1)
            props.duration_s = _max_number(expr)
            if len(_NUMBER_RE.findall(expr)) > 1:
                props.notes.append(f"durée variable ({expr}), max retenu")

    if props.duration_s is None:
        for stat_name, expr in leveling:
            if "duration" in stat_name.lower():
                props.duration_s = _max_number(expr)
                if props.duration_s is not None:
                    props.notes.append(f"durée depuis leveling « {stat_name} », max retenu")
                    break
    if props.duration_s is None and cc_type != "slow":
        props.notes.append("durée introuvable")

    if cc_type == "slow":
        slow = _SLOW_RE.search(description)
        if slow:
            props.slow_pct = _max_number(slow.group(1))
            if len(_NUMBER_RE.findall(slow.group(1))) > 1:
                props.notes.append(f"%slow variable ({slow.group(1)}), max retenu")
        else:
            props.notes.append("%slow introuvable")

    props.area = any(marker in lowered for marker in _AREA_MARKERS)
    return props


def reliability_of(targeting: str) -> str:
    """`targeting` du template → fiabilité (point_click si ciblage unitaire/auto)."""
    return "point_click" if re.search(r"unit|auto", targeting, re.IGNORECASE) else "skillshot"


def availability_of(skill: str) -> str:
    """`skill` du template → disponibilité (`ultimate` si R, sinon `base`)."""
    return "ultimate" if skill.strip().upper().startswith("R") else "base"
