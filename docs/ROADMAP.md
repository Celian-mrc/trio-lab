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

## Phase 2 — Extraction des stats par match ✅

- [x] Identification du trio (jgl/mid/supp) par équipe et par match
      (`stats/extract.py`, branché à l'ingestion dans le collector +
      `stats/backfill.py` pour les matchs pré-Phase 2)
- [x] Extraction timeline : gold diff à 5/10/15/…, objectifs (grubs, héraut,
      drakes + ordre, Atakhan, Nashor), tours + emplacements + plaques,
      first blood, kill participation < 15 min
      (⚠️ `DRAGON_SOUL_GIVEN` = annonce du type d'âme, pas son obtention —
      l'âme se déduit du cumul de 4 drakes ; constaté sur timelines 16.13)
- [x] Stats de fin de match : vision, dégâts (part du trio), CC empirique
      (`timeCCingOthers`), durée
- [x] Tables agrégées : par champion (WR individuel), par duo, par trio —
      grain (patch, platform), rafraîchissement idempotent par patch
      (`003_aggregates.sql`, `python -m trio_lab.stats.aggregate`)
- [x] Tests sur timelines réelles archivées (2 matchs 16.13, valeurs attendues
      calculées indépendamment des extracteurs ; 79 tests au total)

## Phase 2b — Score CC théorique (parallélisable avec la Phase 3)

- [x] Script one-shot d'import via l'API MediaWiki du wiki LoL → brouillon
      `data/external/cc_reference.draft.csv` (503 sorts, 165 champions ;
      page « Types of Crowd Control/Sources » + templates de données des
      sorts, durées depuis la prose + fallback leveling, 253 lignes annotées
      `note_relecture`)
- [ ] Relecture humaine du brouillon, puis gel en
      `data/external/cc_reference.csv` (champion, sort, type_cc, durée, %slow,
      zone, fiabilité, disponibilité, repositionnement + attribution CC BY-SA)
- [ ] Calcul du score par sort/champion/trio (poids en fichier de config,
      airborne = 1.0 avec coef_repositionnement 1.15)
- [ ] Validation : corrélation score théorique ↔ `timeCCingOthers` empirique
- [ ] Tests + procédure de re-versionnage à chaque rework (relance du script,
      relecture du diff)

## Phase 3 — Scores de synergie

- [x] WR individuels par champion/rôle/patch (baseline, pondérés fenêtre)
- [x] Synergie duo (`score_duo`, synergie brute publiée)
- [ ] Validation des synergies duo contre les valeurs dpm.lol — **en attente
      de volume de collecte** (contrôle manuel sur duos très joués)
- [x] Synergie trio + lissage bayésien vers la prédiction issue des duos
      (prédiction = moyenne des 3 synergies de duo, elles-mêmes rétrécies vers
      0 pour éviter les priors extraits des mêmes matchs à faible volume ;
      k = 200 games-équivalents, à recalibrer avec le volume)
- [x] Fenêtre multi-patchs glissante (1-3 patchs, poids 1.0/0.6/0.35,
      coupure sur rework d'un membre — `REWORKS` dans windows.py, vide au
      démarrage 16.13)
- [x] Intervalles de confiance (Wilson 95 % sur n effectif) et tiers de
      fiabilité (faible < 50 ≤ moyen < 400 ≤ élevé)

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
