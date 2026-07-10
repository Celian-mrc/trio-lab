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
