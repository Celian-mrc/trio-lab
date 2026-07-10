"""Tests du parseur wikitext (fixtures réelles archivées du wiki, 2026-07-10).

Les fixtures sont figées : ces tests ne dépendent pas du wiki en ligne. Si le
wiki restructure ses pages, relancer l'import échouera bruyamment et les
fixtures seront re-archivées avec le diff relu (procédure de re-versionnage).
"""

from __future__ import annotations

from pathlib import Path

from trio_lab.ccref import parse

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- parse_sources (page réelle complète) ---


def test_sources_covers_expected_cc_types():
    entries = parse.parse_sources(_fixture("cc_sources.wikitext"))
    types = {e.cc_type for e in entries}
    # Polymorph vit en sous-titre `;` de la section Silence (constat 16.13).
    assert types == {
        "airborne",
        "blind",
        "charm",
        "fear",
        "ground",
        "polymorph",
        "root",
        "silence",
        "sleep",
        "slow",
        "stun",
        "suppression",
        "taunt",
    }
    # Volume plausible : des centaines de sorts, aucun doublon exact.
    assert len(entries) > 250
    keys = [(e.champion, e.ability_ref, e.cc_type) for e in entries]
    assert len(keys) == len(set(keys))


def test_sources_airborne_displacement_by_subsection():
    entries = parse.parse_sources(_fixture("cc_sources.wikitext"))
    by_key = {(e.champion, e.ability_ref, e.cc_type): e for e in entries}
    # Pull → repositionnement ; le Q de Blitzcrank déplace.
    assert by_key[("Blitzcrank", "Q", "airborne")].displaces is True
    # Knock up pur (Q d'Alistar) → pas de repositionnement.
    assert by_key[("Alistar", "Q", "airborne")].displaces is False
    # Listé en knock back ET knock up (E de Poppy) → True l'emporte.
    assert by_key[("Poppy", "E", "airborne")].displaces is True


def test_sources_forced_action_subheaders():
    entries = parse.parse_sources(_fixture("cc_sources.wikitext"))
    by_champ_type = {(e.champion, e.cc_type) for e in entries}
    assert ("Ahri", "charm") in by_champ_type
    assert ("Rammus", "taunt") in by_champ_type
    assert ("Nocturne", "fear") in by_champ_type
    # Berserk (Renata R) hors table de poids → ignoré.
    assert ("Renata Glasc", "berserk") not in {(e.champion, e.cc_type) for e in entries}


def test_sources_excludes_non_champion_cc():
    entries = parse.parse_sources(_fixture("cc_sources.wikitext"))
    keys = {(e.champion, e.ability_ref, e.cc_type) for e in entries}
    # « {{cai|R|Aatrox}} (to non-champions) » sous Fear → exclu.
    assert ("Aatrox", "R", "fear") not in keys
    # « {{cai|W|Sejuani}} (to non-champions) and {{ai|E|Sejuani}} » → W exclu, E gardé.
    assert ("Sejuani", "W", "airborne") not in keys
    assert ("Sejuani", "E", "airborne") in keys


def test_sources_ignores_items_and_units():
    entries = parse.parse_sources(_fixture("cc_sources.wikitext"))
    champions = {e.champion for e in entries}
    assert "Baron Nashor" not in champions
    assert "Rift Herald" not in champions


# --- parse_ability_fields + extract_cc_properties (templates réels) ---


def test_rocket_grab_pull_has_no_prose_duration():
    """La durée d'un pull n'est pas écrite dans la prose : None + note de relecture."""
    fields = parse.parse_ability_fields(_fixture("data_rocket_grab.wikitext"))
    assert fields["skill"] == "Q"
    assert parse.reliability_of(fields["targeting"]) == "skillshot"  # Direction
    assert parse.availability_of(fields["skill"]) == "base"
    props = parse.extract_cc_properties(fields["description"], "airborne", fields["leveling"])
    assert props.duration_s is None
    assert any("durée introuvable" in n for n in props.notes)


def test_charm_says_for_a_duration_only():
    """Ahri : « for a duration » sans chiffre ni leveling → None + note (à relire)."""
    fields = parse.parse_ability_fields(_fixture("data_charm.wikitext"))
    props = parse.extract_cc_properties(fields["description"], "charm", fields["leveling"])
    assert props.duration_s is None
    assert any("durée introuvable" in n for n in props.notes)


def test_whimsy_duration_from_leveling_fallback():
    """Lulu W : durée absente de la prose mais « Disable Duration » en leveling."""
    fields = parse.parse_ability_fields(_fixture("data_whimsy.wikitext"))
    assert parse.reliability_of(fields["targeting"]) == "point_click"  # Unit
    props = parse.extract_cc_properties(fields["description"], "polymorph", fields["leveling"])
    assert props.duration_s == 2.0  # {{ap|1.2 to 2}} → max
    assert any("leveling" in n for n in props.notes)


# --- heuristiques sur descriptions synthétiques ---


def test_duration_takes_max_of_range_with_note():
    props = parse.extract_cc_properties(
        "{{tip|stun|stunning}} them for {{ap|1 to 2}} seconds", "stun"
    )
    assert props.duration_s == 2.0
    assert any("max retenu" in n for n in props.notes)


def test_duration_single_value_inside_template():
    props = parse.extract_cc_properties(
        "{{tip|stun|stunning}} them for {{fd|0.65 seconds}}", "stun"
    )
    assert props.duration_s == 0.65
    assert props.notes == []


def test_missing_duration_is_noted():
    props = parse.extract_cc_properties("{{tip|stun|stunning}} the target briefly", "stun")
    assert props.duration_s is None
    assert any("durée introuvable" in n for n in props.notes)


def test_slow_percentage_max_of_range():
    props = parse.extract_cc_properties(
        "{{tip|slow|slowing}} them by {{ap|20% to 40%}} for 2 seconds", "slow"
    )
    assert props.slow_pct == 40.0
    assert props.duration_s == 2.0


def test_area_heuristic():
    multi = parse.extract_cc_properties(
        "{{tip|stun|stunning}} all enemies hit for 1 second", "stun"
    )
    mono = parse.extract_cc_properties(
        "{{tip|stun|stunning}} the first enemy hit for 1 second", "stun"
    )
    assert multi.area is True
    assert mono.area is False
