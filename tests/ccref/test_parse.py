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


def test_charm_duration_from_multiline_leveling():
    """Ahri : « for a duration » en prose, mais « Disable Duration » en leveling
    (ligne de continuation — le champ leveling s'étale sur plusieurs lignes)."""
    fields = parse.parse_ability_fields(_fixture("data_charm.wikitext"))
    props = parse.extract_cc_properties(fields["description"], "charm", fields["leveling"])
    assert props.duration_s is not None
    assert any("leveling" in n for n in props.notes)


def test_leveling_stat_of_other_cc_type_is_rejected():
    """« Stun Duration » ne doit pas fournir la durée d'un slow (cas Anivia Q)."""
    props = parse.extract_cc_properties(
        "{{tip|slow|slows}} them briefly",
        "slow",
        [("Stun Duration", "{{ap|1.1 to 1.5}} seconds")],
    )
    assert props.duration_s is None
    props_stun = parse.extract_cc_properties(
        "{{tip|stun|stuns}} them briefly",
        "stun",
        [("Stun Duration", "{{ap|1.1 to 1.5}} seconds")],
    )
    assert props_stun.duration_s == 1.3


def test_whimsy_duration_from_leveling_fallback():
    """Lulu W : durée absente de la prose mais « Disable Duration » en leveling."""
    fields = parse.parse_ability_fields(_fixture("data_whimsy.wikitext"))
    assert parse.reliability_of(fields["targeting"]) == "point_click"  # Unit
    props = parse.extract_cc_properties(fields["description"], "polymorph", fields["leveling"])
    assert props.duration_s == 1.6  # {{ap|1.2 to 2}} → moyenne des bornes
    assert any("leveling" in n for n in props.notes)


# --- heuristiques sur descriptions synthétiques ---


def test_duration_takes_mean_of_range_with_note():
    props = parse.extract_cc_properties(
        "{{tip|stun|stunning}} them for {{ap|1 to 2}} seconds", "stun"
    )
    assert props.duration_s == 1.5  # moyenne des bornes du barème
    assert any("moyenne retenue" in n for n in props.notes)


def test_duration_pp_template_ignores_level_breakpoints():
    """Barème par niveau (passif de Braum) : le « 1 to 13 » est un niveau, pas une durée."""
    props = parse.extract_cc_properties(
        "{{tip|stun|stunning}} them for "
        "{{pp|changedisplay=true|1.25 to 1.75 for 3|1 to 13|type=his level|label1=level}} seconds",
        "stun",
    )
    assert props.duration_s == 1.5  # moyenne de 1.25-1.75, le 13 (niveau) ignoré


def test_implausible_hard_cc_duration_is_discarded():
    props = parse.extract_cc_properties("{{tip|stun|stunning}} them for 13 seconds", "stun")
    assert props.duration_s is None
    assert any("invraisemblable" in n for n in props.notes)


def test_airborne_duration_with_over_wording():
    """Les airbornes disent souvent « knocks back over X seconds » plutôt que « for »."""
    props = parse.extract_cc_properties(
        "{{tip|airborne|knocks them back}} 700 units over {{fd|0.5 seconds}}", "airborne"
    )
    assert props.duration_s == 0.5


def test_slot_label_maps_innate_to_passive():
    assert parse.slot_label("I") == "P"
    assert parse.slot_label("Q") == "Q"
    assert parse.slot_label("Bandage Toss") == "Bandage Toss"


def test_reliability_aimed_part_wins_on_multipart_abilities():
    """Q de Thresh : {{dv|Direction|Auto}} (lancer visé + recast auto) = skillshot."""
    assert (
        parse.reliability_of("{{dv|[[Direction-targeted|Direction]]|[[Auto-targeted|Auto]]}}")
        == "skillshot"
    )
    assert (
        parse.reliability_of("{{dv|[[Auto-targeted|Auto]]|[[Direction-targeted|Direction]]}}")
        == "skillshot"
    )
    assert parse.reliability_of("[[Unit-targeted|Unit]]") == "point_click"
    assert parse.reliability_of("") == "skillshot"


def test_duration_found_on_later_keyword_occurrence():
    """R de Yasuo : la 1re mention 'airborne' (condition de cast) n'a pas de durée,
    le chiffre arrive sur l'occurrence suivante (knock up du recast)."""
    props = parse.extract_cc_properties(
        "targets an {{tip|airborne}} enemy champion nearest to the cursor, then "
        "{{tip|airborne|knocks up}} all nearby enemies for 1 second",
        "airborne",
    )
    assert props.duration_s == 1.0


def test_form_champions_map_to_real_champion():
    assert parse.FORM_TO_CHAMPION["Rhaast"] == "Kayn"


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


def test_slow_percentage_mean_of_range():
    props = parse.extract_cc_properties(
        "{{tip|slow|slowing}} them by {{ap|20% to 40%}} for 2 seconds", "slow"
    )
    assert props.slow_pct == 30.0  # moyenne des bornes
    assert props.duration_s == 2.0


def test_slow_percentage_from_leveling_stat():
    """Q de Poppy : « slows enemies within » sans chiffre, stat « Slow » en leveling."""
    props = parse.extract_cc_properties(
        "{{tip|slow|slows}} enemies within, which then ruptures",
        "slow",
        [("Slow", "{{ap|20 to 40}}%}}\n{{st|Total Physical Damage|...")],
    )
    assert props.slow_pct == 30.0
    assert any("leveling" in n for n in props.notes)


def test_slow_percentage_from_movement_speed_stat_truncated_at_percent():
    """W d'Orianna : stat « Movement Speed Modifier » ; les nombres après le %
    (cooldown, champs voisins) ne doivent pas polluer la moyenne."""
    props = parse.extract_cc_properties(
        "{{tip|slow|slowing}} enemies inside briefly",
        "slow",
        [("Movement Speed Modifier", "{{ap|30}}%}}\n|cooldown     = 7\n|co")],
    )
    assert props.slow_pct == 30.0


def test_area_heuristic():
    multi = parse.extract_cc_properties(
        "{{tip|stun|stunning}} all enemies hit for 1 second", "stun"
    )
    mono = parse.extract_cc_properties(
        "{{tip|stun|stunning}} the first enemy hit for 1 second", "stun"
    )
    assert multi.area is True
    assert mono.area is False
