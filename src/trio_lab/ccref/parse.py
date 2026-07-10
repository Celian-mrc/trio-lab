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

# Formes alternatives listées comme « champion » par le wiki → champion réel.
# Rhaast = la forme Darkin de Kayn (le wiki donne une page par forme).
FORM_TO_CHAMPION: dict[str, str] = {"Rhaast": "Kayn"}

# {{cai|<sort>|<champion>|<affichage?>}} ou {{ai|...}} — param1 = page de
# données du sort (slot Q/W/E/R/I ou nom), param2 = champion.
_TEMPLATE_RE = re.compile(r"\{\{c?ai\|([^|}]+)\|([^|}]+)(?:\|[^}]*)?\}\}")
_HEADER_RE = re.compile(r"^(={3,4})\s*(.+?)\s*\1\s*$")
# {{tip|X}} → X ; {{tip|X|Affichage}} → Affichage.
_TIP_RE = re.compile(r"\{\{tip\|([^|}]+)(?:\|([^}]+))?\}\}")

_FIELD_RES = {
    name: re.compile(rf"^\|\s*{name}\s*=\s*(.*?)\s*$", re.MULTILINE)
    for name in ("skill", "targeting")
}
# Les champs description peuvent s'étaler sur PLUSIEURS lignes/paragraphes
# (constaté sur Fandom : le knockback conditionnel du E de Briar est un 2e
# paragraphe du même champ) : capture jusqu'au prochain champ `|xxx =` en
# début de ligne.
_DESCRIPTION_RE = re.compile(
    r"^\|\s*description\d*\s*=\s*(.*?)(?=^\|\s*[a-zA-Z][\w. ]*=|\Z)",
    re.MULTILINE | re.DOTALL,
)
# {{st|Nom de stat|expression}} — stats de leveling. Les champs `leveling`
# s'étalent souvent sur PLUSIEURS lignes (constaté : « Stun Duration » d'Anivia
# sur une ligne de continuation), donc on scanne tout le wikitext ; l'expression
# peut contenir des templates imbriqués → on capture les ~100 caractères
# suivants pour les nombres.
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
# « for {{ap|1 to 2}} seconds », « knocks them back over {{fd|0.5 seconds}} »
# (les airbornes utilisent souvent « over »)… L'unité peut suivre l'expression
# ou être incluse dedans : validé a posteriori dans `extract_cc_properties`.
_DURATION_RE = re.compile(r"(?:for|over)\s+(\{\{[^{}]+\}\}|[\d.]+)(\s*seconds?)?", re.IGNORECASE)
# « slowing them by {{ap|20% to 40%}} », « by 30% »…
_SLOW_RE = re.compile(r"slow[^.]{0,90}?by\s+(\{\{[^{}]+\}\}|\d+(?:\.\d+)?\s*%)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# « 1.25 to 1.75 » : bornes d'un barème par rang/niveau.
_TO_PAIR_RE = re.compile(r"([\d.]+)\s*to\s*([\d.]+)")
# CC subi par le LANCEUR, voix passive : « charges while being {{tip|slow|slowed}} »
# (W de Viego, Q de Vi). Appliqué aux ~30 caractères précédant le mot-clé — le
# mot-clé peut être le 2e paramètre du template ({{tip|slow|slowed}}), d'où le
# préfixe optionnel `{{tip|xxx|`.
_SELF_BEFORE_RE = re.compile(
    r"(?:while\s+being|becoming)\s*(?:\{\{tip\|(?:[\w ]+\|)?)?$", re.IGNORECASE
)
# Durée de CC dur au-delà de ce seuil = extraction très probablement fausse
# (ex. niveau attrapé dans un {{pp}}) → valeur écartée, note de relecture.
MAX_PLAUSIBLE_HARD_CC_S = 4.0

# Marqueurs de prose d'un CC CONDITIONNEL : collision avec le terrain (E de
# Poppy, Q de Bard, E de Briar), charge complète requise… Heuristique, à relire.
_CONDITION_MARKERS = (
    "collid",
    "against terrain",
    "into terrain",
    "hits terrain",  # « If the target hits terrain » (E de Poppy)
    "hits terrain or",  # « If the bolt hits terrain or a second enemy » (Q de Bard)
    "against a wall",
    "into a wall",
    "hits a wall",
    "if fully charged",
    "charged for its full duration",
    "if the charge",
)

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
    conditional: bool = False  # CC sous condition (collision terrain, charge…)
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
        " ".join(block.split()) for block in _DESCRIPTION_RE.findall(wikitext)
    ).strip()
    leveling = [
        (stat.group(1).strip(), wikitext[stat.end() : stat.end() + 100])
        for stat in _STAT_RE.finditer(wikitext)
    ]
    return {
        "skill": fields.get("skill", ""),
        "targeting": fields.get("targeting", ""),
        "description": description,
        "leveling": leveling,
    }


