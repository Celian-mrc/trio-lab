"""Assemblage du brouillon `cc_reference.draft.csv` (Phase 2b, one-shot).

Pipeline : page Sources → entrées par type de CC → fetch batch des templates de
données des sorts → extraction des propriétés → CSV brouillon trié, avec
en-tête d'attribution CC BY-SA et colonne `note_relecture`.

Le brouillon n'est PAS le fichier final : relecture humaine obligatoire puis
gel en `data/external/cc_reference.csv` (règle 4).
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import re
from collections import Counter
from pathlib import Path

from trio_lab import config
from trio_lab.ccref import parse, wiki

logger = logging.getLogger(__name__)

SOURCES_PAGE = "Types of Crowd Control/Sources"
DRAFT_PATH = config.PROJECT_ROOT / "data" / "external" / "cc_reference.draft.csv"
# Arbitrages de relecture humaine, appliqués APRÈS toutes les passes automatiques
# (ils priment) : les décisions de Célian survivent ainsi aux régénérations.
OVERRIDES_PATH = config.PROJECT_ROOT / "data" / "external" / "cc_reference.overrides.csv"

CSV_COLUMNS = [
    "champion",
    "sort",
    "type_cc",
    "duree_s",
    "pct_slow",
    "zone",
    "fiabilite",
    "disponibilite",
    "repositionnement",
    "conditionnel",
    "note_relecture",
]

_HEADER_COMMENT = """\
# cc_reference.draft.csv — BROUILLON généré le {date} par `python -m trio_lab.ccref`.
# NE PAS FIGER SANS RELECTURE HUMAINE (CLAUDE.md règle 4) : durées/%slow extraits
# par heuristiques depuis la prose du wiki ; toute ligne avec `note_relecture`
# non vide demande une vérification. Après relecture : renommer en
# cc_reference.csv (fichier immuable, re-versionné à chaque rework).
#
# Source : League of Legends Wiki — « {page} » et pages de sorts,
# via l'API MediaWiki (https://wiki.leagueoflegends.com/en-us/api.php).
# Contenu dérivé sous licence CC BY-SA 4.0 ; attribution : League of Legends Wiki
# (https://wiki.leagueoflegends.com), communauté des contributeurs.
# League of Legends et Riot Games sont des marques de Riot Games, Inc.
#
# Colonnes : zone ∈ {{mono, multi}} (heuristique), fiabilite ∈ {{point_click, skillshot}}
# (le ciblage, indépendant de la condition), disponibilite ∈ {{base, ultimate}},
# repositionnement ∈ {{0, 1}} (airborne déplaçant), conditionnel ∈ {{0, 1}} (CC sous
# condition : collision terrain, charge complète — E de Poppy, Q de Bard).
# Les coefficients (poids par type, coef_zone, coef_conditionnel…) vivent dans la
# config, pas ici.
"""


def _data_page(entry: parse.SourceEntry) -> str:
    return f"Template:Data {entry.champion}/{entry.ability_ref}"


# Défauts de durée des CC de déplacement jamais chiffrés par les wikis
# (principe validé par Célian le 2026-07-10) : appliqués en dernier recours
# après les deux wikis, TOUJOURS annotés « défaut appliqué » pour la relecture.
def default_duration(cc_type: str, displaces: bool, wall: bool) -> tuple[float, str] | None:
    """(durée par défaut, libellé) pour un CC sans durée chiffrée, sinon None."""
    if cc_type == "airborne":
        if wall:
            return 0.25, "knock-up très court (mur/pilier)"
        if displaces:
            return 0.5, "knockback/pull"
        return 0.75, "knock-up"
    if cc_type == "suppression":
        return 1.25, "suppression liée à un déplacement"
    return None


def _apply_defaults(rows: list[dict[str, object]], walls: dict[int, bool]) -> int:
    """Applique les défauts aux lignes restées sans durée. Retourne le nombre appliqué."""
    applied = 0
    for index, row in enumerate(rows):
        if row["duree_s"] != "":
            continue
        default = default_duration(
            str(row["type_cc"]), bool(row["repositionnement"]), walls.get(index, False)
        )
        if default is None:
            continue
        value, label = default
        row["duree_s"] = value
        notes = [
            n for n in str(row["note_relecture"]).split(" ; ") if n and n != "durée introuvable"
        ]
        notes.append(f"défaut appliqué : {label} {value} s — à vérifier")
        row["note_relecture"] = " ; ".join(notes)
        applied += 1
    return applied


def build_rows() -> list[dict[str, object]]:
    """Interroge le wiki et construit les lignes du brouillon (triées)."""
    sources_wikitext = wiki.fetch_wikitext(SOURCES_PAGE)
    entries = parse.parse_sources(sources_wikitext)
    logger.info(
        "%d entrées depuis « %s » : %s",
        len(entries),
        SOURCES_PAGE,
        dict(Counter(e.cc_type for e in entries)),
    )

    pages = wiki.fetch_many(sorted({_data_page(e) for e in entries}))
    _retry_forms(pages, entries)

    rows: list[dict[str, object]] = []
    fandom_candidates: list[tuple[int, parse.SourceEntry]] = []
    walls: dict[int, bool] = {}  # index de ligne → le sort crée un mur/pilier
    for entry in entries:
        champion = parse.FORM_TO_CHAMPION.get(entry.champion, entry.champion)
        form_note = [f"forme {entry.champion}"] if champion != entry.champion else []
        wikitext = pages.get(_data_page(entry))
        if wikitext is None:
            rows.append(
                {
                    "champion": champion,
                    "sort": parse.slot_label(entry.ability_ref),
                    "type_cc": entry.cc_type,
                    "duree_s": "",
                    "pct_slow": "",
                    "zone": "",
                    "fiabilite": "",
                    "disponibilite": "",
                    "repositionnement": int(entry.displaces),
                    "conditionnel": 0,
                    "note_relecture": " ; ".join(
                        ["page de données introuvable sur le wiki", *form_note]
                    ),
                }
            )
            continue
        fields = parse.parse_ability_fields(wikitext)
        props = parse.extract_cc_properties(
            fields["description"], entry.cc_type, fields["leveling"]
        )
        rows.append(
            {
                "champion": champion,
                "sort": parse.slot_label(fields["skill"] or entry.ability_ref),
                "type_cc": entry.cc_type,
                "duree_s": "" if props.duration_s is None else props.duration_s,
                "pct_slow": "" if props.slow_pct is None else props.slow_pct,
                "zone": "multi" if props.area else "mono",
                "fiabilite": parse.reliability_of(fields["targeting"]),
                "disponibilite": parse.availability_of(fields["skill"]),
                "repositionnement": int(entry.displaces),
                "conditionnel": int(props.conditional),
                "note_relecture": " ; ".join(props.notes + form_note),
            }
        )
        walls[len(rows) - 1] = bool(
            re.search(r"\bwall|\bpillar", fields["description"], re.IGNORECASE)
        )
        if props.duration_s is None or (entry.cc_type == "slow" and props.slow_pct is None):
            fandom_candidates.append((len(rows) - 1, entry))

    _fill_from_fandom(rows, fandom_candidates)
    applied = _apply_defaults(rows, walls)
    logger.info("défauts de durée appliqués : %d lignes", applied)
    rows = apply_overrides(rows, load_overrides())
    rows.sort(key=lambda r: (r["champion"], r["sort"], r["type_cc"]))
    return rows


def load_overrides(path: Path = OVERRIDES_PATH) -> list[dict[str, str]]:
    """Charge les arbitrages de relecture (liste ordonnée ; vide si pas de fichier)."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(line for line in fh if not line.startswith("#")))


