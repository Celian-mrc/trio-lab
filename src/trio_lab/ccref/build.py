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
from collections import Counter
from pathlib import Path

from trio_lab import config
from trio_lab.ccref import parse, wiki

logger = logging.getLogger(__name__)

SOURCES_PAGE = "Types of Crowd Control/Sources"
DRAFT_PATH = config.PROJECT_ROOT / "data" / "external" / "cc_reference.draft.csv"

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
# Colonnes : zone ∈ {{mono, multi}} (heuristique), fiabilite ∈ {{point_click, skillshot}},
# disponibilite ∈ {{base, ultimate}}, repositionnement ∈ {{0, 1}} (airborne déplaçant).
# Les coefficients (poids par type, coef_zone…) vivent dans la config, pas ici.
"""


def _data_page(entry: parse.SourceEntry) -> str:
    return f"Template:Data {entry.champion}/{entry.ability_ref}"


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
                "note_relecture": " ; ".join(props.notes + form_note),
            }
        )
        if props.duration_s is None:
            fandom_candidates.append((len(rows) - 1, entry))

    _fill_from_fandom(rows, fandom_candidates)
    rows.sort(key=lambda r: (r["champion"], r["sort"], r["type_cc"]))
    return rows


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
    """2e passe : durées manquantes cherchées sur l'ancien wiki (Fandom).

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
        if props.duration_s is None:
            continue
        row = rows[index]
        row["duree_s"] = props.duration_s
        notes = [
            n for n in str(row["note_relecture"]).split(" ; ") if n and n != "durée introuvable"
        ]
        extra = [n for n in props.notes if "moyenne retenue" in n]
        notes.append("durée via wiki Fandom (contenu possiblement daté)")
        row["note_relecture"] = " ; ".join(notes + extra)
        if row["pct_slow"] == "" and props.slow_pct is not None:
            row["pct_slow"] = props.slow_pct
        filled += 1
    logger.info("2e passe Fandom : %d durées complétées sur %d candidates", filled, len(candidates))


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