def _template_value_parts(expr: str) -> list[str]:
    """Paramètres positionnels porteurs de valeurs d'un template de barème.

    `{{pp|changedisplay=true|1.25 to 1.75 for 3|1 to 13|type=…}}` : les params
    `clé=valeur` sont ignorés et seul le PREMIER positionnel porte les valeurs
    (les suivants sont les niveaux/rangs du barème — le « 13 » de Braum est un
    niveau, pas une durée). Pour `{{ap|…}}`/`{{fd|…}}`, tout est valeur.
    """
    match = re.fullmatch(r"\{\{(\w+)\|(.*)\}\}", expr, re.DOTALL)
    if not match:
        return [expr]
    name, body = match.group(1).lower(), match.group(2)
    positional = [part for part in body.split("|") if "=" not in part]
    if not positional:
        return [body]
    return positional[:1] if name == "pp" else positional


def _mean_number(expr: str) -> float | None:
    """Valeur moyenne d'une expression de barème (« 1 to 2 » → 1.5).

    Dans un segment « X to Y … », seules les bornes comptent (« 1.25 to 1.75
    for 3 » : le « for 3 » est un pas de niveaux, pas une valeur).
    """
    values: list[float] = []
    for part in _template_value_parts(expr):
        pair = _TO_PAIR_RE.search(part)
        if pair:
            values.extend((float(pair.group(1)), float(pair.group(2))))
        else:
            values.extend(float(n) for n in _NUMBER_RE.findall(part))
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def extract_cc_properties(
    description: str,
    cc_type: str,
    leveling: list[tuple[str, str]] = (),
) -> CCProperties:
    """Durée / %slow / zone extraits de la prose, avec notes pour la relecture.

    Durée : premier motif « for/over X second(s) » dans les 140 caractères
    suivant un mot-clé du type de CC ; un barème ({{ap|1 to 2}}) retient la
    **moyenne** des bornes (choix de relecture, 2026-07-10). Fallback : stat de
    leveling dont le nom contient « duration ». Une durée de CC dur au-delà de
    `MAX_PLAUSIBLE_HARD_CC_S` est écartée (extraction probablement fausse).
    """
    props = CCProperties()
    lowered = description.lower()

    # TOUTES les occurrences des mots-clés, en ordre de texte : la première
    # mention d'un CC est souvent sans durée (annonce), le chiffre arrivant
    # dans le recast ou une condition (constaté : R de Yasuo, E de Briar).
    all_positions = sorted(
        {
            match.start()
            for keyword in _CC_KEYWORDS[cc_type]
            for match in re.finditer(re.escape(keyword), lowered)
        }
    )
    # Mentions appliquées au LANCEUR (« slowing himself », « charges while
    # being slowed » : W de Viego, Q de Vi…) : pas un CC ennemi, fenêtres
    # écartées. Si TOUTES les mentions d'un slow sont self, la ligne entière
    # est suspecte — la page Sources liste ces sorts à cause du malus du lanceur.
    positions = [p for p in all_positions if not _is_self_mention(description, lowered, p)]
    if cc_type == "slow" and all_positions and not positions:
        props.notes.append("slow appliqué au lanceur (self), pas un CC ennemi — à exclure ?")
        return props
    if not positions:
        props.notes.append(f"mot-clé {cc_type} introuvable dans la description")
    for window_start in positions:
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
            props.duration_s = _mean_number(expr)
            if len(_NUMBER_RE.findall(expr)) > 1:
                props.notes.append(f"durée variable ({expr}), moyenne retenue")
            break

    if props.duration_s is None:
        for stat_name, expr in _duration_stats_for(cc_type, leveling):
            props.duration_s = _mean_number(expr)
            if props.duration_s is not None:
                props.notes.append(f"durée depuis leveling « {stat_name} », moyenne retenue")
                break

    if (
        props.duration_s is not None
        and cc_type != "slow"
        and props.duration_s > MAX_PLAUSIBLE_HARD_CC_S
    ):
        props.notes.append(
            f"durée extraite {props.duration_s} s invraisemblable "
            f"(> {MAX_PLAUSIBLE_HARD_CC_S} s), écartée — à vérifier"
        )
        props.duration_s = None

    if props.duration_s is None and cc_type != "slow":
        props.notes.append("durée introuvable")

    if cc_type == "slow":
        slow = next(
            (
                m
                for m in _SLOW_RE.finditer(description)
                if not _is_self_mention(description, lowered, m.start())
            ),
            None,
        )
        if slow:
            props.slow_pct = _mean_number(slow.group(1))
            if len(_NUMBER_RE.findall(slow.group(1))) > 1:
                props.notes.append(f"%slow variable ({slow.group(1)}), moyenne retenue")
        if props.slow_pct is None:
            # Le % vit souvent en leveling seulement : stat « Slow » (Poppy Q)
            # ou « Movement Speed Modifier » (Orianna W). On tronque au premier
            # « % » : ce qui suit (cooldown, champs voisins) n'est pas la valeur.
            for stat_name, expr in leveling:
                name = stat_name.lower()
                if ("slow" in name or "movement speed" in name) and "%" in expr:
                    props.slow_pct = _mean_number(expr.split("%")[0])
                    if props.slow_pct is not None:
                        props.notes.append(
                            f"%slow depuis leveling « {stat_name} », moyenne retenue"
                        )
                        break
        if props.slow_pct is None:
            props.notes.append("%slow introuvable")

    props.area = any(marker in lowered for marker in _AREA_MARKERS)
    # Conditionnel : marqueur à proximité d'une mention du CC (±160 caractères) —
    # pas sur toute la description, où une autre partie du sort pourrait porter
    # la condition.
    props.conditional = any(
        marker in lowered[max(0, pos - 160) : pos + 160]
        for pos in positions
        for marker in _CONDITION_MARKERS
    )
    if props.conditional:
        props.notes.append("conditionnel (collision/charge) — heuristique, à vérifier")
    return props