def apply_overrides(
    rows: list[dict[str, object]], overrides: list[dict[str, str]]
) -> list[dict[str, object]]:
    """Applique les arbitrages humains : `set` écrase les champs fournis,
    `exclude` retire la ligne. Les overrides s'appliquent dans l'ordre du
    fichier ; chaque override consomme la première ligne correspondante non
    encore traitée (gère les doublons de clé). Un override sans correspondance
    est signalé (ligne disparue du wiki, typo…).
    """
    consumed: set[int] = set()
    excluded: set[int] = set()
    for override in overrides:
        key = (override["champion"], override["sort"], override["type_cc"])
        index = next(
            (
                i
                for i, row in enumerate(rows)
                if i not in consumed and (row["champion"], str(row["sort"]), row["type_cc"]) == key
            ),
            None,
        )
        if index is None:
            logger.warning("override sans correspondance : %s %s %s", *key)
            continue
        consumed.add(index)
        if override["action"] == "exclude":
            excluded.add(index)
            continue
        row = rows[index]
        for field, column in (("duree_s", "duree_s"), ("pct_slow", "pct_slow")):
            if override[field].strip():
                row[column] = float(override[field])
        if override["conditionnel"].strip():
            row["conditionnel"] = int(override["conditionnel"])
        row["note_relecture"] = f"relecture : {override['note']}"
    kept = [row for i, row in enumerate(rows) if i not in excluded]
    logger.info(
        "overrides appliqués : %d set, %d exclusions (%d lignes restantes)",
        len(consumed) - len(excluded),
        len(excluded),
        len(kept),
    )
    return kept


