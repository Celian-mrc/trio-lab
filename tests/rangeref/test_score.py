"""Tests unitaires purs de rangeref/score.py — pas de réseau (JSON Data
Dragon simulé)."""

from __future__ import annotations

from trio_lab.rangeref import score


def _detail(
    attack_range: float,
    spell_ranges: list[list[float]],
    spell_ids: list[str] | None = None,
    spell_cooldowns: list[list[float]] | None = None,
    attack_speed: float = 1.0,
) -> dict:
    """JSON Data Dragon minimal (`champion/<Nom>.json`, clé `data.<Nom>`) —
    seuls les champs lus par `champion_range_score`. `spell_ids` par défaut à
    des identifiants factices distincts (Q0/W0/E0/R0) — les vrais ids
    (`XerathArcanopulseChargeUp`...) ne comptent que pour les tests qui
    exercent `RANGE_SPELL_OVERRIDES`. `spell_cooldowns` absent par défaut
    (comme un test qui ne fournit pas ce champ) : `_NO_COOLDOWN_THROTTLE`
    s'applique, la contribution reste une simple distance — c'est pour ça que
    tous les tests écrits avant l'intégration du cooldown restent valides
    sans modification. `attack_speed` par défaut à 1.0 : `attack_range *
    1.0 == attack_range`, donc tous les tests écrits avant l'intégration de
    l'autoattaque pondérée restent valides sans modification eux aussi."""
    default_ids = [f"{slot}0" for slot in ("Q", "W", "E", "R")][: len(spell_ranges)]
    ids = spell_ids or []
    ids = list(ids) + default_ids[len(ids) :]
    cooldowns = spell_cooldowns or [None] * len(spell_ranges)
    spells = []
    for sid, ranges, cds in zip(ids, spell_ranges, cooldowns, strict=True):
        spell = {"id": sid, "range": ranges}
        if cds is not None:
            spell["cooldown"] = cds
        spells.append(spell)
    return {
        "stats": {"attackrange": attack_range, "attackspeed": attack_speed},
        "spells": spells,
    }


def test_sums_attack_range_and_the_rate_of_every_eligible_spell():
    # Retour utilisateur 2026-07-28 (cas Samira) : "il faut utiliser le kit
    # en entier" — chaque sort éligible contribue à la somme, pas seulement
    # le meilleur. attack_range ajouté une seule fois (une distance, pas un
    # taux) ; sans cooldown fourni, chaque sort compte pour sa portée brute
    # (division par `_NO_COOLDOWN_THROTTLE` = 1).
    detail = _detail(525, [[750], [1000], [1050], [5000]])
    assert score.champion_range_score(detail, "SomeChamp") == 525 + 750 + 1000 + 1050 + 5000


def test_ultimate_contributes_additively_alongside_other_spells():
    # Un ultime n'est pas exclu par principe (retour utilisateur 2026-07-28,
    # 2e relecture) : il s'ajoute à la somme comme n'importe quel autre sort
    # éligible.
    detail = _detail(500, [[400], [400], [400], [6000]])
    assert score.champion_range_score(detail, "SomeChamp") == 500 + 400 + 400 + 400 + 6000


def test_takes_the_max_rank_of_a_spell_with_a_scaling_range():
    detail = _detail(0, [[600, 650, 700, 750, 800], [0], [0], [0]])
    assert score.champion_range_score(detail, "SomeChamp") == 800


def test_no_override_for_spells_not_in_range_spell_overrides():
    detail = _detail(525, [[750], [0], [0], [0]])
    assert score.champion_range_score(detail, "NotXerath") == 525 + 750


def test_xerath_override_replaces_the_understated_ddragon_value():
    # Data Dragon ne rapporte que la portée de base du Q à charge (750) —
    # la vraie portée max chargée (1450, cf. wiki "capped at 1450 range")
    # remplace cette valeur plutôt que de s'y ajouter.
    detail = _detail(525, [[750], [0], [0], [0]], spell_ids=["XerathArcanopulseChargeUp"])
    assert score.champion_range_score(detail, "Xerath") == 525 + 1450


def test_override_only_matches_the_exact_champion_and_spell_id():
    # Même id de sort mais sur un autre champion : pas d'override, valeur
    # Data Dragon brute conservée (les overrides sont scopés par paire
    # (champion, spell_id), jamais par id de sort seul).
    detail = _detail(525, [[750], [0], [0], [0]], spell_ids=["XerathArcanopulseChargeUp"])
    assert score.champion_range_score(detail, "NotXerath") == 525 + 750


def test_none_override_excludes_the_spell_entirely():
    # Ashe E (Hawkshot) mappé sur None : pas une distance de poke (reveal de
    # vision "anywhere on the map") — sa contribution ne compte pas dans la
    # somme, contrairement au Q (W à 0 pour isoler ce comportement précis).
    detail = _detail(550, [[900], [0], [1234]], spell_ids=["AsheQ", "AsheW", "AsheSpiritOfTheHawk"])
    assert score.champion_range_score(detail, "Ashe") == 550 + 900


