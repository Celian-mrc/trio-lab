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

## Phase 2b — Score CC théorique (parallélisable avec la Phase 3) ✅

- [x] Script one-shot d'import via l'API MediaWiki du wiki LoL → brouillon
      `data/external/cc_reference.draft.csv` (503 sorts, 165 champions ;
      page « Types of Crowd Control/Sources » + templates de données des
      sorts, durées depuis la prose + fallback leveling, 253 lignes annotées
      `note_relecture`)
- [x] Relecture humaine du brouillon (3 passes de Célian : valeurs, zones,
      fiabilité/conditionnel), puis gel en `data/external/cc_reference.csv`
      le 2026-07-11 (480 lignes + attribution CC BY-SA ; colonnes étendues :
      conditionnel 0/1, fiabilité = « non esquivable par déplacement »)
- [x] Calcul du score par sort/champion/trio (`ccref/score.py` : poids et
      coefs en config, airborne 1.0 × repositionnement 1.15, conditionnel 0.7,
      CC durs simultanés d'un même sort non cumulés — règle du max)
- [x] Validation : corrélation score théorique ↔ `timeCCingOthers` empirique
      (162 champions ≥ 30 games : Spearman 0.744, Pearson 0.503 — les écarts
      viennent des CC répétables type on-hit, non modélisés : piste de
      recalibrage fréquence/cooldown notée)
- [x] Tests + procédure de re-versionnage à chaque rework : relancer
      `python -m trio_lab.ccref`, relire le diff du brouillon (les arbitrages
      de `cc_reference.overrides.csv` sont réappliqués automatiquement),
      puis `--freeze`

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

## Phase 4 — Counters ✅

- [x] WR du trio face à chaque champion ennemi individuel (par rôle)
      (`agg_trio_vs_champion`, migration 006, jointure trio × participants
      adverses dans `stats.aggregate` — jamais de trio vs trio)
- [x] Même traitement statistique (seuils, lissage, confiance)
      (`synergy/counters.py` → `score_trio_vs_champion` : delta = WR(trio vs
      ennemi) − WR global du trio sur la même fenêtre, rétréci vers 0 avec le
      même k ; coupure de rework étendue à l'ennemi ; CLI
      `python -m trio_lab.synergy --patches X --counters` ; les deltas
      resteront ~0 tant que le volume par matchup est faible — max 7 games au
      2026-07-11)

## Phase 5 — Interface ✅

- [x] API de lecture (FastAPI) sur Postgres (`trio_lab.web` : /api/trios,
      /api/trios/{jgl}/{mid}/{sup}, /api/duos, /api/windows, /api/champions ;
      pool psycopg sync, routes `def` en threadpool — pas de piège event loop)
- [x] Front : tier list des trios (filtres fenêtre/région, champion+rôle,
      games min, fiabilité min, tri), page détail trio (stats détaillées
      agrégées à la volée sur match_trio_stats pondérées fenêtre, duos
      internes, pires/meilleurs matchups), tier list duos
      (pas de filtre « rang » : la collecte est scopée Emerald+ et l'en-tête
      de l'interface l'affiche — décision du 2026-07-11)
- [x] Choix du front validé le 2026-07-11 : Jinja2 + htmx (hx-boost, vendorisé
      dans static/), un seul service à déployer ; noms/icônes champions via
      Data Dragon (index paresseux injectable dans les tests)
      — `python -m trio_lab.web`, port $PORT (défaut 8000)

## Phase 6 — Déploiement Railway 24/24

- [x] Collector en service Railway permanent (les 3 régions)
      (`python -m trio_lab.collector --service` : patch courant auto via
      Data Dragon avec bornes de repli si PATCH_DATES incomplet, cycles
      batch → refresh agrégats/scores/counters, archives timeline
      débrayées via ARCHIVE_TIMELINES=0, résilience par cycle)
- [x] Postgres Railway en production, rétention/rotation par patch
      (`trio_lab.maintenance` : purge quotidienne des matchs au-delà des
      3 patchs les plus récents, cascade ; agg_*/score_*/journal conservés)
- [x] Monitoring simple : volume collecté/jour, erreurs, 429
      (`GET /api/status` : matchs/jour 7 j par plateforme, total, dernier
      match, compteurs journal ; 429 visibles dans les logs Railway)
- [ ] Interface hébergée sur Railway (accès perso) — code prêt
      (`python -m trio_lab.web`, $PORT géré) ; **déploiement dashboard à
      faire par Célian, checklist dans `docs/DEPLOY.md`**
