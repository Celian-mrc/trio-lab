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
- [x] Recalibrage fréquence/cooldown (2026-07-12) : cooldown extrait du wiki
      (`|recharge=` pour les sorts à charges type Caitlyn W/Rumble E, sinon
      `|cooldown=`), coef_frequence borné ×1.0-1.5 (médiane des cooldowns
      extraits = 12 s), appliqué aux sorts de BASE uniquement (jamais les
      ultimates) — corrige Ashe passif, Garen Q, Udyr E notamment

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

## Phase 4 — Counters ❌ abandonnée (2026-07-19)

Implémentée (WR par champion ennemi individuel via `agg_trio_vs_champion`/
`score_trio_vs_champion`, meilleurs alliés Top/ADC via `agg_trio_with_ally`/
`score_trio_with_ally`), puis **retirée en totalité** : ces deux tables
`score_*` étaient déjà le plus gros poste de volumétrie du schéma (constaté
via `pg_stat_user_tables`, cf. mémoire `supabase-disk-growth`) alors que le
signal reste peu fiable (peu de games par combo trio×ennemi/allié, le
lissage bayésien ne compense pas assez). Tables droppées
(`migrations/022_drop_counters_and_allies.sql`), code retiré
(`synergy/counters.py`, `synergy/allies.py`, sections correspondantes de
`aggregate.py`/`service.py`/`maintenance.py`/`web/`).

## Phase 5 — Interface ✅

- [x] API de lecture (FastAPI) sur Postgres (`trio_lab.web` : /api/trios,
      /api/trios/{jgl}/{mid}/{sup}, /api/duos, /api/windows, /api/champions ;
      pool psycopg sync, routes `def` en threadpool — pas de piège event loop)
- [x] Front : tier list des trios (filtres fenêtre/région, champion+rôle,
      games min, fiabilité min, tri), page détail trio (stats détaillées
      agrégées à la volée sur match_trio_stats pondérées fenêtre, duos
      internes), tier list duos
      (pas de filtre « rang » : la collecte est scopée Emerald+ et l'en-tête
      de l'interface l'affiche — décision du 2026-07-11)
- [x] Page détail duo (2026-07-12) : mêmes stats que la page trio (`score_duo`
      porte déjà les mêmes colonnes), filtrées sur les 2 rôles fixés du duo
      quel que soit le 3e membre ; section « Meilleurs 3e membres » (top
      `score_trio` contenant ce duo, aucune nouvelle table) ; liens depuis la
      tier list des duos
- [x] Choix du front validé le 2026-07-11 : Jinja2 + htmx (hx-boost, vendorisé
      dans static/), un seul service à déployer ; noms/icônes champions via
      Data Dragon (index paresseux injectable dans les tests)
      — `python -m trio_lab.web`, port $PORT (défaut 8000)
- [x] Score de scaling (2026-07-12) : pente WR/durée de game (tranches de
      5 min, `agg_trio_duration`/`agg_duo_duration`, régression pondérée pure
      Python) — mesuré uniquement, pas de mélange avec la trajectoire gold
      (corrélation quasi nulle vérifiée empiriquement avant implémentation)
- [x] WR individuels des membres affichés sur les pages trio/duo + « WR avec
      l'âme » (2026-07-12)
- [x] Page détail champion (2026-07-12) : `/champion/{role}/{id}` — WR
      baseline, score CC théorique brut, meilleurs partenaires par rôle,
      meilleurs trios ; liens depuis les en-têtes trio/duo
- [x] CI GitHub Actions (2026-07-12) : ruff + pytest sur Postgres éphémère
      (service container), indépendante du déploiement Railway

## Phase 6 — Déploiement Railway 24/24

- [x] Collector en service Railway permanent (na1/euw1/kr, +eun1/br1 depuis
      le 2026-07-13 — chaque région a son propre budget de rate limit Riot,
      collecte concurrente via `asyncio.gather`, donc ajout ~gratuit tant que
      les régions ajoutées ont un volume Emerald+ suffisant)
      (`python -m trio_lab.collector --service` : patch courant auto via
      Data Dragon avec bornes de repli si PATCH_DATES incomplet, cycles
      batch → refresh agrégats/scores, archives timeline
      débrayées via ARCHIVE_TIMELINES=0, résilience par cycle)
