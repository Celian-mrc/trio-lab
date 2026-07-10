# Roadmap Trio Lab

Phase par phase : la phase N+1 ne démarre pas avant que la phase N soit verte
(tests passent, commit fait, cases cochées ici).

## Phase 0 — Cadrage et squelette ✅

- [x] Décisions de cadrage (scope Emerald+ NA/EUW/KR, Postgres Railway,
      réutilisation collector macro-lab, usage perso d'abord)
- [x] Docs de cadrage (PROJECT.md, ROADMAP.md, CLAUDE.md)
- [x] Schéma Postgres v0 en pseudo-code, validé avant implémentation
      (`migrations/001_init.sql`)

## Phase 1 — Collector ✅

- [x] Extraire/adapter le client Riot de macro-lab (throttling, back-off 429)
- [x] Support des 3 régions de routage (americas/europe/asia), budgets séparés
      (limiteur pulsefire par région, une boucle async par plateforme)
- [x] Découverte des joueurs Emerald+ (league-v4) par région (apex +
      entries paginé EMERALD/DIAMOND, plafonné par `--max-pages`)
- [x] Récupération match + timeline, filtrage ranked soloQ, dédoublonnage
      (PK `match_id` + journal `002_collector_journal.sql` ; timelines brutes
      archivées en JSON.gz local, jamais en base — extraction en Phase 2)
- [x] Écriture dans Postgres (Railway distant : migrations appliquées, 7 tests
      d'intégration verts contre `triolab_test`, smoke run réel validé —
      12 718 joueurs découverts, 2 matchs ingérés + timelines archivées)
- [x] Tests : parsing, dédoublonnage, respect des rate limits (mock)
      (45 tests, aucun appel réseau ; back-off 429/5xx prouvé via aioresponses)

## Phase 2 — Extraction des stats par match

- [ ] Identification du trio (jgl/mid/supp) par équipe et par match
- [ ] Extraction timeline : gold diff à 5/10/15/…, objectifs (grubs, héraut,
      drakes + ordre, Atakhan, Nashor), tours + emplacements + plaques,
      first blood, kill participation < 15 min
- [ ] Stats de fin de match : vision, dégâts (part du trio), CC empirique
      (`timeCCingOthers`), durée
- [ ] Tables agrégées : par champion (WR individuel), par duo, par trio —
      toujours segmentées par `patch` (fenêtre multi-patchs appliquée à la
      lecture, jamais au stockage)
- [ ] Tests sur timelines réelles archivées

## Phase 2b — Score CC théorique (parallélisable avec la Phase 3)

- [ ] Script one-shot d'import via l'API MediaWiki du wiki LoL (~15 pages de
      types de CC) → brouillon de `cc_reference.csv`
- [ ] Relecture humaine du brouillon, puis gel en
      `data/external/cc_reference.csv` (champion, sort, type_cc, durée, %slow,
      zone, fiabilité, disponibilité, repositionnement + attribution CC BY-SA)
- [ ] Calcul du score par sort/champion/trio (poids en fichier de config,
      airborne = 1.0 avec coef_repositionnement 1.15)
- [ ] Validation : corrélation score théorique ↔ `timeCCingOthers` empirique
- [ ] Tests + procédure de re-versionnage à chaque rework (relance du script,
      relecture du diff)

## Phase 3 — Scores de synergie

- [ ] WR individuels par champion/rôle/patch (baseline)
- [ ] Synergie duo (validation contre les valeurs dpm.lol)
- [ ] Synergie trio + lissage bayésien vers la prédiction issue des duos
- [ ] Fenêtre multi-patchs glissante (1-3 patchs, pondération décroissante,
      coupure sur rework d'un membre du trio)
- [ ] Intervalles de confiance et tiers de fiabilité

## Phase 4 — Counters

- [ ] WR du trio face à chaque champion ennemi individuel (par rôle)
- [ ] Même traitement statistique (seuils, lissage, confiance)

## Phase 5 — Interface

- [ ] API de lecture (FastAPI) sur Postgres
- [ ] Front : tier list des trios (filtres patch/région/rang), page détail
      trio (toutes les stats, counters), recherche par champion
- [ ] Choix du front à valider en début de phase (léger d'abord)

## Phase 6 — Déploiement Railway 24/24

- [ ] Collector en service Railway permanent (les 3 régions)
- [ ] Postgres Railway en production, rétention/rotation par patch
- [ ] Monitoring simple : volume collecté/jour, erreurs, 429
- [ ] Interface hébergée sur Railway (accès perso)