def _is_self_mention(description: str, lowered: str, pos: int) -> bool:
    """Vrai si la mention de CC en `pos` s'applique au lanceur, pas à un ennemi."""
    if "self" in lowered[pos : pos + 30]:
        return True
    return bool(_SELF_BEFORE_RE.search(description[max(0, pos - 30) : pos]))


def _duration_stats_for(cc_type: str, leveling: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Stats de leveling « … Duration » utilisables pour ce type de CC.

    Une stat nommée d'après un AUTRE type (« Stun Duration » pour un slow) est
    écartée ; les noms génériques (« Disable Duration ») restent acceptés, en
    préférant toujours une stat qui nomme le type cherché.
    """
    own_keywords = _CC_KEYWORDS[cc_type]
    other_keywords = [kw for t, kws in _CC_KEYWORDS.items() if t != cc_type for kw in kws]
    matching, generic = [], []
    for stat_name, expr in leveling:
        name = stat_name.lower()
        if "duration" not in name:
            continue
        if any(kw in name for kw in own_keywords):
            matching.append((stat_name, expr))
        elif not any(kw in name for kw in other_keywords):
            generic.append((stat_name, expr))
    return matching + generic


def reliability_of(targeting: str) -> str:
    """`targeting` du template → fiabilité.

    Les sorts multi-parties portent plusieurs ciblages ({{dv|Direction|Auto}}
    sur le Q de Thresh : lancer visé puis recast automatique) : la présence
    d'un ciblage visé (Direction/Location) prime — c'est lui qu'il faut
    toucher. Point-and-click seulement si le sort est purement Unit/Auto.
    """
    if re.search(r"direction|location|vector", targeting, re.IGNORECASE):
        return "skillshot"
    if re.search(r"unit|auto", targeting, re.IGNORECASE):
        return "point_click"
    return "skillshot"


def availability_of(skill: str) -> str:
    """`skill` du template → disponibilité (`ultimate` si R, sinon `base`)."""
    return "ultimate" if skill.strip().upper().startswith("R") else "base"


def slot_label(skill: str) -> str:
    """Libellé du slot pour le CSV : le wiki note les passifs `I` (Innate) → `P`."""
    cleaned = skill.strip().upper()
    return "P" if cleaned == "I" else skill.strip()