def test_none_override_can_exclude_every_provided_spell(monkeypatch):
    # Si tous les sorts fournis sont exclus, le score retombe sur l'autoattack
    # seul (somme vide = 0.0).
    monkeypatch.setattr(
        score,
        "RANGE_SPELL_OVERRIDES",
        {
            ("Aatrox", "AatroxQ"): None,
            ("Aatrox", "AatroxW"): None,
            ("Aatrox", "AatroxE"): None,
        },
    )
    detail = _detail(525, [[1], [1], [1]], spell_ids=["AatroxQ", "AatroxW", "AatroxE"])
    assert score.champion_range_score(detail, "Aatrox") == 525 + 0.0


def test_none_override_excludes_a_dash_ultimate_despite_a_huge_ddragon_value():
    # Hecarim R (Onslaught of Shadows) : Data Dragon rapporte 50000 (une
    # sentinelle) pour ce qui est en réalité un dash d'engage à 300-1000 de
    # portée — pas un outil de poke, exclu comme n'importe quel dash. Q/W/E à
    # 0 pour isoler ce comportement précis (sinon leurs contributions
    # s'ajouteraient aussi, cf. somme du kit entier).
    detail = _detail(450, [[0], [0], [0], [50000]], spell_ids=["Q0", "W0", "E0", "HecarimUlt"])
    assert score.champion_range_score(detail, "Hecarim") == 450 + 0.0


def test_global_range_override_represents_a_map_wide_damage_ultimate():
    # Karthus R (Requiem) : dégâts à toute la carte, sans ciblage — remplacé
    # par la constante GLOBAL_RANGE plutôt que la valeur Data Dragon brute
    # (Karthus n'a même pas de champ de portée officiel, l'ultimate n'a
    # littéralement aucune limite de distance). Q/W/E à 0 pour isoler.
    detail = _detail(450, [[0], [0], [0], [1]], spell_ids=["Q0", "W0", "E0", "KarthusFallenOne"])
    assert score.champion_range_score(detail, "Karthus") == 450 + score.GLOBAL_RANGE


def test_raw_value_beyond_range_cap_is_clamped(monkeypatch):
    # Filet de sécurité (retour utilisateur 2026-07-28 : "il faut instaurer
    # un plafond") pour un sort à portée aberrante pas encore examiné à la
    # main — même mécanisme que le bug Janna W déjà corrigé (Data Dragon
    # rapportait 4294967295), mais générique plutôt que traité au cas par
    # cas dans RANGE_SPELL_OVERRIDES. Q/W/E à 0 pour isoler.
    monkeypatch.setattr(score, "RANGE_CAP", 2000.0)
    detail = _detail(500, [[0], [0], [0], [4294967295]])
    assert score.champion_range_score(detail, "SomeChamp") == 500 + 2000.0


def test_override_beyond_range_cap_is_also_clamped(monkeypatch):
    # Le plafond s'applique aussi à une valeur déjà overridée, pas seulement
    # à la valeur brute Data Dragon. Q/W/E à 0 pour isoler.
    monkeypatch.setattr(score, "RANGE_CAP", 2000.0)
    detail = _detail(500, [[0], [0], [0], [1]], spell_ids=["Q0", "W0", "E0", "HugeR"])
    monkeypatch.setattr(score, "RANGE_SPELL_OVERRIDES", {("SomeChamp", "HugeR"): 999999.0})
    assert score.champion_range_score(detail, "SomeChamp") == 500 + 2000.0


def test_divides_by_the_fastest_rank_cooldown():
    # Retour utilisateur 2026-07-28 : "la capacité à infliger des dégâts de
    # loin ET régulièrement" — un sort à portée 1000, cooldown 8/7/6/5/4,
    # compte pour 1000/4 = 250/s (attack_range ajouté à part, pas divisé).
    detail = _detail(500, [[1000]], spell_cooldowns=[[8, 7, 6, 5, 4]])
    assert score.champion_range_score(detail, "SomeChamp") == 500 + 1000 / 4


def test_a_fast_short_range_spell_contributes_more_per_second_than_a_slow_long_range_one():
    # Q : portée modeste mais cooldown court -> fort taux de poke. R : portée
    # énorme mais cooldown très long (typique d'un ultimate) -> faible taux
    # malgré la plus grande distance. Les deux s'ADDITIONNENT (kit entier),
    # mais le Q pèse plus lourd dans la somme malgré sa portée inférieure —
    # c'est ce mécanisme qui déclasse Renata Glasc sans règle spéciale dédiée.
    detail = _detail(
        500,
        [[800], [8000]],
        spell_ids=["FastQ", "SlowR"],
        spell_cooldowns=[[4], [110]],
    )
    fast_rate = 800 / 4
    slow_rate = 8000 / 110
    assert fast_rate > slow_rate
    assert score.champion_range_score(detail, "SomeChamp") == 500 + fast_rate + slow_rate


