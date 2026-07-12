# Trio Lab — Synergies Jungle / Mid / Support

## Vision

Les champions jungler, mid et support sont ceux qui impactent le plus la map dans
les 20-25 premières minutes et déterminent le dynamisme d'une partie. Les sites
existants (dpm.lol, etc.) proposent des tier lists de synergies pour des **duos**
de rôles ; aucun ne le fait pour le **trio jungle/mid/support**. Trio Lab comble
ce vide : une interface qui expose les meilleurs winrates par trio de champions
à ces trois rôles, avec un score de synergie et des statistiques détaillées.

## Score de synergie

Même définition que dpm.lol pour les duos, étendue au trio :

```
Synergy(trio) = WR(trio) - moyenne(WR individuels des 3 champions)
```

Positif = le trio performe mieux ensemble que ce que laissent attendre les
champions pris isolément.

### Le défi statistique central

~170 champions → ~5 millions de trios possibles. Même à ~1M de matchs par patch,
la majorité des trios auront trop peu de games pour un winrate fiable. Stratégie :

1. **Seuil de games minimum** + intervalle de confiance affiché (tiers de fiabilité).
2. **Lissage bayésien** : le score d'un trio peu joué est tiré vers la prédiction
   issue de ses trois synergies de duos (jgl+mid, jgl+supp, mid+supp), qui ont
   beaucoup plus de données. Le trio n'affiche un score « pur » que quand son
   volume le justifie. Bonus : les tier lists de duos sortent gratuitement du
   même pipeline.
3. **Counters** : trio vs trio ennemi est combinatoirement intraitable (10^13).
   On calcule les counters **par champion ennemi individuel** (ex. « ce trio
   souffre contre Nocturne jungle »). Même raisonnement côté allié : pas de
   combinaison 5v5, mais un **meilleur allié Top/ADC individuel** (ex. « ce
   trio est boosté par tel Top »).
4. **Fenêtre multi-patchs glissante.** Les patchs ne sont **jamais fusionnés au
   stockage** : chaque match garde sa colonne `patch`, la fenêtre (1 à 3 patchs)
   est un filtre appliqué à la lecture. Patchs récents pondérés plus fort
   (décroissance ex. 1.0 / 0.6 / 0.35), fenêtre coupée en cas de rework majeur
   d'un membre du trio, patchs contributeurs toujours affichés. Note : la
   synergie étant une *différence* (WR trio − WR individuels moyens calculés
   sur la même fenêtre), elle est naturellement robuste aux équilibrages entre
   patchs — un nerf déplace les deux termes.

## Statistiques par trio

Toutes extraites des endpoints match-v5 + timeline :

- Winrate, nombre de games, score de synergie, intervalle de confiance
- **Avantage gold** à 5/10/15/20/25/30/35 min
- **Score de vision** cumulé du trio
- **Objectifs** : grubs (nombre pris), héraut, drakes (1er/2e/3e/4e…),
  taux d'obtention de l'âme, winrate quand l'âme est perdue, Nashor (taux de
  premier Nashor, timing moyen) — Atakhan non suivi (absent cette saison)
- **Tours** : première tour, ordre et emplacements des tours détruites, gold de
  plaques avant 14 min
- **Combat** : dégâts (part du trio dans les dégâts de l'équipe), first blood,
  kill participation du trio avant 15 min, CC score (`timeCCingOthers`)
- **Profil de tempo** : durée moyenne des games gagnées vs perdues (trio
  early-game vs scaling)
- **Counters** : winrate du trio face à chaque champion ennemi (par rôle)
- **Meilleurs alliés** : winrate du trio accompagné de chaque champion
  Top/ADC individuel (jamais de combinaison 5v5, même raisonnement que les
  counters)

## Score CC théorique par champion

Complément du CC *empirique* (`timeCCingOthers`) : un score intrinsèque au kit,
qui profile le potentiel d'engage/pick d'un trio même sur peu de games.

```
score_sort     = poids(type_cc) × durée_base(s) × coef_zone × coef_fiabilité × coef_disponibilité
score_champion = Σ score_sort sur les sorts du kit
score_trio     = Σ score_champion des 3 membres
```

Poids par type de CC (paramètres de config, à recalibrer empiriquement en
vérifiant la corrélation avec `timeCCingOthers`) :

| Type | Poids |
|---|---|
| Airborne (knock-up, knock-back, pull — non réduit par tenacity, **non cleansable**), suppression | 1.0 |
| Stun, charm, fear, taunt, sleep | 0.9 |
| Root | 0.7 |
| Silence, ground, blind, polymorph | 0.5 |
| Slow | 0.3 × (%slow/100) |

Les déplacements forcés (kick de Lee Sin, pull de Blitz…) sont des airborne :
même statut que les knock-ups, ni tenacity ni Cleanse/QSS ne les retirent, et
pas de flash possible en l'air. Ils reçoivent en plus un
`coef_repositionnement = 1.15` : déplacer la cible (vers son équipe ou hors de
la sienne) a une valeur au-delà de la durée du CC.

Coefficients : `coef_zone` = 1.5 si multi-cibles ; `coef_fiabilité` = 1.2 si
point-and-click, 1.0 si skillshot ; `coef_disponibilité` = 1.0 sort de base,
0.5 ultimate ; `coef_repositionnement` = 1.15 si le CC déplace la cible.

**Source des données** : ni Data Dragon ni l'API Riot n'exposent les types de
CC de façon structurée. → Import assisté depuis le **wiki LoL via son API
MediaWiki** (`api.php`, contenu sous licence CC BY-SA — attribution en en-tête
du fichier) : le wiki maintient une page par type de CC listant tous les sorts
concernés avec leurs durées. Un script **one-shot** interroge ces ~15 pages et
génère un brouillon de `data/external/cc_reference.csv` (champion, sort,
type_cc, durée, %slow, zone, fiabilité, disponibilité, repositionnement), puis
**relecture humaine obligatoire** avant de figer. Le fichier figé est immuable,
jamais mélangé aux données de match, re-versionné à chaque rework (script
relancé + relecture du diff). Même pattern que le scaling dpm.lol dans
macro-lab. Pas de scraping HTML, pas de screenshots/OCR : l'API MediaWiki
donne le texte proprement.

## Collecte

- **Scope** : Emerald+ sur **NA + EUW + KR**, segmenté par patch.
- Les rate limits Riot s'appliquent **par région de routage** : americas,
  europe et asia ont chacune leur budget (~100 req/2min en clé personnelle).
  Un match = 2 appels (match + timeline) → **~60-75K matchs/jour** en 24/24,
  soit ~1M de matchs par patch.
- Collector Python dérivé de celui de **macro-lab** (`C:\macro-lab`) : client
  Riot centralisé avec throttling et back-off 429, parsing des timelines et
  mappings d'events déjà validés là-bas.
- Le collector tourne 24/24 sur **Railway** et écrit dans **Postgres Railway**.
  L'interface lit la même base.

## Usage cible

Perso d'abord, public peut-être. Si le projet devient un site public : dossier
de **clé production Riot** obligatoire (produit enregistré chez Riot) — la clé
personnelle ne couvre que l'usage privé.

## Non-objectifs (pour l'instant)

- Pas de counters trio vs trio.
- Pas de comptes utilisateurs / auth.
- Pas d'analyse par joueur (c'est le territoire de macro-lab).
- Pas de scraping de sites tiers : données de match uniquement via l'API Riot.