- [x] Postgres Railway en production, rétention/rotation par patch
      (`trio_lab.maintenance` : purge quotidienne des matchs au-delà des
      3 patchs les plus récents, cascade ; agg_*/score_*/journal conservés)
- [x] Monitoring simple : volume collecté/jour, erreurs, 429
      (`GET /api/status` : matchs/jour 7 j par plateforme, total, dernier
      match, compteurs journal ; 429 visibles dans les logs Railway)
- [x] Interface hébergée sur Railway (accès perso) — déployée le 2026-07-11
      avec le service collector (build Dockerfile commun, checklist et pièges
      dans `docs/DEPLOY.md` ; ~100 K matchs/jour constatés au démarrage)

## Phase 7 — Duo généralisé (n'importe quelle paire de rôles)

Le trio jgl/mid/sup reste le cœur du produit (pipeline `match_trio_stats`
inchangé) ; le duo devient utilisable pour les 10 combinaisons de rôles
possibles (pas seulement les 3 internes au trio), comme le fait dpm.lol mais
avec le même niveau de détail que les pages trio/duo existantes.

- [x] `migrations/023_match_role_stats.sql` : table brute par rôle individuel
      (5 rôles, gold BRUT par checkpoint — pas de diff précalculé, dérivé à
      l'agrégation) — table séparée, `match_trio_stats` non touchée
- [x] `migrations/024_widen_duo_roles.sql` : CHECK `agg_duo`/`score_duo.roles`
      élargi aux 7 nouvelles paires (top_jgl, top_mid, top_bot, top_sup,
      jgl_bot, mid_bot, bot_sup)
- [x] `stats/extract.py` : `extract_role_stats()` (fonctions indépendantes de
      `extract_match`, jamais appelées à sa place) → 10 lignes/match
- [x] `stats/aggregate.py` : 2e requête `agg_duo` pour les 7 nouvelles paires,
      sourcée sur `match_role_stats` (gold diff réel de la paire par
      auto-jointure avec l'équipe adverse ; objectifs récupérés par jointure
      sur `match_trio_stats`, pas dupliqués)
- [x] `synergy/compute.py` : `DUO_ROLES` élargi à 10 entrées — le reste du
      pipeline scores (`score_duo`) était déjà générique sur `roles`
- [x] Web : `queries.duo_role_match_rows()` (nouvelle source pour les 7
      paires, colonnes `champ_a/b_cc_time_s`/`champ_a/b_dmg_per_gold`
      génériques réutilisées telles quelles par `summary.summarize`) ;
      `/duos` et `/duo/{roles}/...` déjà génériques sur `roles`, juste
      étendus (sélecteur, badges) ; pas de section « meilleurs 3e membres »
      pour les paires hors trio (pas de notion de trio au-delà de jgl/mid/sup)
- [ ] **Pas de backfill possible** : `match_role_stats` vient de la timeline
      brute, jamais conservée après extraction (CLAUDE.md, pas de JSON brut en
      base) — les 7 nouvelles paires démarrent à vide et grossissent
      seulement à partir du déploiement, contrairement aux 3 historiques (déjà
      des mois de profondeur via `match_trio_stats`)
- [x] Cohérence trio/duo (2026-07-19) : la page détail duo affiche les mêmes
      cartes que la page trio (Avantage gold, Objectifs, Combat, Vision),
      pour les 10 paires — `migrations/025_role_stats_combat.sql` ajoute
      `damage`/`first_blood`/`kp_pre15` à `match_role_stats`.
      Pour les 3 paires historiques : stats déjà présentes dans `t.*`
      (`match_trio_stats`), zéro nouveau calcul, affichées telles quelles
      (contexte d'équipe, pas attribuées à ces 2 joueurs en particulier —
      même principe que la synergie/WR déjà affichés). Pour les 7 nouvelles :
      décomposition réelle à 2 membres — gold diff (auto-jointure équipe
      adverse), vision/wards (somme exacte), part de dégâts (somme exacte,
      aucune ambiguïté), first blood (OR exact, un seul événement). Exception
      volontaire : le kill participation < 15 min reste INDIVIDUEL par membre
      pour les nouvelles paires (pas combiné en « au moins un des deux » —
      demanderait de revérifier l'appartenance des 2 pids à chaque kill,
      risque de double-comptage) ; objectifs (grubs/héraut/drakes/âme/
      Nashor/tours/plaques) restés team-level partout, structurellement non
      attribuables à un sous-ensemble de joueurs (`match_objective_events`
      n'a pas de tueur identifié, seulement un `team_id`).
- [x] Counters 1v1 même rôle (2026-07-19, `migrations/026_role_matchups.sql`) :
      retour redesigné des counters Phase 4 (abandonnés en 022) — le problème
      initial était la dimension TRIO (jgl×mid×sup×ennemi×rôle, combinatoire
      intraitable), pas le concept de counter. Grain = duel même rôle
      (champ_a vs champ_b, ex. top vs top), comme les outils de draft du
      marché (METAsrc Counter Picker etc.) : `agg_matchup` (auto-jointure
      `match_participants`, même match/rôle, équipe adverse — historique
      complet, aucune dépendance à `match_role_stats`), `score_matchup`
      (`synergy/matchups.py`, delta lissé vs baseline `agg_champion`, même
      mécanique que `synergy/compute.py`). Combinatoire borné comme
      `agg_duo`/`score_duo`, pas d'explosion. Branché dans le cycle service
      et la rétention. CLAUDE.md nuancé : counter trio toujours exclu,
      counter 1v1 même rôle OK.

## Phase 8 — Onglet Coach (en cours)

Suite aux recherches sur les outils de draft du marché (ProComps, DraftForge,
LoLDraftAI, METAsrc Counter Picker) : trio-lab reste le seul à faire de la
synergie de TRIO, mais peut couvrir le reste (draft, méta, "ce qui fait
gagner") avec les données déjà en place.

- [x] Simulateur de draft interactif (2026-07-19) : `/draft` — état
      entièrement dans l'URL (query params, pas de session serveur, même
      principe que window/platform), 5 rôles × 2 équipes + bans. Pour
      chaque slot vide : edge = Σ synergie avec les alliés déjà posés
      (`champion_best_partners`, existant) + delta counter vs l'ennemi même
      rôle (`queries.matchup_candidates`, nouveau, symétrique de
      `champion_best_partners`) — même unité (points de WR) donc sommable
      sans pondération arbitraire. 1er pick d'un rôle (rien de verrouillé) :
      repli sur le WR baseline (`champion_role_baseline_list`, nouveau).
      Candidat sans donnée commune : contribution 0, jamais exclu. Fiabilité
      grisée sous `DRAFT_MIN_GAMES_EFF` (50, cohérent avec le tier "moyen"),
      jamais filtrée. hx-boost (déjà en place) rend chaque pick réactif sans
      JS custom.
- [x] Dashboard "ce qui fait gagner" (2026-07-19) : `/insights` — régression
      logistique multi-variables (`synergy/win_factors.py`, IRLS pure Python,
      cohérent avec la philosophie du projet — pas de numpy/scipy pour ~10
      variables sur quelques dizaines de milliers de lignes), matérialisée
      dans `score_win_factors` (migration 027). Deux populations : toutes
      les games, et celles où le trio est derrière au gold à 15 min (leviers
      de comeback, différents — vision/efficacité ressources y pèsent 2-3x
      plus, cf. recherches de session). Poids par patch identiques à
      `synergy.compute` (`weights_for`), appliqués comme poids d'observation
      dans l'IRLS, pas une repondération a posteriori. Rafraîchissement
      MANUEL (`python -m trio_lab.synergy.win_factors --patches X`), jamais
      dans le cycle service — même philosophie que `ccref.sync_theoretical`
      (signal de patch, pas de cycle de collecte).
- [ ] Détecteur de picks flex/hybrides (profil gold/CC/dmg-per-gold d'un
      champion entre ses rôles via `match_role_stats`) — pas encore construit.