def test_all_zero_real_cooldown_array_is_floored_not_treated_as_no_throttle():
    # Bug trouvé en vérifiant les vraies données : Data Dragon rapporte un
    # cooldown ENTIÈREMENT à 0 pour certains sorts réels (Aphelios E,
    # Veigar W, Vi W, Talon E — un flou de données, pas un vrai "sans
    # cooldown"). Un champ PRÉSENT mais tout à 0 doit retomber sur
    # MIN_COOLDOWN, pas sur `_NO_COOLDOWN_THROTTLE` (qui ne doit s'appliquer
    # qu'à un champ absent, cf. test suivant) — sinon ces sorts explosent le
    # classement en étant traités comme littéralement gratuits à relancer.
    detail = _detail(500, [[1000]], spell_cooldowns=[[0, 0, 0, 0, 0]])
    assert score.champion_range_score(detail, "SomeChamp") == 500 + 1000 / score.MIN_COOLDOWN


def test_missing_cooldown_field_falls_back_to_no_throttle():
    # Champ absent du dict (jamais le cas en vrai — tout sort Data Dragon a
    # un champ `cooldown`, même bugué à 0 comme ci-dessus) : uniquement pour
    # la commodité des tests écrits avant l'intégration du cooldown, qui ne
    # fournissent pas ce champ et doivent garder leur comportement d'origine.
    detail = _detail(500, [[1000]])
    assert score.champion_range_score(detail, "SomeChamp") == 500 + 1000


def test_near_zero_cooldown_is_floored_to_min_cooldown():
    # Retour utilisateur 2026-07-28 : Data Dragon rapporte des cooldowns
    # quasi nuls pour des sorts qui ne sont pas vraiment "lancés à volonté"
    # (Yasuo E: 0.1s, Rengar Q/W/E: 0.25s, Teemo E: 0s) — sans plancher, ces
    # sorts explosent le classement malgré une portée modeste. 0.1s réel
    # remonté au plancher MIN_COOLDOWN (4.0 par défaut) avant division.
    detail = _detail(500, [[1000]], spell_cooldowns=[[0.1, 0.1, 0.1, 0.1, 0.1]])
    assert score.champion_range_score(detail, "SomeChamp") == 500 + 1000 / score.MIN_COOLDOWN


def test_a_full_poke_kit_outscores_a_single_isolated_poke_tool():
    # Cas Samira (retour utilisateur 2026-07-28) : un champion avec UN seul
    # sort de poke (portée correcte, cooldown rapide) doit scorer moins
    # qu'un champion au kit ENTIER tourné vers le poke (plusieurs sorts
    # éligibles, même si chacun est individuellement plus modeste), puisque
    # les sorts s'additionnent désormais au lieu de ne garder que le meilleur.
    single_tool_kit = _detail(500, [[950]], spell_cooldowns=[[6, 5, 4, 3, 2]])
    full_poke_kit = _detail(
        500,
        [[900], [1000], [800]],
        spell_cooldowns=[[5, 5, 5, 5, 5], [10, 10, 10, 10, 10], [15, 15, 15, 15, 15]],
    )
    single_tool_score = score.champion_range_score(single_tool_kit, "SingleTool")
    full_kit_score = score.champion_range_score(full_poke_kit, "FullKit")
    assert full_kit_score > single_tool_score


def test_autoattack_range_is_weighted_by_attack_speed():
    # Retour utilisateur 2026-07-28 : "quel est le poids de la portée
    # d'auto attaque" — attack_range n'est plus une distance brute ajoutée
    # telle quelle, mais un taux (portée × vitesse d'attaque), traitée
    # comme n'importe quel autre sort (portée / "cooldown").
    detail = _detail(600, [], attack_speed=0.7)
    assert score.champion_range_score(detail, "SomeChamp") == 600 * 0.7


def test_a_slower_attacker_gets_less_credit_for_the_same_attack_range():
    # Annie (attackspeed 0.61) vs Caitlyn (attackspeed 0.681) : même portée
    # d'attaque, mais l'autoattaque plus lente pèse moins dans le score —
    # avant ce correctif, les deux auraient compté pour exactement la même
    # valeur brute malgré la différence de cadence réelle.
    slow_attacker = _detail(625, [], attack_speed=0.61)
    fast_attacker = _detail(625, [], attack_speed=0.681)
    assert score.champion_range_score(fast_attacker, "Fast") > score.champion_range_score(
        slow_attacker, "Slow"
    )
