"""Score de portée théorique par champion (Phase 8+, retour utilisateur
2026-07-28 : "compos poke avec de la range" — quel archétype favorise les
champions qui harcèlent à distance).

Contrairement au CC (`ccref/`), aucun scraping du wiki n'est nécessaire :
Data Dragon (CDN statique Riot, déjà utilisé par `web/champions.py` pour
noms/icônes) expose directement `stats.attackrange` et `spells[].range`
par champion, en JSON structuré — pas de prose à parser, pas de résolution
nom-wiki → championId (le champ `key` du résumé DDragon EST le championId).

Formule : score_brut = somme, sur l'autoattaque ET chaque sort Q/W/E/R
éligible, de sa portée divisée par son cooldown (rang max pour un sort, le
temps entre deux attaques — `1 / attackspeed` — pour l'autoattaque) — un
taux par source, additionné plutôt que de ne garder que le meilleur.

Autoattaque pondérée, pas une distance brute (retour utilisateur 2026-07-28,
6e relecture : "quel est le poids de la portée d'auto attaque... annie a une
bonne portée d'attaque mais ses auto attaques font 50 de dégâts"). Avant ce
correctif, `attack_range` était ajoutée telle quelle (une distance de
500-650, jamais divisée) : elle pesait 40 à 65% du score total de N'IMPORTE
QUEL champion (mage ou tireur), sans lien avec la fréquence ou l'intérêt
réel de l'autoattaque pour ce champion. `attackdamage × attackspeed` (une
vraie mesure de DPS d'autoattaque) a été envisagé mais rejeté : aux stats de
base (niveau 1, sans objets), Annie et Caitlyn ne se distinguent presque pas
(30.5 vs 42.2) — l'écart réel entre un mage et un tireur vient de l'objet,
hors du périmètre d'un score de champion. Traiter l'autoattaque exactement
comme un sort (portée / "cooldown", ici `1 / attackspeed`) reste cohérent
avec le reste de la formule sans introduire un chantier de données
supplémentaire, même si ça ne corrige pas la question des dégâts (même
limite assumée que pour les sorts, cf. section "Portée VS dégâts").
Pas de plancher `MIN_COOLDOWN` appliqué ici : `attackspeed` est un champ
Data Dragon fiable (contrairement aux cooldowns quasi nuls de sorts
mal modélisés, cf. section "Régularité") — une cadence d'attaque de
1 à 2 par seconde est réelle, pas un artefact.

Kit entier plutôt que meilleur sort seul (retour utilisateur 2026-07-28, 5e
relecture, cas Samira) : Samira Q (Flair, 950 de portée, cooldown jusqu'à 2s)
donnait à Samira un score comparable à Vel'Koz/Ziggs/Zoe sous l'ancienne
formule (`max` sur les sorts), alors que Samira n'a QU'UN outil de poke réel
dans son kit — le reste (W bouclier de mêlée auto-centré, E dash d'engage,
R zone d'exécution auto-centrée, tous les trois exclus dans
RANGE_SPELL_OVERRIDES) est un kit d'all-in au contact, pas de harcèlement.
"Vel'koz ziggs zoe sont d'excellents pokeurs grâce à LEUR KIT" (plusieurs
sorts de poke qui s'additionnent), pas un seul sort isolé — d'où la somme.
Un vrai kit de poke (3-4 sorts éligibles) score maintenant nettement plus
haut qu'un kit avec un seul outil de poke isolé, même si ce dernier sort a
individuellement une bonne portée et un bon cooldown.

Régularité (retour utilisateur 2026-07-28, 4e relecture) : "le but de ce
score est... la capacité à infliger des dégâts de loin ET régulièrement" —
une grande portée sur un sort qu'on relance une fois toutes les deux minutes
n'est pas du poke. `cooldown` est un champ Data Dragon propre et sans
ambiguïté (contrairement aux dégâts, cf. plus bas) : pas de collecte
supplémentaire nécessaire. Ce choix résout structurellement le cas Renata
Glasc soulevé par l'utilisateur (son ultimate, portée capée mais cooldown
~100-120s, retombe loin derrière un sort de mage à 5-9s de cooldown) sans
règle spéciale dédiée à ce champion — et déclasse mécaniquement la plupart
des ultimates "Global" de la première place, leur cooldown de 60-130s les
pénalisant fortement face aux sorts non-ultimates à rotation rapide.

Portée VS dégâts (retour utilisateur 2026-07-28) : les vrais dégâts par sort
NE sont PAS intégrés — contrairement à `cooldown`, Data Dragon ne les expose
pas proprement (les tableaux `effect`/`vars` bruts n'ont pas de libellé
sémantique fiable indiquant quel indice est "dégâts" vs "rayon de zone" vs
"% de ralentissement" ; il faudrait retourner au wiki sort par sort, un
chantier de l'ampleur de la table de référence CC). Portée décision
utilisateur explicite : cooldown seul pour l'instant, dégâts (et la
dimension zone/AOE, ex. Jayce Shock Blast à travers Acceleration Gate)
laissés pour un chantier ultérieur si besoin.

Les ultimates COMPTENT (retour utilisateur 2026-07-28, deuxième relecture du
top 25 : "il ne faut pas exclure les ultis, ça compte comme de la portée et
du poke"). Choix initial inverse (tout exclure, cf. git history) — corrigé :
un ultime qui inflige des dégâts/CC à longue portée EST un outil de poke,
souvent le plus extrême du kit (Karthus, Xerath R, Ziggs R...). Le filtre
qui compte n'est pas "est-ce un ultimate ?" mais le même critère déjà
appliqué aux sorts non-ultimates : est-ce que l'EFFET (dégâts/CC) se
déclenche à la portée annoncée (poke) ou seulement après un dash/mêlée/sans
rapport avec les dégâts (Hecarim R, Rammus R, Kled R : dash d'engage ;
Aatrox R, Ryze R : self-buff/teleport ; Kai'Sa R, Yasuo R, Rek'Sai R :
dash-execute vers une cible déjà marquée, le dégât part au contact pas à
distance ; Nocturne R : aveugle toute l'équipe adverse à distance illimitée
mais sans dégât — le vrai dégât vient du recast en dash, même souci que
Kai'Sa/Yasuo ; Rengar R : buff de vitesse + camouflage + détection, la
portée Data Dragon reflète un rayon de détection, aucun dégât) ?
Vérifié sort par sort sur le wiki pour chaque ultime dont la portée brute
dépassait 1250 (~50 sorts) — sous ce seuil, aucune valeur suspecte trouvée
lors du sondage du reste du roster, mêmes conventions que le sondage
Q/W/E d'origine.

AUCUN mélange empirique (contrairement au CC, `ccref.score.blended_pct`) :
aucune stat Riot ne mesure la distance de poke réelle en jeu (vérifié —
la Timeline API n'expose ni position par sort lancé, ni distance de
toucher ; seules des frames de position toutes les 60s et la position d'un
`CHAMPION_KILL`, bien trop grossier). Ce score reste 100% théorique,
jamais recalé par le comportement réel des joueurs — `synergy/compute.py`
réutilise directement `ccref.score.theoretical_pct` pour la normalisation
0-100 (même formule que le CC théorique, générique, pas de duplication).

RANGE_SPELL_OVERRIDES : certains sorts Data Dragon rapportent une valeur
"range" qui n'est PAS une distance de poke réelle — soit une sentinelle
"illimité/self" (25000, 10000, 4294967295 = 2**32-1 vu sur Janna W) posée
sur un sort self-cast/vision/mobilité, soit (comme Xerath Q) seulement le
minimum d'un sort à charge. Recensé le 2026-07-28 en scannant tous les sorts
Q/W/E des 173 champions dont la portée brute dépassait 2000 (~30 sorts) puis
en lisant la prose de chaque page wiki (`Template:Data <Champion>/<Sort>`,
champ `|range=` ou `|target range=`) pour trancher au cas par cas :

- `None` = sort exclu du calcul (pas une distance de poke — self-buff,
  reveal de vision, dash/mobilité, effet on-hit) : le score du champion
  retombe sur son autre sort ou l'autoattack seul. Ex. Ashe E (Hawkshot,
  vision "anywhere on the map"), Zeri E (dash sur soi), Warwick W (rayon de
  détection passif).
- Valeur numérique = portée réelle du wiki qui REMPLACE la valeur Data
  Dragon (sort à charge où Data Dragon ne rapporte que la borne basse,
  jamais mesurée en jeu autrement). Ex. Xerath Q capé à 1450 (Data Dragon :
  750), Sion Q capé à 850 (Data Dragon : 10000, une sentinelle et non une
  vraie distance), Jhin W ~2520 (portée jamais publiée officiellement,
  estimation communautaire du wiki, cf. `{{tt|2520|Estimated}}`).

Certains sorts à portée surprenante mais dont la valeur Data Dragon widget
s'est révélée CORRECTE après vérification wiki n'apparaissent PAS dans cette
table (comportement par défaut conservé) : Kai'Sa W (Void Seeker, 3000 —
outil de longue portée officiel) et Swain W (Vision of Empire, 5500 à 7500
selon le rang — conçu comme un outil de contrôle de zone à très longue
portée, confirmé par le wiki `{{ap|5500 to 7500}}`).

Deuxième passe (retour utilisateur 2026-07-28, top 25 relu) : une grande
valeur de portée VALIDE par le wiki n'est pas forcément un outil de poke —
il faut aussi que l'effet (dégâts/CC) se déclenche À la portée annoncée, pas
seulement le ciblage initial. Exclus sur ce critère : Nunu W (le boulet de
neige grossit en ROULANT — Willump avance avec lui pendant le channel, ce
n'est pas un cast statique comme Xerath Q), Skarner E (le wiki ne définit
même pas de `target range` — c'est une charge/dash qui agrippe au contact,
pas un sort visé à distance), Ekko W et Evelynn W (les deux ne font que
MARQUER une position/cible à portée ; les dégâts et le CC ne se déclenchent
que si Ekko entre dans la sphère ou qu'Evelynn touche la cible avec un
sort/attaque suivant — donc au contact, pas à distance), Rek'Sai W (Burrow,
un toggle de forme self-buff — la valeur Data Dragon reflète Tremor Sense/le
tunnel, pas une portée de sort visé).

Jayce est un cas à part : Data Dragon ne modélise qu'UN seul jeu de sorts
par champion, celui de sa forme par défaut (Mercury Hammer, mêlée) — son
vrai outil de poke, Shock Blast (forme Mercury Cannon, sort Q), n'existe
nulle part dans `champion/Jayce.json`. Portée réelle confirmée par le wiki
(`Template:Data Jayce/Shock Blast`) : 1050 de base, jusqu'à 1600 à travers
Acceleration Gate (W) — une combo courante dans son kit de poke, pas un cas
limite. Seul champion du roster actuel avec ce problème de changement de
forme qui affecte vraiment son profil de poke (Nidalee capture déjà sa vraie
portée de poke via son Q forme humaine, sa forme Cougar est un kit de
mêlée ; Gnar Mini est déjà la forme par défaut exposée par Data Dragon).

Ziggs Q (Bouncing Bomb) rebondit deux fois : le wiki distingue explicitement
`target range = 850 (Cast range)` de `range = 1400 (Maximum range with
bounces)` — Data Dragon ne rapporte que la première. Override à 1400, la
vraie portée effective du sort. Avec son ultimate maintenant compté (R,
5000, confirmé réel par le wiki, pas une sentinelle), Ziggs devient l'un des
scores les plus hauts du roster — cohérent avec sa réputation de siège
extrême, portée par l'ulti bien plus que par son Q.

GLOBAL_RANGE : certains ultimates ont une portée officiellement "Global" au
wiki (`|target range = Global` ou, pour Karthus, aucune limite de portée
décrite du tout — "deals damage to all targetable enemy champions") ET
infligent de vrais dégâts/CC à leur cible, contrairement aux sentinelles
Data Dragon (25000 etc., de fausses valeurs sur des sorts self/vision sans
rapport avec une distance). Une valeur numérique fixe est nécessaire pour le
calcul. `GLOBAL_RANGE = 8000.0` (retour utilisateur 2026-07-28, 4e relecture
: "il faut mettre un cap beaucoup plus bas pour éviter les écarts énormes" —
la valeur initiale de 15000, un ordre de grandeur du plus grand axe de la
Faille de l'Invocateur, créait un fossé disproportionné avec le reste du
roster). Choisie juste au-dessus de Swain W (5500 à 7500 selon le rang, la
plus grande portée BORNÉE confirmée du roster) pour ne pas l'écraser, tout
en gardant les ultimates "Global" nettement devant sans être absurdement
loin devant. Champions concernés : Gangplank R, Ashe R, Ezreal R, Jinx R,
Mel R, Draven R, Senna R, Karthus R (portée totale, sans ciblage), Lillia R
(touche tous les ennemis déjà marqués par ses stacks, où qu'ils soient).

Portées "Global" mais SANS dégât (vision/soin/téléport allié) restent
exclues comme n'importe quel effet non-poke : Shen R (bouclier allié),
Twisted Fate R / Destiny (reveal pur), Soraka R (soin "regardless of
distance"), Ivern W / Triggerseed (bouclier allié — trouvé en vérifiant
pourquoi Ivern apparaissait haut dans le classement une fois le cooldown
intégré : son W a un cooldown Data Dragon de 0.5s manifestement erroné).

Zoe (retour utilisateur 2026-07-28, 3e relecture) : ni le recast "Global" du
Q ni la tech de combo R+E de l'E n'ont de chiffre officiel fixe (cf. wiki,
détaillé plus haut dans l'historique de ce module) — plutôt que de laisser
ces deux sorts sous-évalués sur la valeur de base Data Dragon (800 chacun),
consigne explicite : doubler. `ZoeQ` et `ZoeE` à 1600.0 chacun — pas une
mesure, un proxy pragmatique assumé pour refléter la réputation de snipe
longue distance sans se fier à un "Global" non quantifiable.

RANGE_CAP : plafond dur appliqué à CHAQUE contribution (brute ou
`override`) dans `champion_range_score`, pas seulement à celles listées dans
`RANGE_SPELL_OVERRIDES` (retour utilisateur 2026-07-28 : "il y a des
champions avec des sorts à portée infinie, il faut instaurer un plafond").
Sert de filet de sécurité pour tout sort à portée aberrante qu'on n'a pas
encore examiné à la main (dans l'esprit du bug Janna W déjà corrigé, où
Data Dragon rapportait 4294967295) — sans plafond, un futur cas similaire
non détecté fausserait le score sans qu'on s'en rende compte. Fixé à la même
valeur que `GLOBAL_RANGE` (8000.0) : les ultimates "Global" légitimes
restent donc au plafond plutôt que d'être artificiellement réduits par lui.

Ultimates à grande portée mais PAS conçus pour le poke (retour utilisateur
2026-07-28) : une portée valide et un effet dégâts/CC à distance ne
suffisent pas — encore faut-il que le sort serve à harceler depuis une
position sûre, pas à ENGAGE un combat de loin (le lanceur se téléporte dans
la mêlée, ou verrouille l'ennemi pour qu'un allié vienne se battre).
Exclus sur ce critère : Galio R et Pantheon R (téléportent le champion
LUI-MÊME au corps-à-corps de la cible — l'inverse de rester à distance),
Bard R (stase pure, ZÉRO dégât par elle-même, sert uniquement à figer
l'ennemi pour qu'une équipe s'engage), Taliyah R (outil de contrôle de zone
et split-push conçu pour repousser/séparer, pas pour harceler). Briar R
également exclu en repassant sa description : "marks the first enemy
champion hit and reveals them" — aucun dégât mentionné, un outil de
marquage/reveal comme Ashe E ou Twisted Fate R, pas du poke malgré sa
portée réelle confirmée de 12000.

À revérifier à chaque gros rework de patch touchant un des sorts listés ici.
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPIONS_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
CHAMPION_DETAIL_URL = (
    "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion/{name}.json"
)
USER_AGENT = "trio-lab/0.1 (projet perso; score de portee theorique par champion)"

# Cf. docstring du module — ordre de grandeur du plus grand axe de la Faille
# de l'Invocateur, valeur représentative pour les ultimates à portée
# officiellement "Global" (pas une mesure officielle Riot).
GLOBAL_RANGE = 8000.0

# Plafond dur appliqué à toute contribution dans champion_range_score, brute
# ou déjà overridée — filet de sécurité pour un sort à portée aberrante non
# encore examiné à la main (cf. docstring, section RANGE_CAP). Même valeur
# que GLOBAL_RANGE : les ultimates "Global" légitimes restent au plafond.
RANGE_CAP = GLOBAL_RANGE

RANGE_SPELL_OVERRIDES: dict[tuple[str, str], float | None] = {
    ("Aatrox", "AatroxQ"): None,
    ("Aatrox", "AatroxE"): None,
    ("Aatrox", "AatroxR"): None,
    ("Akshan", "AkshanW"): None,
    ("Ashe", "AsheSpiritOfTheHawk"): None,
    ("Ashe", "EnchantedCrystalArrow"): GLOBAL_RANGE,
    ("Bard", "BardQ"): 850.0,
    ("Bard", "BardR"): None,
    ("Braum", "BraumE"): None,
    ("Briar", "BriarR"): None,
    ("Draven", "DravenRCast"): GLOBAL_RANGE,
    ("Ekko", "EkkoW"): None,
    ("Evelynn", "EvelynnW"): None,
    ("Evelynn", "EvelynnR"): None,
    ("Ezreal", "EzrealR"): GLOBAL_RANGE,
    ("Galio", "GalioR"): None,
    ("Gangplank", "GangplankR"): GLOBAL_RANGE,
    ("Hecarim", "HecarimUlt"): None,
    ("Ivern", "IvernW"): None,
    ("Janna", "SowTheWind"): None,
    ("Jayce", "JayceToTheSkies"): 1600.0,
    ("Jhin", "JhinW"): 2520.0,
    ("Jhin", "JhinR"): 3500.0,
    ("Jinx", "JinxR"): GLOBAL_RANGE,
    ("Kaisa", "KaisaR"): None,
    ("Kalista", "KalistaW"): None,
    ("Karthus", "KarthusFallenOne"): GLOBAL_RANGE,
    ("Katarina", "KatarinaW"): None,
    ("Khazix", "KhazixR"): None,
    ("Kled", "KledR"): None,
    ("Leblanc", "LeblancR"): None,
    ("Lillia", "LilliaR"): GLOBAL_RANGE,
    ("Mel", "MelR"): GLOBAL_RANGE,
    ("MissFortune", "MissFortuneBulletTime"): 1450.0,
    ("Mordekaiser", "MordekaiserW"): None,
    ("Nocturne", "NocturneParanoia"): None,
    ("Nunu", "NunuW"): None,
    ("Ornn", "OrnnW"): None,
    ("Pantheon", "PantheonR"): None,
    ("Quinn", "QuinnW"): None,
    ("Rammus", "Tremors2"): None,
    ("RekSai", "RekSaiW"): None,
    ("RekSai", "RekSaiR"): None,
    ("Rengar", "RengarR"): None,
    ("Ryze", "RyzeR"): None,
    ("Samira", "SamiraW"): None,
    ("Samira", "SamiraE"): None,
    ("Samira", "SamiraR"): None,
    ("Senna", "SennaR"): GLOBAL_RANGE,
    ("Seraphine", "SeraphineR"): 1300.0,
    ("Sett", "SettW"): None,
    ("Shen", "ShenR"): None,
    ("Shyvana", "ShyvanaQ"): None,
    ("Sion", "SionQ"): 850.0,
    ("Sion", "SionR"): None,
    ("Skarner", "SkarnerE"): None,
    ("Soraka", "SorakaR"): None,
    ("TahmKench", "TahmKenchE"): None,
    ("TahmKench", "TahmKenchRWrapper"): None,
    ("Taliyah", "TaliyahR"): None,
    ("TwistedFate", "WildCards"): 1450.0,
    ("TwistedFate", "Destiny"): None,
    ("Warwick", "WarwickR"): None,
    ("Warwick", "WarwickW"): None,
    ("Xayah", "XayahE"): None,
    ("Xerath", "XerathArcanopulseChargeUp"): 1450.0,
    ("Yasuo", "YasuoR"): None,
    ("Yone", "YoneE"): None,
    ("Yuumi", "YuumiQ"): 850.0,
    ("Yuumi", "YuumiW"): None,
    ("Yuumi", "YuumiE"): None,
    ("Zaahen", "ZaahenQ"): None,
    ("Zaahen", "ZaahenE"): None,
    ("Zeri", "ZeriE"): None,
    ("Ziggs", "ZiggsQ"): 1400.0,
    ("Zoe", "ZoeQ"): 1600.0,
    ("Zoe", "ZoeE"): 1600.0,
    ("Zoe", "ZoeW"): None,
}

# Les sorts d'un champion sont listés [Q, W, E, R] dans Data Dragon — les 4
# comptent désormais pour le poke (retour utilisateur 2026-07-28, cf.
# docstring du module : un ultime n'est plus exclu par principe).
_ACTIVE_SPELL_SLOTS = 4

# Sentinelle distincte de `None` : `RANGE_SPELL_OVERRIDES.get(...)` doit
# distinguer "sort absent de la table" (comportement par défaut : valeur
# Data Dragon brute) de "sort présent, mappé explicitement sur None" (sort
# exclu du calcul).
_NOT_OVERRIDDEN = object()

# Diviseur par défaut quand un sort n'a pas de cooldown exploitable (absent
# — cas de test uniquement, tout sort réel Data Dragon en a un) : pas de
# ralentissement appliqué, la contribution du sort reste une simple distance
# plutôt qu'un taux, cohérent avec le comportement d'avant l'intégration du
# cooldown (cf. docstring, section "Régularité").
_NO_COOLDOWN_THROTTLE = 1.0

# Plancher appliqué au cooldown le plus rapide avant division (cf. docstring,
# section "Plancher de cooldown") : Data Dragon rapporte des cooldowns quasi
# nuls (souvent 0 à 0.25s) pour des sorts qui ne sont pas vraiment "lancés à
# volonté" — passifs modélisés comme un sort (Teemo E, Toxic Shot, on-hit),
# effets d'embuscade appliqués à la prochaine attaque (Rengar Q/W/E), dashs
# de repositionnement (Yasuo E) — sans lien avec une vraie fréquence de poke.
# 4.0s est un plancher générique plutôt qu'une vérification sort par sort
# (retour utilisateur 2026-07-28 : approximatif mais rapide à mettre en
# place), calé sur le cooldown réel le plus court observé parmi les sorts de
# poke déjà vérifiés à la main cette session (Xerath Q, 5s à 9-5s).
MIN_COOLDOWN = 4.0


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def latest_version() -> str:
    return _get_json(VERSIONS_URL)[0]


def champion_range_score(detail: dict, name: str) -> float:
    """Score brut d'UN champion à partir de son JSON détail Data Dragon déjà
    récupéré (`champion/<Nom>.json`, clé `data.<Nom>`) — extrait pour être
    testable sans réseau (`tests/rangeref/test_score.py`). Malgré le nom
    (hérité), ce n'est plus une pure distance depuis l'intégration du
    cooldown et de la somme du kit (cf. docstring du module, sections
    "Régularité", "Kit entier" et "Autoattaque") : somme, sur l'autoattaque
    et chaque sort éligible, de sa portée divisée par son "cooldown" (le
    temps entre deux attaques pour l'autoattaque) — un taux, pas une
    distance brute."""
    attack_range = detail["stats"]["attackrange"]
    attack_speed = detail["stats"]["attackspeed"]
    poke_rate_total = attack_range * attack_speed
    for sp in detail["spells"][:_ACTIVE_SPELL_SLOTS]:
        override = RANGE_SPELL_OVERRIDES.get((name, sp["id"]), _NOT_OVERRIDDEN)
        if override is _NOT_OVERRIDDEN:
            spell_range = min(max(sp["range"]), RANGE_CAP)
        elif override is None:
            continue
        else:
            spell_range = min(override, RANGE_CAP)
        raw_cooldowns = sp.get("cooldown")
        if raw_cooldowns is None:
            # Champ absent : cas de test uniquement (tout sort réel Data
            # Dragon en a un, cf. MIN_COOLDOWN) — pas de ralentissement.
            fastest_cooldown = _NO_COOLDOWN_THROTTLE
        else:
            positive_cooldowns = [c for c in raw_cooldowns if c > 0]
            fastest_cooldown = (
                max(min(positive_cooldowns), MIN_COOLDOWN) if positive_cooldowns else MIN_COOLDOWN
            )
        poke_rate_total += spell_range / fastest_cooldown
    return poke_rate_total


def fetch_champion_scores(version: str | None = None) -> dict[int, float]:
    """`{championId: score_brut}` — 1 requête Data Dragon par champion
    (~173, CDN statique Riot, pas de clé/rate-limit connu contrairement à
    l'API de match)."""
    version = version or latest_version()
    summary = _get_json(CHAMPIONS_URL.format(version=version))["data"]
    scores: dict[int, float] = {}
    for name, champ in summary.items():
        detail = _get_json(CHAMPION_DETAIL_URL.format(version=version, name=name))["data"][name]
        scores[int(champ["key"])] = champion_range_score(detail, name)
    logger.info("Data Dragon %s : %d champions scorés (portée)", version, len(scores))
    return scores