def _retry_forms(pages: dict[str, str | None], entries: list[parse.SourceEntry]) -> None:
    """Pages absentes des formes alternatives : retente sous le champion réel.

    Ex. « Template:Data Rhaast/Blade's Reach R » absent → la page vit parfois
    sous « Template:Data Kayn/… ».
    """
    retry = {
        _data_page(e): f"Template:Data {parse.FORM_TO_CHAMPION[e.champion]}/{e.ability_ref}"
        for e in entries
        if e.champion in parse.FORM_TO_CHAMPION and pages.get(_data_page(e)) is None
    }
    if not retry:
        return
    fetched = wiki.fetch_many(sorted(set(retry.values())))
    for original_title, mapped_title in retry.items():
        pages[original_title] = fetched.get(mapped_title)


def _fill_from_fandom(
    rows: list[dict[str, object]],
    candidates: list[tuple[int, parse.SourceEntry]],
) -> None:
    """2e passe : durées et %slow manquants cherchés sur l'ancien wiki (Fandom).

    Même moteur MediaWiki et mêmes templates, prose souvent plus chiffrée.
    Contenu possiblement daté (wiki migré en 2024) → chaque valeur récupérée
    reste annotée pour la relecture.
    """
    if not candidates:
        return
    pages = wiki.fetch_many(
        sorted({_data_page(e) for _, e in candidates}), api_url=wiki.FANDOM_API_URL
    )
    filled = 0
    for index, entry in candidates:
        wikitext = pages.get(_data_page(entry))
        if wikitext is None:
            continue
        fields = parse.parse_ability_fields(wikitext)
        props = parse.extract_cc_properties(
            fields["description"], entry.cc_type, fields["leveling"]
        )
        row = rows[index]
        resolved = []
        if row["duree_s"] == "" and props.duration_s is not None:
            row["duree_s"] = props.duration_s
            resolved.append("durée introuvable")
        if row["pct_slow"] == "" and props.slow_pct is not None:
            row["pct_slow"] = props.slow_pct
            resolved.append("%slow introuvable")
        if not resolved:
            continue
        notes = [n for n in str(row["note_relecture"]).split(" ; ") if n and n not in resolved]
        notes.extend(n for n in props.notes if "moyenne retenue" in n)
        notes.append("complété via wiki Fandom (contenu possiblement daté)")
        row["note_relecture"] = " ; ".join(dict.fromkeys(notes))
        filled += 1
    logger.info("2e passe Fandom : %d lignes complétées sur %d candidates", filled, len(candidates))


def write_draft(rows: list[dict[str, object]], path: Path = DRAFT_PATH) -> Path:
    """Écrit le brouillon CSV avec l'en-tête d'attribution."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(_HEADER_COMMENT.format(date=dt.date.today().isoformat(), page=SOURCES_PAGE))
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    to_review = sum(1 for r in rows if r["note_relecture"])
    logger.info("%d lignes écrites dans %s (%d à relire)", len(rows), path, to_review)
    return path
