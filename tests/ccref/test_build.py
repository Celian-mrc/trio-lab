"""Tests des politiques pures de `build` (défauts de durée) — aucun réseau."""

from __future__ import annotations

from trio_lab.ccref import build


def test_default_durations_by_subtype():
    # Murs/piliers (Anivia W, Trundle E, Ornn Q) : hop très court.
    assert build.default_duration("airborne", displaces=False, wall=True) == (
        0.25,
        "knock-up très court (mur/pilier)",
    )
    # Déplacements (knockback/pull, repositionnement=1).
    assert build.default_duration("airborne", displaces=True, wall=False) == (
        0.5,
        "knockback/pull",
    )
    # Knock-up standard.
    assert build.default_duration("airborne", displaces=False, wall=False) == (
        0.75,
        "knock-up",
    )
    # Suppression liée à un déplacement (R de Sett).
    assert build.default_duration("suppression", displaces=False, wall=False)[0] == 1.25


def test_no_default_for_other_cc_types():
    """Stun/fear/root sans durée : trop variés pour un défaut — restent à relire."""
    for cc_type in ("stun", "fear", "root", "charm", "slow"):
        assert build.default_duration(cc_type, displaces=False, wall=False) is None


def _row(champion, sort, type_cc, **extra):
    base = {
        "champion": champion,
        "sort": sort,
        "type_cc": type_cc,
        "duree_s": "",
        "pct_slow": "",
        "conditionnel": 0,
        "repositionnement": 0,
        "note_relecture": "durée introuvable",
    }
    base.update(extra)
    return base


def test_apply_overrides_set_exclude_and_duplicates():
    rows = [
        _row("Viego", "W", "slow", pct_slow=10.0),
        _row("Urgot", "R", "slow"),  # doublon 1 : effet à l'impact
        _row("Urgot", "R", "slow"),  # doublon 2 : effet à l'exécution
        _row("Sett", "E", "stun", duree_s=1.0),
    ]
    overrides = [
        {
            "champion": "Viego",
            "sort": "W",
            "type_cc": "slow",
            "action": "exclude",
            "duree_s": "",
            "pct_slow": "",
            "conditionnel": "",
            "note": "self",
        },
        {
            "champion": "Urgot",
            "sort": "R",
            "type_cc": "slow",
            "action": "set",
            "duree_s": "4.0",
            "pct_slow": "75.0",
            "conditionnel": "",
            "note": "impact",
        },
        {
            "champion": "Urgot",
            "sort": "R",
            "type_cc": "slow",
            "action": "exclude",
            "duree_s": "",
            "pct_slow": "",
            "conditionnel": "",
            "note": "doublon",
        },
        {
            "champion": "Sett",
            "sort": "E",
            "type_cc": "stun",
            "action": "set",
            "duree_s": "",
            "pct_slow": "",
            "conditionnel": "1",
            "note": "conditionnel",
        },
        {
            "champion": "Fantome",
            "sort": "Q",
            "type_cc": "stun",
            "action": "set",
            "duree_s": "9",
            "pct_slow": "",
            "conditionnel": "",
            "note": "sans correspondance",
        },
    ]
    kept = build.apply_overrides(rows, overrides)

    assert len(kept) == 2  # Viego W et le doublon Urgot exclus
    urgot = next(r for r in kept if r["champion"] == "Urgot")
    assert (urgot["duree_s"], urgot["pct_slow"]) == (4.0, 75.0)
    assert urgot["note_relecture"].startswith("relecture :")
    sett = next(r for r in kept if r["champion"] == "Sett")
    assert sett["conditionnel"] == 1
    assert sett["duree_s"] == 1.0  # champ vide dans l'override : non touché


def test_apply_overrides_add_creates_missing_row():
    """Ligne absente du wiki (W d'Ashe qui applique le passif) : action add."""
    overrides = [
        {
            "champion": "Ashe",
            "sort": "W",
            "type_cc": "slow",
            "action": "add",
            "duree_s": "2",
            "pct_slow": "25",
            "conditionnel": "0",
            "zone": "multi",
            "fiabilite": "skillshot",
            "note": "applique le passif",
        },
    ]
    kept = build.apply_overrides([], overrides)
    assert len(kept) == 1
    row = kept[0]
    assert (row["champion"], row["sort"], row["type_cc"]) == ("Ashe", "W", "slow")
    assert (row["duree_s"], row["pct_slow"]) == (2.0, 25.0)
    assert row["disponibilite"] == "base"  # dérivé du slot (R → ultimate)
    assert row["note_relecture"].startswith("relecture (ajout)")


def test_apply_defaults_annotates_and_fills():
    rows = [
        {
            "type_cc": "airborne",
            "repositionnement": 1,
            "duree_s": "",
            "note_relecture": "durée introuvable",
        },
        {
            "type_cc": "airborne",
            "repositionnement": 0,
            "duree_s": 1.0,  # déjà chiffrée : intouchée
            "note_relecture": "",
        },
        {
            "type_cc": "stun",
            "repositionnement": 0,
            "duree_s": "",
            "note_relecture": "durée introuvable",
        },
    ]
    applied = build._apply_defaults(rows, walls={0: False})
    assert applied == 1
    assert rows[0]["duree_s"] == 0.5
    assert "défaut appliqué" in rows[0]["note_relecture"]
    assert "durée introuvable" not in rows[0]["note_relecture"]
    assert rows[1]["duree_s"] == 1.0
    assert rows[2]["duree_s"] == ""  # pas de défaut pour un stun
