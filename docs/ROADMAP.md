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
- [x] **Bug corrigé (2026-07-20, retour utilisateur)** : le choix ci-dessus
      pour les 3 paires historiques ("zéro nouveau calcul, affichées telles
      quelles") s'est révélé être un vrai défaut, pas neutre : Gold@,
      Vision/min et CC/min (le total, pas la répartition par membre — déjà
      correcte) affichaient en réalité le chiffre du TRIO ENTIER, pas de la
      paire regardée — sur la vue Jungle+Mid, le nombre incluait quand même
      le Support. `stats/aggregate.py` (`_DUO_STAT_SUMS_SQL`) calcule
      désormais ces 3 stats pair-spécifiques pour les 3 paires internes,
      même principe que les 7 étendues (`_DUO_EXT_SQL`) — mais en LEFT JOIN
      + `coalesce(..., valeur trio-wide)`, PAS en INNER JOIN comme les 7
      étendues : `match_role_stats` a un historique bien plus court que
      `match_trio_stats` (16.14+ seulement), un INNER JOIN aurait effacé
      silencieusement les patchs plus anciens (16.13, 700k+ games) au
      prochain refresh (même piège que `agg_matchup`, cf. mémoire
      `agg-matchup-backfill-gap`) — retombe sur l'ancien calcul trio-wide
      quand la donnée pair-spécifique n'existe pas, jamais de perte.
      Objectifs (drakes/soul/héraut/tour) restent team-level, inchangés.
- [x] **Bug corrigé (2026-07-20, retour utilisateur)** : `agg_duo_duration`
      (source du score de Scaling) était resté limité aux 3 paires internes
      au trio (jgl_mid/jgl_sup/mid_sup) — la Phase 7 avait bien élargi
      `agg_duo`/`score_duo` aux 10 paires (migration 024) mais oublié
      `agg_duo_duration` (CHECK constraint ET requête d'agrégation), donc
      Scaling restait NULL pour les 7 paires étendues (ex. bot_sup) quel que
      soit le volume — constaté sur Ashe + Séraphine, 900+ games, toujours
      NULL. `migrations/029_widen_duo_duration_roles.sql` élargit le CHECK ;
      `stats/aggregate.py` ajoute `_DUO_DURATION_EXT_SQL` (symétrique de
      `_DUO_EXT_SQL`, sourcée sur `match_role_stats`). Backfill possible
      seulement pour les patchs où `match_role_stats` est encore retenu
      (16.14 au moment du fix — 16.13 déjà purgé, comme pour `agg_matchup`).
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

- [x] Simulateur de draft interactif (2026-07-19, refonte façon champ select
      le même jour suite au retour « je ne comprends pas comment ça
      fonctionne ») : `/draft` — état entièrement dans l'URL (query params,
      pas de session serveur), 5 rôles × 2 équipes + bans. Un seul slot
      "actif" à la fois (`active` en query param, 1er slot vide par défaut,
      avance automatiquement après un pick — `DRAFT_SLOT_ORDER`) : la grille
      du slot actif liste TOUT le roster disponible du rôle (pas de liste
      tronquée), trié par edge = Σ synergie avec les alliés déjà posés
      (`champion_best_partners`) + delta counter vs l'ennemi même rôle
      (`queries.matchup_candidates`) — même unité (points de WR) donc
      sommable sans pondération arbitraire ; les `DRAFT_RECOMMENDED_COUNT`
      (12) premiers sont badgés "Recommandé", le reste reste cliquable. 1er
      pick d'un rôle (rien de verrouillé) : repli sur le WR baseline
      (`champion_role_baseline_list`). Candidat sans donnée commune :
      contribution nulle, jamais exclu. Fiabilité grisée sous
      `DRAFT_MIN_GAMES_EFF` (50), jamais filtrée — MAIS triée (retour
      utilisateur 2026-07-19 : le WR baseline n'est jamais lissé,
      contrairement à `edge` ; sans tri par fiabilité un champion à 25
      games peut passer devant un champion à 1000+ games pour un écart de
      WR qui n'est que du bruit). Roster limité aux champions avec ≥ 1 game
      réelle dans ce rôle (`agg_champion`) — pas de WR inventé sur 0 game ;
      sur une seule région un rôle inhabituel peut afficher moins de
      candidats (ex. jungle en KR : 141/173 champions), "toutes régions"
      donne la couverture la plus large. **Sécurité blind
      pick** (retour utilisateur : « un blind pick est un pick qui a peu de
      counter, ou dont les counters n'ont pas un énorme WR contre lui ») :
      quand aucun ennemi même rôle n'est verrouillé, chaque candidat affiche
      le NOMBRE de contres notables (delta ≤ `DRAFT_NOTABLE_COUNTER_DELTA`,
      -3 pts) et le pire d'entre eux (`queries.role_worst_matchups`, un seul
      aller-retour par rôle) — pas seulement le pire cas isolé, qui ne
      distingue pas un champion avec un seul contre sévère d'un champion
      avec dix contres modérés. hx-boost rend chaque pick réactif sans JS
      custom.
- [x] Dashboard "ce qui fait gagner" (2026-07-19, reconstruit le même jour
      suite au retour « pourquoi ça ne parle que du trio jgl/mid/sup ? »,
      puis révisé le 20/07 suite à un audit méthodologique en 2 recherches
      approfondies — sources dans le commit) : `/insights` — régression
      logistique multi-variables (`synergy/win_factors.py`, IRLS pure
      Python), matérialisée dans `score_win_factors` (migration 027), sur
      l'**équipe complète des 5 rôles** (`match_role_stats`) : gold
      d'équipe, CC/vision d'équipe, CS jungle vs adverse à 15 min,
      objectifs. `damage_share` et `kill_participation_pre15` abandonnés
      dès la reconstruction du 19/07 (pas d'interprétation team-wide
      valable). **Dégâts/gold par rôle retiré le 20/07** (audit
      méthodologique, sources Persoskie & Ferrer 2017 *Am J Prev Med*,
      Christiansen/Gensby/Weber 2022 *IEEE ToG* : ce ratio reflète surtout
      l'archétype du champion — tank/support à dégâts/gold structurellement
      bas — pas une performance actionnable). Diagnostic de colinéarité
      (`_compute_vif`, VIF par régression auxiliaire, seuil d'alerte 5) à
      chaque ajustement, loggé, renforce le `ridge` de l'IRLS sans jamais
      orthogonaliser manuellement (déconseillé par la littérature citée —
      biaise l'interprétation). Affichage en **probabilité absolue**
      (ex. « 50 % → 78 % »), pas seulement en odds ratio (qui exagère
      l'effet perçu quand l'issue est ~50 %, pas rare). Deux populations
      (toutes games / derrière au gold à 15 min) affichées dans un même
      tableau, une ligne par feature dans un ordre fixe
      (`_combined_win_factors`) — jamais deux tableaux qui peuvent
      désaligner. Rafraîchissement MANUEL
      (`python -m trio_lab.synergy.win_factors --patches X`).

- [x] Modèle "qu'est-ce qui construit l'avantage au gold" (2026-07-20, phase
      2 de l'audit méthodologique) : nouvelle section sur `/insights`,
      `synergy/gold_factors.py` — OLS pondéré (forme fermée, pas d'IRLS,
      cible continue), matérialisé dans `score_gold_factors` (migration
      028). En amont de `win_factors` (qui prédit la victoire à partir du
      gold ; ici on prédit `gold_diff_15` lui-même — répond à « qu'est-ce
      qui CONSTRUIT l'avantage », pas seulement « avoir l'avantage prédit
      la victoire », question quasi tautologique que le modèle précédent
      ne pouvait pas poser). Un seul modèle à 2 blocs temporellement
      ordonnés (pas de cascade — biais des "generated regressors", Pagan
      1984), R²(draft seul) et R²(complet) rapportés séparément (2 fits
      imbriqués) pour montrer ce que chaque étape ajoute :
      - **Draft** : `team_baseline_wr` (WR des picks, `agg_champion` AU
        PATCH de la game — pas la fenêtre), `team_matchup_delta`
        (`score_matchup`, moyenné sur les rôles disponibles),
        `team_trio_synergy` (`score_trio`, jgl/mid/sup seulement — top/bot
        sans synergie native pour l'instant, cf. mémoire trio-lab).
        3 scores agrégés au lieu du one-hot sur ~170 champions × 5 rôles
        (target/mean encoding régularisé, Micci-Barreca 2001).
      - **Exécution 0-15 min** : `jgl_cs_diff_15`, `first_blood_team`
        (`match_role_stats.first_blood` agrégé par OR, `coalesce` à false
        pour les games antérieures à la migration 025 sans rétro-remplissage).
      - Diagnostic VIF + ridge adaptatif : extrait dans `synergy/_linalg.py`
        (module partagé avec `win_factors.py`, même Gauss/VIF, plus
        `fit_weighted_ols`/`weighted_r_squared` pour l'OLS).
      Résultat sur 16.14+16.13 (118k lignes, avant l'ajout ci-dessous) :
      R²(draft seul) ≈ 1,9 %, R²(complet) ≈ 26,2 % — le draft explique très
      peu de l'avantage au gold à 15 min, l'exécution précoce domine
      largement. Cohérent avec la littérature citée dans l'audit (picks
      seuls proches du hasard en LoL, features in-game très supérieures).
      Piège Postgres identique à `win_factors` rencontré et contourné
      (`enable_nestloop = off`, cf. mémoire).
- [x] Héraut/dragon/wards AVANT 15 min (2026-07-20, `migrations/030_pre15_objectives.sql`,
      retour utilisateur) : `herald_taken`/`soul_taken`/`vision_score`
      existants sur `match_trio_stats` n'ont AUCUNE coupure à 15 min
      (calculés sur la partie entière) — inutilisables dans le bloc
      exécution de `gold_factors` sans risquer une causalité inversée. 3
      nouvelles colonnes, dérivées dans `stats/extract.py::pre15_stats` :
      - `herald_taken_pre15`/`dragons_taken_pre15` (compte exact, pas de
        `soul_pre15` — 4 drakes avant 15 min n'arrive essentiellement
        jamais) : depuis les events de timeline déjà timestampés
        (RIFT_HERALD/DRAGON), coupure exacte à 15 min.
      - `wards_pre15` : Riot n'expose `visionScore` qu'en cumulé fin de
        partie, à AUCUN timestamp intermédiaire (ni `detail` ni les frames
        de la timeline) — impossible d'en dériver une version bornée
        fidèle. Proxy le plus honnête : wards posées + détruites avant
        15 min, lu directement dans les events bruts WARD_PLACED/WARD_KILL
        de la timeline (absents de `match_objective_events`, qui ne garde
        que les objectifs de map). Pas identique à `vision_score`.
      Ajoutées au bloc exécution de `gold_factors.py`. **Pas de backfill
      possible** (timeline brute jamais conservée, CLAUDE.md) — NULL sur
      tout l'historique déjà collecté, `_FETCH_SQL` filtre sur leur
      présence plutôt que planter ; se peuplent à partir du déploiement,
      même situation que les 7 paires de duo étendues (Phase 7).
- [x] Détecteur de picks flex/hybrides (2026-07-19, seuils revus le même
      jour suite au retour « il y a peu de flex picks ») : `/flex` — rôle
      secondaire non anecdotique (`agg_champion`, historique complet : ≥ 5 %
      des games du champion ET ≥ 100 games brutes) dont le profil de gold à
      15 min (`match_role_stats`, ≥ 30 games) dévie de la moyenne du rôle
      d'au moins `FLEX_MIN_DEVIATION` (5 % — plancher de significativité,
      remplace un ancien plafond arbitraire de 20 résultats affichés qui
      masquait silencieusement 137 candidats réels sur 157). Phrase en
      langage clair par ligne + filtre par rôle secondaire. Calcul live (pas
      de table matérialisée, ~1s sur prod).
- [x] Profil de résilience par champion (2026-07-20,
      `migrations/031_champion_resilience.sql`, retour utilisateur) : `/resilience`
      — répond à « pas de combinaison parfaite universelle de métriques,
      des champions différents dépendent différemment de chaque facteur »
      (exemple qui a lancé la discussion : Nasus jungle mené au gold@15
      dans 60 % de ses games, WR 34 % dans cet état, mais ~52 % au global).
      `synergy/resilience.py` matérialise, PAR (rôle, champion, facteur),
      l'écart de WR entre "en avance" et "en retard". 3 facteurs retenus
      après vérification empirique en session (corrélations de Pearson,
      ~50k lignes prod) pour leur signal réel ET leur indépendance
      mutuelle : `team_gold_diff_15` (r=0,535 avec la victoire),
      `jgl_cs_diff_15` (r=0,279 avec la victoire, r=0,456 avec le gold —
      recoupe partiellement), `first_blood_team` (r=0,123, largement
      indépendant). CC/min écarté malgré son indépendance du gold
      (r=0,084) : trop faiblement corrélé à la victoire (r=0,092) pour
      produire des écarts par champion fiables plutôt que du bruit —
      candidat d'extension si le volume grandit. Même piège
      Postgres/contournement que win_factors/gold_factors.
- [x] **Révision `/resilience` (2026-07-20, retour utilisateur)** : lignes
      sous `RESILIENCE_MIN_GAMES_PER_SIDE` désormais **exclues**, plus
      grisées (une ligne illisible n'apporte rien) — filtres par seuil
      min/max ajoutés (games total, écart, WR en avance/en retard),
      calculés en Python après lecture (volume trop petit pour justifier du
      SQL dédié, contrairement aux 13 colonnes de `/`/`/duos`).
      **Bug de message corrigé le jour même** : la page vide pointait
      toujours vers `synergy.resilience --patches ...`, y compris quand la
      donnée existait déjà mais qu'un filtre ne matchait rien (ex. sur la
      fenêtre 16.14+16.13, aucun champion ne dépasse 46 % de WR en retard ni
      un écart sous 24 % — un filtre "WR en retard min. 50" est un choix
      naturel mais ne peut structurellement rien retourner). `_resilience_rows`
      retourne maintenant aussi le nombre de lignes fiables AVANT filtres :
      message "pas encore calculé" seulement s'il est à 0, sinon "aucun
      champion ne correspond à ces filtres".
      **2e bug, plus grave, trouvé en creusant le signalement utilisateur**
      (le message ci-dessus n'expliquait pas tout : l'utilisateur avait
      "0 champions" en cliquant Filtrer AVANT même de toucher un filtre) :
      `<select name="role">` envoie `role=` (chaîne vide) au submit, jamais
      une clé absente — `champion_resilience` testait `role is not None`,
      donc choisir "tous" ajoutait silencieusement `AND role = ''` en SQL
      (ne matche jamais rien). Corrigé par `role = role or None` juste après
      validation dans `resilience_page`. Piège classique de ce projet (même
      commentaire déjà présent ailleurs dans `app.py` pour `min_tier`/champ
      search), raté ici faute de test qui simule un VRAI submit de formulaire
      (les tests précédents omettaient `role` plutôt que d'envoyer `""`).
- [x] **`/resilience` passé en rafraîchissement automatique (2026-07-20,
      retour utilisateur)** : jusqu'ici manuel comme `win_factors`/
      `gold_factors` (philosophie initiale de la migration 031). Coût mesuré
      avant de trancher : ~13s pour ~2500 lignes, négligeable face à un
      cycle qui dure déjà plusieurs minutes (rate limit Riot) —
      `resilience.refresh(window, dsn=dsn)` ajouté dans
      `service.refresh_scores`, juste après `matchups.refresh`. Ajouté aussi
      à `maintenance._SCORE_TABLES` (purge automatique par fenêtre) : elle
      ne l'était pas, contrairement à `score_win_factors` — sans ça,
      `score_champion_resilience` aurait accumulé une fenêtre non purgée à
      chaque rollover de patch. `win_factors`/`gold_factors` restent
      manuels, eux (pas demandé, pas de mesure de coût faite pour eux).
- [x] **Zone neutre sur `team_gold_diff_15` (2026-07-20, retour
      utilisateur)** : le seuil "en avance"/"en retard" (0 gold, invisible
      sur la page) comptait -50 gold comme "en retard" au même titre que
      -3000 — dilution du signal, question légitime de l'utilisateur en
      creusant "de combien est le seuil ?". Vérifié empiriquement avant de
      choisir la largeur : écart médian en valeur absolue = 2597 gold
      (fenêtre 16.14+16.13, 287k lignes équipe), un écart franc est la
      norme. `_NEUTRAL_ZONES = {"team_gold_diff_15": 1000.0}` dans
      `resilience.py` : les games à moins de ±1000 gold sont ignorées pour
      ce facteur (ni avance ni retard), pas de zone neutre pour
      `jgl_cs_diff_15`/`first_blood_team` (pas demandé, `first_blood_team`
      est booléen). Seuil maintenant affiché sur la page
      (`RESILIENCE_FACTOR_THRESHOLDS`).
- [x] **Badges de rôle top/adc gris (2026-07-20, retour utilisateur)** :
      `.role-jgl`/`.role-mid`/`.role-sup` avaient une couleur, `.role-top`/
      `.role-bot` non — ajoutées (`style.css`).
- [x] **Diff gold@15 de l'équipe entière sur `/` et `/duos` (2026-07-20,
      `migrations/032_team_gold15.sql`, retour utilisateur)** : jusqu'ici
      seul le gold@15 du trio/duo lui-même était affiché. Nouvelle colonne
      "Gold@15 équipe", sourcée sur `match_role_stats` (5 rôles, LEFT JOIN
      jamais INNER — même précaution que le fix des 3 duos internes
      ci-dessus) : NULL sur les patchs antérieurs à 16.14 (pas de backfill
      possible), se peuple tout seul au fil des patchs suivants. Colonnes
      dédiées `team_gold15_sum/n` (agg_trio/agg_duo) et `team_gold_diff_15`
      (score_trio/score_duo) — ajout d'une entrée à `compute.STAT_PAIRS`
      suffit à la faire traverser tout le pipeline de moyenne pondérée
      fenêtre existant, aucune autre logique dupliquée.
- [x] **Révision `/flex` (2026-07-20, retour utilisateur)** : 3 défauts
      remontés — colonnes non triables, `×0.85` peu parlant, utilité du
      dégâts/gold questionnée. Corrigés ensemble :
      - Colonnes triables (`sort`/`dir`, même mécanisme à seuil unique que
        `/resilience` — pas le tri multi-colonnes façon tableur de `/`/
        `/duos`, pas nécessaire sur ce volume déjà filtré).
      - `×0.85`/`×1.08` remplacés par un écart % signé et coloré (`-15 %`),
        cohérent avec le reste du site (`gold_diff`, `synergy`) et avec la
        phrase de résumé déjà affichée sur chaque ligne.
      - Dégâts/gold : même traitement (écart % coloré au lieu de 2 valeurs
        brutes côte à côte) plutôt que retiré — reste une mesure distincte
        du gold (efficacité, pas juste la quantité de ressources).
      - Nouvelle colonne **WR du rôle secondaire** (`agg_champion.wins`,
        pas encore sélectionnée avant ce jour) : répond à « est-ce que ce
        pick flex gagne vraiment », question que le seul profil de
        ressources ne peut pas poser (un profil qui dévie mais un WR qui
        s'effondre n'est pas le même signal qu'un profil qui dévie ET
        gagne). Baseline du rôle calculée gratuitement en réutilisant
        `champion_role_distribution` déjà chargée en mémoire (pas de
        requête SQL supplémentaire).
- [x] **Bug de tri corrigé sur `/flex` le jour même (retour utilisateur)** :
      le tri initial de "Gold@15 vs moyenne"/"Dégâts/gold" comparait par
      MAGNITUDE (`abs()`), pas par signe — croissant/décroissant changeait
      bien l'ordre mais mélangeait + et - sans redonner un classement
      cohérent, contraire à la convention déjà en place sur `/`/`/duos`
      (tri par valeur brute). Corrigé en triant par la valeur signée ;
      `_sort_flex_picks` gère `None` (dégâts/gold manquant) toujours en
      dernier quel que soit le sens (même principe que `NULLS LAST` en
      SQL), une comparaison directe `None < float` aurait sinon levé une
      exception. Régression testée avec un 3e champion à déviation
      négative (les 2 précédents étaient tous deux positifs, insuffisant
      pour distinguer un tri par signe d'un tri par magnitude).
- [x] **Audit `win_factors` (2026-07-24, retour utilisateur)** : parti d'une
      question simple ("le modèle est-il bon ? quel AUC ?") — réponse :
      aucun AUC n'était calculé nulle part. Mesuré à la main puis comparé au
      modèle dédié de `macro-lab` (`wp_v3`, AUC@15 = 0,813, test set propre,
      anti-fuite vérifiée) : `win_factors` ressortait à 0,852 en échantillon,
      ce qui aurait dû alerter plutôt que rassurer (un modèle à 4 variables
      "battant" un modèle à 28 variables par rôle est suspect). Cause
      trouvée : `herald_taken`/`soul_taken`/`first_tower` sont des résultats
      DE FIN DE PARTIE (l'âme de dragon n'arrive quasiment jamais avant
      25-30 min), pas bornés à 15 min — `gold_factors.py` les excluait déjà
      pour cette raison précise, `win_factors` ne s'appliquait pas la même
      règle à lui-même. Corrigé :
      - Les 3 variables retirées de `FEATURES` (AUC sans elles : 0,822 —
        baisse modeste, beaucoup du signal de `soul_taken` recoupait déjà
        `team_gold_diff_15`).
      - AUC hors-échantillon désormais calculé et affiché sur `/insights`
        (`_auc_test`, ligne de diagnostic comme `gold_factors._r2_*`) :
        split déterministe 80/20 par hash `crc32(match_id)` (jamais les 2
        perspectives d'un match dans des ensembles différents), coefficients
        SERVIS ajustés séparément sur 100 % des données (précision
        maximale) — l'ajustement train-only ne sert qu'au diagnostic.
      - Piste "facteur X" (champions qui gagnent plus/moins que leurs stats
        mesurées ne le prédisent, cf. recherche du même jour sur l'analyse
        de résidus façon xG) mise en pause : elle dépendait de ce modèle,
        pas construite tant que l'audit n'était pas fait.
- [x] **`×N` retiré de `/insights` (2026-07-24, retour utilisateur, "perturbant
      et pas très lisible")** : le tableau "Ce qui fait gagner" n'affiche
      plus l'odds ratio, seulement le swing de probabilité déjà calculé
      (« 49 % → 81 % ») — c'était déjà le format recommandé dans le texte
      d'aide ("×N a tendance à exagérer l'effet perçu quand gagner/perdre
      est ~50/50"), maintenant le seul affiché. Comportement inchangé côté
      calcul (`_win_prob_swing`), uniquement `insights.html`/tests touchés.
      Texte d'aide aussi clarifié sur le piège de la colonne "Derrière au
      gold" : "+1 écart-type" n'y veut pas dire la même chose qu'en
      population complète (toutes les games y sont déjà en déficit — le
      facteur y mesure "un peu moins en retard", pas "passer en avance"),
      confusion remontée par l'utilisateur en creusant le tableau.
- [x] **Compositions suggérées sur `/draft` (2026-07-24, retour
      utilisateur ; algorithme revu le 2026-07-25, 2e retour utilisateur)** :
      "un système qui propose 3 drafts, peu importe les champions en face,
      avec des recommandations sur comment les jouer". Nouvelle section
      indépendante du simulateur pick-par-pick existant (repart toujours de
      zéro, ne complète pas les picks en cours).
      **v1 (2026-07-24)** : partait du meilleur TRIO jgl/mid/sup par
      synergie, complété par le TOP puis l'ADC. **Remplacée le jour même** :
      un trio exige 3 champions précis simultanément, bien moins de données
      qu'un duo (2 champions) — retour utilisateur.
      **v2 (2026-07-25)** : part d'un DUO parmi les 10 paires de rôles
      possibles (`score_duo`, `roles=None` — mêmes données que le filtre
      "tous les rôles" de `/duos`), puis étend rôle par rôle SANS ordre
      fixe : à chaque étape, le (rôle, champion) qui maximise la Σ synergie
      avec TOUT ce qui est déjà posé, quel que soit le rôle restant. La Σ
      des scores ajoutés à chaque étape (2 ancrages, puis 3, puis 4) couvre
      exactement les 10 paires d'un draft à 5 (1+2+3+4=10=C(5,2)) : le total
      final est la vraie somme de synergie sur toutes les paires, pas une
      approximation.
      **4 profils de poids ("archétypes", 2e retour utilisateur : "une
      bonne draft pro c'est une draft qui scale, qui a un avantage aux
      golds@15, qui a des CC et qui peut farm les drakes")**, poids
      arbitraires (comme `DRAFT_NOTABLE_COUNTER_DELTA`), pas de test
      statistique dessus :
      - "Meilleure synergie" : synergie 100 %.
      - "Scaling / fin de partie" : synergie 30 % / scaling 38,5 % / CC 14 %
        / gold 7 % / drakes 10,5 %.
      - "Avantage early / lane" : synergie 30 % / gold 31,5 % / CC 17,5 % /
        drakes 21 % / scaling 0 %.
      - "Contrôle des objectifs" : synergie 30 % / drakes 31,5 % / CC 24,5 %
        / gold 7 % / scaling 7 %.
      Seuils "notable" des conseils de jeu recalibrés à l'échelle DUO (pas
      trio) et vérifiés contre la vraie distribution prod (`score_duo`
      fiable, n=25 173) : scaling ±0,03 (~0,8 écart-type), CC ≥ 40 (~0,4
      écart-type au-dessus de la moyenne), gold@15 ±350 (~0,8 écart-type) —
      mêmes ordres de grandeur relatifs que les seuils v1, juste réduits
      pour refléter 2 champions plutôt que 3.
      **v3 (2026-07-25, 4e retour utilisateur)** : "un champion d'un duo
      fait forcément partie d'un autre duo" — les poids ne pilotaient QUE le
      choix du duo de départ, l'extension gloutonne restait ensuite par
      synergie pure quel que soit le profil. Corrigé : `synergy` devient un
      axe pondéré parmi les autres (100 % pour "Meilleure synergie", 30 %
      pour les 3 profils pondérés — le reste réparti sur scaling/CC/gold/
      drakes, poids d'origine × 0,7 — pour ne pas sacrifier la synergie pure
      au profit du profil), et le MÊME score composite (Σ poids × z-score,
      moyenné sur les nouvelles paires formées à chaque étape) sert à choisir
      le duo de départ ET chaque champion ajouté ensuite — plus seulement le
      premier. Le total "Synergie totale" affiché reste la vraie Σ synergie
      sur les 10 paires (calcul inchangé) ; seul le critère de sélection a
      changé. Vérifié sur données réelles (16.14+16.13) : "Scaling" diverge
      maintenant de "Meilleure synergie" au niveau MID/SUP (Katarina/Nami →
      Syndra/Milio) sur le même duo de départ, preuve que l'archétype
      pèse bien sur toute la composition et pas seulement le seed.
      Chaque composition propose un lien pour se recharger dans le
      simulateur (réutilise le schéma d'URL `blue_*` déjà existant).
      Coût mesuré (réseau local → Supabase, donc plutôt une borne haute) :
      ~3-40s selon le profil pour explorer jusqu'à 8 duos de départ —
      variabilité surtout réseau (latence par requête), pas algorithmique
      (le tout premier duo essayé réussit presque toujours). Toujours
      déclenché par un bouton explicite (`?suggest=1`), jamais au
      chargement de page ; ne persiste pas dans l'état URL du simulateur.
      **Seuil de fiabilité relevé le jour même (3e retour utilisateur)** :
      `DRAFT_SUGGEST_MIN_TIER` passé de "moyen" (games_eff ≥ 50) à "eleve"
      (games_eff ≥ 400) — un duo a bien plus de volume qu'un trio, on peut
      se permettre d'être exigeant. Vérifié avant de changer : 5867 duos
      "eleve" sur la fenêtre courante, 325 à 1040 par paire de rôles
      (aucune des 10 sous-alimentée). Bénéfice inattendu : aussi bien plus
      rapide (moins de lignes à sommer par requête), ~6s mesurées pour les
      4 archétypes au lieu de ~40s avec le seuil "moyen".
      **v4 (2026-07-25, 5e retour utilisateur)** : "est-ce possible de ne
      pas se baser sur un duo de départ" — un ancrage réel reste nécessaire
      (aucune donnée n'existe pour 5 champions précis à la fois, même
      logique que l'abandon des counters trio, cf. CLAUDE.md), mais 2
      corrections rapprochent du principe :
      1. `_propose_drafts` ne s'arrête plus au premier duo de départ qui
         complète : les `DRAFT_SUGGEST_SEED_SHORTLIST` (8) candidats sont
         tous complétés, puis comparés sur un nouveau score
         (`_full_draft_score`) calculé sur les 10 VRAIES paires du draft
         fini (pas juste les moyennes partielles vues pendant la
         construction) — la composition FINIE au meilleur score archétype
         est retenue, pas celle du duo le mieux classé isolément.
      2. **Bug préexistant découvert en marge** : le pool de duos candidats
         pour le choix du duo de départ était silencieusement plafonné à 50
         lignes (`duo_tierlist` pagine par défaut, `PER_PAGE`), toujours les
         50 meilleures en synergie BRUTE — les archétypes non-synergie ne
         pouvaient donc jamais considérer un duo hors de ce top-50, même
         taillé pour leur profil. `duo_tierlist` accepte désormais un
         `per_page` optionnel ; `_propose_drafts` passe `DRAFT_SUGGEST_POOL_SIZE
         = 10 000` (marge au-dessus des ~5867 duos "eleve" actuels).
      Résultat vérifié sur données réelles (16.14+16.13) : les 4 archétypes
      donnent maintenant 4 compositions ET 4 duos de départ réellement
      différents (540/497/690/701 games), alors qu'avant ce correctif 3 des
      4 convergeaient sur la même composition que "Meilleure synergie".
      Coût mesuré : ~26-29s pour les 4 profils (vs ~6s v3) — attendu, on
      construit et compare désormais plusieurs drafts complets par profil
      au lieu de s'arrêter au premier ; jugé acceptable pour une action
      "clic et attente" explicite, pas un chargement de page.
- [x] **`/draft` : suppression du simulateur pick-par-pick, remplacé par
      "Compose à partir de tes champions" + contres (2026-07-25, retour
      utilisateur — "il faut qu'on enlève la partie draft avec la sélection
      des champions... possibilité de voir les contres d'une composition et
      de compléter une draft à partir de champions choisis")** :
      - **Suppression** : `_draft_role_grid`/`_first_empty_slot`, l'état
        `blue_*`/`red_*`/`active`/`bans` dans l'URL, `role_worst_matchups`/
        `champion_role_baseline_list` (queries.py, devenues mortes),
        `DRAFT_SLOT_ORDER`/`DRAFT_RECOMMENDED_COUNT`/`DRAFT_MIN_GAMES_EFF`/
        `DRAFT_SAFETY_MIN_GAMES_EFF` et tout le CSS/template associés.
      - **`_greedy_complete_draft` généralisé** : accepte désormais un état
        de départ `placed` PARTIEL (1 à 4 rôles déjà posés, pas seulement un
        duo) + `total` — le corps de la boucle gloutonne ne change pas,
        réutilisé tel quel par les 2 sections de la page.
      - **`_full_draft_score` refactorée** : extrait `_full_draft_stat_averages`
        (moyenne d'une liste de colonnes sur les 10 vraies paires d'un draft
        complet), réutilisée aussi bien pour classer les candidats que pour
        les conseils de jeu — les conseils portent maintenant sur la moyenne
        du DRAFT COMPLET, pas seulement le duo de départ (plus honnête,
        surtout quand le "départ" est 1 à 4 champions choisis à la main).
      - **Contres (`_draft_counters`)** : toujours du 1v1 par rôle
        (`score_matchup`, jamais un contre de la draft entière — combinatoire
        intraitable, cf. Phase 4 ❌ ci-dessus). Rôle le plus exploitable
        (meilleur delta disponible) = contre PRIMAIRE, jusqu'à
        `DRAFT_COUNTER_PRIMARY_PICKS` (3) champions ; les
        `DRAFT_COUNTER_SECONDARY_ROLES` (2) rôles suivants : 1 champion
        chacun (retour utilisateur : "les deux combinés c'est possible ?").
        Seuils : `games_eff ≥ 50` (même plancher que l'ancienne sécurité
        blind pick) + delta ≥ 3 pts de WR — vérifié sur données réelles
        (16.14+16.13, platform=all) : 945 lignes score_matchup passent les 2
        filtres, largement assez pour ne jamais starver un rôle.
      - **"Compose à partir de tes champions"** (`_seed_from_champions`) :
        1 à 5 champions choisis à la main (état dans l'URL, `seed_top`..
        `seed_sup` + `archetype`) → même algorithme que les compositions
        auto-suggérées. Fiabilité de CHAQUE paire déjà choisie affichée
        honnêtement (y compris "aucune donnée ensemble" si jamais jouée
        ensemble) — jamais bloquant (retour utilisateur), contrairement aux
        rôles que le système complète ensuite, qui restent filtrés
        `DRAFT_SUGGEST_MIN_TIER = "eleve"`. `pool`/`zstats` (coûteux, ~10 000
        lignes) calculés au plus une fois par requête, partagés entre les 2
        sections si demandées ensemble.
      Vérifié en local sur données réelles : composition à partir d'1 seul
      champion (Malphite top, profil "objectifs") complétée en ~8s (pas de
      recherche de duo de départ à faire, un seul candidat à construire) ;
      `/draft?suggest=1` (4 profils + leurs contres) ~47s — plus lent que
      les ~26-29s de la v4 (contres + conseils sur le draft complet ajoutent
      ~15 requêtes par carte finale), jugé acceptable pour une action
      "clic et attente" explicite.
- [x] **Contres/design retravaillés + compositions PRÉCALCULÉES (2026-07-25/26,
      2 retours utilisateur : "je voulais garder les drafts proposées sans
      avoir à cliquer" + "le contre n'est pas très compréhensible / améliore
      le design de Compose")** :
      - **Contres reformulés** : chaque contre affiche désormais explicitement
        le champion CONTRÉ ("contre {champion} : {contres}"), pas juste le
        nom du rôle — l'ancienne formulation ("ADC est le rôle le plus
        exploitable : Katarina...") ne disait pas contre QUI. Layout en
        "chips" (rôle + champion + delta), plus lisible qu'un paragraphe.
      - **Design de "Compose à partir de tes champions"** retravaillé :
        badges de rôle colorés (mêmes couleurs que le reste du site) à côté
        de chaque champ de saisie, formulaire réorganisé, barre de
        chargement (CSS + `htmx-request`, hx-boost déjà actif sur `<body>`)
        pendant le calcul — vérifié visuellement en navigateur (serveur
        `uvicorn` local), la barre s'affiche bien pendant la requête AJAX.
      - **Compositions suggérées PRÉCALCULÉES pour `platform="all"`**
        (la région par défaut à l'arrivée sur `/draft`, le plus de games) :
        nouvelles tables `draft_suggestion`/`draft_suggestion_counter`
        (migration 033, style normalisé cohérent avec le reste du schéma,
        pas de JSON) ; le moteur de calcul entier (archétypes, algorithme
        glouton, contres 1v1 — `_sum_synergy`, `_greedy_complete_draft`,
        `_full_draft_score`, `_draft_counters`, etc.) déménage de
        `web/app.py` vers un nouveau module autonome
        `synergy/draft_suggestions.py` (aucune dépendance à `trio_lab.web`,
        y compris ses propres requêtes SQL déjà écrites côté
        `web/queries.py` — dupliquées à dessein pour que le module reste
        importable par le collector sans jamais tirer FastAPI). Rafraîchi
        dans `collector/service.py` → `refresh_scores`, juste après
        `resilience.refresh` — même pipeline que score_duo/score_trio/
        score_matchup/résilience, pas de cron séparé, cadencé par les
        cycles de collecte (rate limit Riot, déjà plusieurs minutes).
        Seule "toutes régions" est précalculée (décision explicite, retour
        utilisateur : chaque région ajouterait ~30-47s à CHAQUE cycle de
        collecte) — les autres régions gardent le bouton "Proposer des
        compositions" en calcul à la demande, comme avant ; si rien n'est
        encore matérialisé pour "toutes régions" (ex. juste après un
        déploiement, avant le 1er cycle), retombe aussi sur le bouton
        plutôt qu'une page cassée/vide.
      - `web/app.py` devient un pur adaptateur de rendu (`_build_draft_result`,
        `_resolve_seed_pairs`, `_resolve_counters`) : résout les
        `champion_id` bruts (calculés en direct OU lus depuis
        `draft_suggestion(_counter)`) en `Champion`/libellés — un seul
        chemin de rendu pour les 2 sources et les 2 sections de la page.
      Vérifié : `synergy/draft_suggestions.refresh` matérialise la bonne
      composition + le bon contre sur un scénario déterministe (tests
      `tests/synergy/test_draft_suggestions_pg.py`) ; `/draft?platform=all`
      affiche les compositions précalculées SANS clic, bouton absent
      (`tests/web/test_app_pg.py`) ; repli sur le bouton vérifié si rien
      n'est encore matérialisé.
- [x] **3 retours utilisateur sur les contres/compose (2026-07-26)** :
      1. **Contres ambigus** : "BOT contre Draven : Mel +6.1 %..." ne disait
         pas si la composition était FORTE ou FAIBLE face à ces champions.
         Reformulé sans ambiguïté — "{champion de la composition} peut être
         puni(e) par : {contres}" — et la couleur passe de vert (`pos`) à
         rouge (`neg`) : ce sont des points FAIBLES (des picks adverses qui
         punissent la composition), pas des atouts. Titre de section et
         paragraphe d'intro alignés ("points faibles" explicite).
      2. **"Compose à partir de tes champions" sans archétype imposé** :
         l'archétype devient optionnel — laissé vide, une proposition par
         archétype est calculée à partir des MÊMES champions choisis à la
         main (même logique indépendante-par-archétype que "Compositions
         suggérées" : un archétype qui échoue n'apparaît juste pas). Whether
         "au moins 1 champion ou 1 archétype" détecte qu'un formulaire a été
         soumis (page fraîche : ni l'un ni l'autre dans l'URL).
      3. **`<select>` archétype illisible** (fond blanc, texte blanc) : bug
         de style pur — il manquait la règle `select option { background:
         var(--card); ... }` déjà utilisée ailleurs sur le site
         (`.filters select option`, jamais reproduite sur ce nouveau
         formulaire). Corrigé en copiant le même pattern.
      Vérifié en navigateur (serveur `uvicorn` local sur les vraies données
      prod) : contres affichés en rouge avec la formulation "peut être
      puni(e) par" ; compose sans archétype produit bien 4 cartes (une par
      archétype) à partir des 2 mêmes champions de départ.
- [x] **Points forts + poids des archétypes affichés (2026-07-26, retour
      utilisateur : "en plus du contre, contre qui la composition est
      forte ?" + "afficher le poids des métriques par archétype")** :
      - **`draft_strengths`** : symétrique de `draft_counters`, matchup 1v1
        inversé (`_matchup_beats`, `champ_a` = notre champion au lieu de
        `champ_b`) — mêmes seuils de fiabilité/notabilité, même format
        primaire/secondaire. Les 2 fonctions partagent désormais leur
        logique de classement (`_rank_matchup_picks`), extraite pour ne pas
        dupliquer le code. Migration 034 : `draft_suggestion_counter` gagne
        une colonne `direction` ('weakness'/'strength', même schéma pour
        les 2, PK élargie) — table entièrement recalculée à chaque
        `refresh`, pas d'historique à migrer.
      - **Poids d'archétype affichés** : chaque carte montre désormais ses
        poids ("Synergie 30 % · Scaling 38 % · CC 14 % · Gold@15 7 % ·
        Drakes 11 %") — lus directement depuis `ARCHETYPES[archetype]
        ["weights"]` (déjà en mémoire, aucun nouveau calcul), axes à poids
        nul omis.
      - Carte : "points forts" (vert, "{champion} domine : ...") affiché
        avant "points faibles" (rouge) — message combiné si aucun des deux
        n'a de signal notable, jamais une section vide sans explication.
      Vérifié : `synergy/draft_suggestions.refresh` écrit bien les 2
      directions dans `draft_suggestion_counter` sur un scénario
      déterministe (`tests/synergy/test_draft_suggestions_pg.py`) ; carte
      web affiche "Points forts"/"domine" + les poids
      (`tests/web/test_app_pg.py`).
- [x] **Winrate + intervalle de confiance affichés (2026-07-27, retour
      utilisateur : "en plus du taux de synergie, le winrate avec son IC ?")** :
      `score_duo.wr`/`ci_low`/`ci_high` (intervalle de Wilson, déjà calculés
      par paire) moyennés sur les 10 vraies paires de la composition —
      même mécanisme que `advice_stats` (scaling/cc/gold) : moyenne simple,
      pas une combinaison statistique rigoureuse des IC, cohérent avec le
      reste des stats affichées sur ces cartes. `DISPLAY_STAT_COLUMNS`
      (nouvelle constante) regroupe les 6 colonnes moyennées (scaling/cc/
      gold + wr/ci_low/ci_high) pour un seul aller-retour sur les 10 paires,
      réutilisée par le calcul en direct ET `refresh`. Migration 035 :
      `draft_suggestion` gagne `wr`/`wr_ci_low`/`wr_ci_high` (déjà appliquée
      en prod). Affiché juste sous "Synergie totale" : "Winrate estimé :
      55.0 % [0.0 % – 5.1 %]".
      Vérifié avec des valeurs exactes calculées à la main (wr uniforme sur
      les 10 paires, ci_high = moyenne des synergies) : `tests/web/
      test_app_pg.py` (rendu HTML) et `tests/synergy/
      test_draft_suggestions_pg.py` (colonnes `draft_suggestion`).
- [x] **`refine_draft` : un passage de remplacement post-construction
      (2026-07-27, retour utilisateur : "est-ce que le système essaie de
      remplacer le duo de base par un autre pour voir s'il n'y a pas une
      meilleure option ?")** : le glouton ne revient jamais en arrière — un
      champion posé tôt (le duo de départ compris) peut ne plus être
      optimal une fois les autres connus. Après une composition complète à
      5, UN SEUL passage (pas itéré jusqu'à convergence, gain marginal jugé
      faible face au coût — discussion utilisateur) : pour chaque rôle NON
      verrouillé, cherche le meilleur candidat compte tenu des 4 AUTRES
      champions actuels et remplace SEULEMENT si strictement mieux sur le
      score composé de l'archétype. Les remplacements s'enchaînent dans le
      même passage (un rôle remplacé devient un ancrage à jour pour les
      rôles suivants).
      - `_combined_score` extraite (partagée avec `greedy_complete_draft`,
        avant dupliquée).
      - Garde-fou `_all_pairs_reliable` : un remplacement en cascade peut en
        théorie laisser un rôle NON remplacé désaccordé avec un
        remplacement survenu après lui dans le même passage (jamais
        revérifié) — revalidation des 10 vraies paires en fin de passage,
        repli sur la composition D'ORIGINE si une paire n'est plus fiable
        (jamais un résultat dégradé silencieusement).
      - **`propose_drafts`** (compositions suggérées) : rien n'est
        verrouillé, le duo de départ lui-même est éligible au remplacement
        — si l'un de ses 2 rôles change, `seed_pairs` est recalculé sur
        l'état FINAL (jamais l'ancien duo affiché comme "de départ" alors
        qu'il ne l'est plus).
      - **`_manual_propose`** ("Compose à partir de tes champions") :
        `locked_roles = seed_picks` — les champions choisis à la main par
        l'utilisateur ne sont JAMAIS remplacés, seuls les rôles complétés
        par le système sont éligibles.
      Vérifié : un scénario où un meilleur candidat existe (`tests/synergy/
      test_draft_suggestions_pg.py`) confirme le remplacement ET l'ajustement
      exact du total de synergie ; un 2e test confirme qu'un rôle verrouillé
      n'est jamais touché même quand un meilleur candidat existe. Coût
      mesuré sur données réelles (16.14+16.13, région non précalculée) :
      ~17s pour les 4 profils — dans le même ordre de grandeur qu'avant
      (~26-47s), pas de régression notable malgré le passage
      supplémentaire.
- [x] **`/draft` épuré (2026-07-27, retour utilisateur : "beaucoup trop de
      phrases et de mots" + sélecteur d'archétype trop collé aux champs de
      rôle)** : suppression des 3 paragraphes d'intro (page, section
      "Compose", section "Compositions suggérées"), titres de bloc
      raccourcis ("Points forts de cette composition (rôle le plus
      dominant...)" → "Points forts"), libellés de ligne compressés
      ("Synergie totale"/"Winrate estimé" → une seule ligne "Synergie : X %
      · Winrate : Y %"), duo de départ sans préfixe de rôle ni mot
      "fiabilité"/"games" ("Jungle/Mid : Lee Sin + Ahri — +30.0 %, fiabilité
      eleve (60 games)" → "Lee Sin + Ahri — +30.0 %, eleve (60)"). CSS :
      `.draft-compose-form` n'avait pas son propre `display:flex` — la grille
      de champs de rôle et le sélecteur d'archétype se retrouvaient sans
      espace entre eux ; ajout de `gap`. Le wrapper `<label
      class="draft-compose-archetype">` autour du `<select>` a été retiré du
      template (sélecteur direct dans `.draft-compose-submit`) — les règles
      CSS du thème sombre du dropdown (fix précédent contre le popup blanc
      sur blanc) migrées vers `.draft-compose-submit select`/`select option`
      pour ne pas régresser. Vérifié visuellement (serveur local + Chrome) :
      espacement correct, dropdown toujours lisible en thème sombre. Tous les
      textes exacts asserted dans `tests/web/test_app_pg.py` mis à jour en
      conséquence (334 tests passent).

- [x] **Cartes `/draft` alignées entre elles (2026-07-27, retour utilisateur :
      "manque de structure cohérente, les données d'une card à une autre
      devraient être alignées")** : CSS Grid + `subgrid` sur `.draft-suggest-
      grid`/`.draft-suggest-card` — 8 lignes de grille partagées entre toutes
      les cartes d'une même rangée (titre, poids, membres, synergie/winrate,
      duo de départ, conseils, points forts, points faibles), chaque section
      épinglée à une ligne fixe via `grid-row` explicite sur sa classe. Une
      section absente sur une carte (ex. pas de conseils) laisse juste un
      trou dans SA colonne — les sections suivantes des AUTRES cartes restent
      alignées sur la même hauteur. `matchup_block` prend un paramètre
      `extra_class` pour distinguer points forts (ligne 7) de points faibles
      (ligne 8). Vérifié visuellement (serveur local + Chrome, 4 archétypes
      côte à côte) : titres, listes de champions et sections de contres
      démarrent tous à la même hauteur d'une carte à l'autre.

- [x] **Duo de départ masqué sur "Compositions suggérées" (2026-07-27, retour
      utilisateur : "est-ce que cette donnée a un intérêt ? Locke + Draven —
      +5.1 %, eleve (902)")** : ce duo n'est qu'un détail interne de
      l'algorithme (le point de départ du glouton, éligible au remplacement
      par `refine_draft`) — sa synergie isolée à côté du total du draft
      prêtait à confusion sans rien apporter. Masqué uniquement sur
      "Compositions suggérées" (`_build_draft_result(...,
      include_seed_pairs=False)`) ; conservé sur "Compose à partir de tes
      champions" où ce sont les champions CHOISIS par l'utilisateur — savoir
      s'ils synergisent déjà et sur combien de games reste une vraie
      question. La preuve que 2 archétypes choisissent bien 2 duos de départ
      différents (motif de `refine_draft`) déplacée d'un test HTTP
      (scraping du texte affiché) vers un test direct de `propose_drafts`
      (`test_propose_drafts_uses_different_seed_duo_per_archetype`), plus
      approprié maintenant que ce détail n'est plus rendu.

- [x] **Poids des archétypes revus (2026-07-27, retour utilisateur après
      relecture des poids affichés)** : `drakes` (taux de dragons pris,
      calculé sur TOUTE la partie — `stats/aggregate.py`, sans coupure
      temporelle) pesait presque autant dans "Avantage early / lane" (21 %)
      que dans "Contrôle des objectifs" (31.5 %), diluant la distinction
      entre les 2 profils sans être un signal vraiment précoce.
      - **"Avantage early / lane"** : `drakes` abaissé 0.21 → 0.07 (niveau
        "axe secondaire", comme les axes non-identitaires des autres
        profils), le delta (0.14) reporté sur `gold` (`gold_diff_15`),
        l'axe qui EST l'identité de ce profil (0.315 → 0.455).
      - **"Scaling / fin de partie"** : `drakes` remplacé par `soul_rate`
        (`score_duo.soul_rate`, taux d'obtention de l'âme — 4 drakes
        non-elder cumulés, dérivé du COMPTE de drakes, jamais de l'event
        `DRAGON_SOUL_GIVEN` qui n'est qu'une annonce et pas l'obtention,
        cf. mémoire `riot-timeline-quirks` et le commentaire dans
        `stats/extract.py`) — signal propre de fermeture de game longue,
        plus cohérent avec l'identité "fin de partie" qu'un taux d'intake
        brut par minute. Poids repris tel quel (0.105), substitué sans
        nouveau calibrage. Déjà agrégé jusqu'à `score_duo` (migrations
        007/008, déjà affiché sur `/duos`) — aucune migration ni nouveau
        pipeline nécessaire, juste ajouté à `ARCHETYPE_STAT_COLUMNS`/
        `_STAT_COLUMNS_SQL`.
      - "Contrôle des objectifs" inchangé (`drakes` reste son identité,
        31.5 %, cohérent).
      Vérifié sur données réelles (16.14+16.13, euw1, calcul en direct) :
      les 2 profils complètent toujours, poids affichés corrects ("Scaling"
      → "Synergie 30 % · Scaling 38 % · CC 14 % · Gold@15 7 % · Âme 10 %",
      "Early" → "Synergie 30 % · CC 18 % · Gold@15 46 % · Drakes 7 %").
      Les compositions PRÉCALCULÉES (`platform=all`, table
      `draft_suggestion`) reprennent les nouveaux poids au prochain cycle
      `refresh()` du collector, automatique — pas d'action manuelle requise
      (même mécanisme que les révisions de poids précédentes).

- [x] **Autocomplétion des champions au lieu de la liste complète au clic
      (2026-07-27, retour utilisateur : "ça ouvre un grand menu inutile, je
      veux juste la complétion quand l'utilisateur commence à écrire")** :
      `<input list="champion-names">` (`/draft`, `/duos`, `/tierlist`)
      partage un seul `<datalist>` rendu côté serveur avec les ~170
      champions — au focus natif du navigateur, ça affichait la liste
      entière. Nouveau `static/champion-autocomplete.js` : vide le datalist
      au premier focus (liste complète mémorisée sur l'élément via
      `_allOptions`, pour la retrouver après vidage) puis le repeuple
      seulement avec les correspondances (sous-chaîne, 20 max) au fil de la
      frappe. Délégué sur `document` (`focus` en capture, `input` en bulle)
      plutôt que bindé aux inputs directement — même raison que
      sort.js/thresholds.js : htmx (`hx-boost`) remplace le DOM à chaque
      navigation, un binding pris avant un swap ne survivrait pas. Aucun
      framework de test JS dans ce repo (sort.js/thresholds.js n'en ont pas
      non plus) : vérifié manuellement (serveur local + Chrome, clic réel
      via CDP — un `.focus()` déclenché en JS pur ne génère pas d'event
      `focus` fiable dans ce contexte d'automatisation, piège découvert en
      testant) sur `/draft` et `/duos` : datalist vide au clic, repeuplé en
      tapant, re-vidé en changeant de champ vide.

- [x] **Jusqu'à 3 propositions par archétype, boutons 1/2/3 (2026-07-27,
      retour utilisateur : "des boutons 1,2,3... 3 propositions par
      archétype" + "une avec fiabilité très élevée, plus de games que les
      autres")** : `propose_drafts` gardait déjà jusqu'à `SEED_SHORTLIST`
      (8) compositions complètes calculées par archétype, n'en gardait que
      la meilleure — les 7 autres étaient jetées. Sélection étendue, sans
      calcul supplémentaire côté "essayer des seeds" :
      - **Rang 0** : meilleur score (comportement inchangé).
      - **Rang 1** : 2e meilleur score suffisamment DIFFÉRENT du rang 0
        (`_is_diverse_enough`, au plus `DIVERSITY_MAX_SHARED_CHAMPIONS`=3
        champions communs sur 5, peu importe le rôle) — des seeds
        différents convergent souvent vers la même fin de complétion
        gloutonne, un pur tri par score aurait sinon produit des
        quasi-doublons.
      - **Rang 2** : parmi les candidats restants encore diversifiés vis-à-
        vis des rangs 0/1, celui dont le duo de départ a le plus de
        `games_eff` (pas le score) — "la plus fiable". Tous les candidats
        passent déjà `MIN_TIER` ("eleve") par construction ; ce rang
        privilégie le VOLUME de données plutôt que le score.
      - Jamais forcé à 3 : si la diversité manque, moins de propositions
        (`test_propose_drafts_never_forces_three_variants`).
      - Chaque rang retenu reçoit SON PROPRE passage `refine_draft` ; si 2
        rangs convergent vers les mêmes 5 champions après raffinement, le
        doublon est supprimé (jamais affiché 2 fois).
      - Migration 036 : `suggestion_rank`/`selection`
        ("score"|"diverse"|"reliable") ajoutés à la clé de
        `draft_suggestion`/`draft_suggestion_counter` — chaque proposition
        a ses PROPRES contres/points forts, pas partagés entre rangs.
      - Web : `_group_draft_variants` regroupe la liste plate par
        archétype ; template `draft_group`/`draft_variant_body` (CSS
        `display: contents` sur le wrapper de variante — les enfants
        restent des enfants directs de la grille `subgrid` de la carte,
        l'alignement entre cartes tient même en changeant d'onglet) ;
        nouveau `static/draft-variant-tabs.js` (délégué sur `document`,
        même raison que sort.js/thresholds.js) bascule l'onglet actif sans
        aller-retour serveur — les 3 variantes sont déjà toutes rendues
        côté serveur, seule leur visibilité change.
      - Scope volontairement limité à "Compositions suggérées" — "Compose à
        partir de tes champions" garde 1 seule proposition par archétype
        (l'utilisateur a déjà fixé son point de départ).
      Coût mesuré sur données réelles (16.14+16.13, `platform=all`) :
      `propose_drafts` ~18s pour 11 compositions (4 archétypes, 1 sans
      diversité suffisante) contre ~17s pour 4 compositions avant ce
      changement — l'essentiel du coût était déjà dans l'essai des 8 seeds,
      pas dans le nombre de résultats gardés. `refresh()` (avec écriture) :
      9,2s, 104 lignes de contres écrites. Migration 036 appliquée en prod
      le 2026-07-27, `refresh()` relancé manuellement pour matérialiser les
      nouvelles propositions immédiatement (sinon prochain cycle collector).
- [x] **Fiabilité du rang 2 jugée sur les 10 paires, pas seulement le duo de
      départ (2026-07-28, retour utilisateur : "pourquoi pas tous les duos
      plutôt que juste le premier ?")** : le duo de départ n'est qu'un
      artefact de l'algorithme (jamais montré à l'utilisateur depuis le
      retour du 26-07) — une composition peut avoir un duo de départ très
      joué mais se compléter avec des paires plus rares, rendant l'ancien
      critère trompeur. Nouvelle fonction `_min_games_eff` : `games_eff`
      MINIMUM sur les 10 vraies paires (pas la moyenne — une composition
      n'est fiable que si CHACUNE de ses paires l'est, une seule paire peu
      jouée ne doit pas être masquée par 9 autres solides), calculé
      uniquement pour les candidats déjà éligibles par diversité (pas les 8
      candidats bruts). Test dédié construit exprès pour départager
      l'ancien critère du nouveau (composition C : duo de départ énorme
      mais 1 paire quasi jamais jouée → aurait gagné avant ; composition D :
      duo de départ modeste mais toutes les autres paires solides → gagne
      maintenant). Coût mesuré sur données réelles : `propose_drafts`
      passe de ~18s à ~20s, `refresh()` de 9,2s à 15,4s — accepté (le
      collector tourne en tâche de fond, pas sur une requête utilisateur).

- [x] **Indicateur "la plus fiable" visible SANS cliquer (2026-07-28, retour
      utilisateur)** : la mention "· la plus fiable" n'apparaissait qu'après
      avoir cliqué sur l'onglet correspondant, dans le corps de la carte.
      `data-selection="{{ v.selection }}"` ajouté sur chaque bouton d'onglet
      + petit point `var(--pos)` (déjà le vocabulaire "bon signal" du reste
      du site) en CSS via `[data-selection="reliable"]::after`, visible
      d'emblée sur le bouton concerné ; `title="La plus fiable"` en plus
      pour le survol. Test web étendu à 3 pentades (au lieu de 2) pour
      couvrir concrètement le rang "fiable".

- [x] **"Personnalise tes poids" — 5e archétype à poids libres (2026-07-28,
      retour utilisateur : "que l'utilisateur décide lui-même des poids
      afin d'avoir un archétype custom" + "un 5ème archétype fixe selon les
      poids de l'utilisateur")** :
      - `synergy.draft_suggestions.propose_for_weights` (ex-`_propose_for_
        weights`, rendue publique) : logique par-archétype extraite de
        `propose_drafts` (seeds/diversité/fiabilité, cf. entrées
        précédentes) — `propose_drafts` devient une simple boucle sur les 4
        archétypes fixes appelant cette fonction, réutilisée telle quelle
        pour un 5e jeu de poids.
      - 6 champs (Synergie/Scaling/CC/Gold@15/Drakes/Âme) — validation
        stricte (`_parse_custom_weights`) : la somme doit faire 100 % (±0.5
        pour l'arrondi), jamais une répartition automatique silencieuse ;
        poids négatifs refusés ; page fraîche (aucun champ rempli) → ni
        carte ni erreur, pas un cas d'échec.
      - Section dédiée "Personnalise tes poids", sous "Compositions
        suggérées" — la carte générée a sa PROPRE place (pas mélangée à la
        grille des 4 archétypes fixes, jamais 5 cartes sur une ligne).
      - TOUJOURS en direct, y compris sur la région par défaut où les 4
        autres sont précalculées : un poids personnalisé ne peut jamais
        être matérialisé à l'avance par le collector (il ne connaît pas les
        poids d'un futur visiteur). Coût mesuré sur données réelles :
        ~4,2s pour un jeu de poids (3 variantes, mêmes boutons 1/2/3 que
        les archétypes fixes, réutilisation directe de `draft_group`).
      - `weights` ajouté au dict brut retourné par `propose_for_weights`/
        `_manual_propose` : `_archetype_weights_display` (web/app.py) prend
        désormais un dict de poids directement plutôt qu'une clé
        d'archétype à chercher dans `ARCHETYPES` (repli sur `ARCHETYPES`
        pour les lignes précalculées, qui ne stockent que des archétypes
        fixes de toute façon).

- [x] **Barre de chargement + poids personnalisés dans "Compose à partir de
      tes champions" (2026-07-28, retour utilisateur)** :
      - `.draft-custom-loadbar`, même mécanisme que `.draft-compose-loadbar`
        (htmx `hx-boost` déjà body-wide, la classe `.htmx-request` s'ajoute
        automatiquement au formulaire pendant la requête — aucun JS
        supplémentaire nécessaire).
      - Option "Personnalisé (poids ci-dessous)" ajoutée au `<select>`
        archétype de "Compose à partir de tes champions" — les 2 formulaires
        (celui-là et "Personnalise tes poids") partagent les mêmes champs
        `w_<axe>` via des inputs cachés à valeur "sticky" (même principe
        déjà établi que `seed_*` porté par le formulaire de filtres tout en
        haut). `_manual_propose` prend désormais `label`/`weights`
        directement (comme `propose_for_weights`) plutôt que de chercher
        `archetype_key` dans `ARCHETYPES` — "Personnalisé" choisi sans poids
        renseignés → message explicite plutôt qu'un plantage.
      - **Bug corrigé en écrivant les tests** : les champs `w_<axe>` non
        soumis rendaient `value="None"` (le `None` Python brut interpolé
        tel quel dans le HTML) plutôt que vide — invisible à l'œil nu car
        un `<input type="number">` invalide s'affiche vide dans un
        navigateur (masque le bug), mais détecté par une assertion de test
        sur le HTML brut. Corrigé en filtrant `v or ""` avant de passer les
        valeurs au template.

- [x] **Poids personnalisés de "Compose à partir de tes champions" rendus
      indépendants de "Personnalise tes poids" (2026-07-28, retour
      utilisateur : "pourquoi il devrait partager les mêmes poids
      personnalisés ?")** : rien ne justifiait que le 5e archétype
      auto-suggéré et une composition bâtie à partir de SES champions soient
      forcés au même réglage — 2 usages différents (l'un part de zéro, l'un
      part de champions déjà choisis), 2 formulaires, désormais 2 états
      indépendants :
      - Champs `cw_<axe>` (au lieu de `w_<axe>` partagés) — parsés par la
        même fonction `_parse_custom_weights` (déjà générique), juste
        appelée 2 fois avec 2 dicts bruts différents.
      - Les 6 champs de poids sont désormais affichés DIRECTEMENT dans
        "Compose à partir de tes champions" (à côté du `<select>`
        archétype), révélés seulement quand "Personnalisé" est choisi —
        résout aussi la confusion du libellé "Personnalisé (poids
        ci-dessous)" (retour utilisateur, "je comprends pas trop le poids
        ci-dessous") qui renvoyait vers une section distante de la page.
        État initial correct côté serveur (`hidden` posé selon
        `selected_archetype`), nouveau `static/draft-custom-archetype.js`
        gère seulement la bascule EN DIRECT pendant que l'utilisateur
        change de sélection (même pattern délégué que
        sort.js/thresholds.js).
      - **Bug trouvé pendant la vérification visuelle** (pas par les tests,
        qui ne rendent jamais le CSS) : `.draft-compose-weights { display:
        grid; ... }` (règle auteur) l'emportait sur la règle `[hidden] {
        display: none }` du navigateur (origine user-agent, toujours
        perdante face à l'auteur à spécificité égale) — les champs
        restaient visibles même avec l'attribut `hidden` posé. Même piège
        déjà rencontré et corrigé pour `.draft-suggest-body` (boutons
        1/2/3) ; oublié cette fois-ci. Corrigé avec `.draft-compose-weights
        [hidden] { display: none; }`.
      - **Faux positif de test découvert en écrivant les tests** : un
        premier test soumettait par erreur `w_synergy` (au lieu de
        `cw_synergy`) et passait quand même — non pas parce que "Compose à
        partir de tes champions" fonctionnait, mais parce que le 5e
        archétype auto-suggéré de "Personnalise tes poids" produisait
        accidentellement la même carte "Personnalisé" ailleurs sur la page.
        Corrigé en soumettant strictement `cw_*` et en vérifiant l'absence
        de `draft-custom-result` dans la réponse.

- [x] **Archétype "Poke / zone" — score de portée théorique par champion
      (2026-07-28, retour utilisateur : "il manque un archétype poke avec de
      la range")** : nouveau module `rangeref/` (mirroir de `ccref/` en plus
      simple), table `champion_range_theoretical` (migration 037,
      1 colonne, jamais de mélange empirique — la Timeline API n'expose
      aucune position de sort lancé, vérifié). Peuplée depuis Data Dragon
      (`python -m trio_lab.rangeref.sync`, aucun scraping wiki récurrent) et
      matérialisée dans `score_duo`/`score_trio.range_theoretical_pct`
      (`synergy/compute.py`, même pipeline que le CC théorique). 5e colonne
      `ARCHETYPE_STAT_COLUMNS`/nouvel archétype `ARCHETYPES["range"]` dans
      `draft_suggestions.py`.
      - La formule a traversé **6 relectures successives** le même jour, le
        détail complet (raisonnement, valeurs de wiki vérifiées, cas
        limites) vit dans le docstring de `rangeref/score.py` plutôt que
        dupliqué ici — résumé des grandes étapes :
        1. Portée brute Q/W/E (attack_range + max) — Data Dragon expose
           `spells[].range` en JSON structuré, pas de prose à parser.
        2. **Sentinelles Data Dragon découvertes** : valeurs "illimité/self"
           (25000, 10000, 4294967295 = 2³²-1 sur Janna W) sur des sorts
           self-cast/vision/mobilité — `RANGE_SPELL_OVERRIDES`
           (champion, spell_id) → `None` (exclu) ou une vraie valeur
           wiki-vérifiée, méthode identique au scan de charge Xerath/Varus
           de la recherche préparatoire.
        3. **Ultimates réintégrés** (retour utilisateur : "ça compte comme
           de la portée et du poke") après avoir été exclus par défaut au
           départ — `GLOBAL_RANGE` (8000, initialement 15000, réduit sur
           retour "écarts énormes") pour les ultimates au vrai "Global"
           officiel (Karthus, Draven R, Ashe R...).
        4. **Cooldown intégré** (retour utilisateur : "la capacité à
           infliger des dégâts de loin ET régulièrement") — `cooldown` est
           un champ Data Dragon propre, contrairement aux dégâts (tableaux
           `effect`/`vars` sans libellé sémantique fiable — chantier de
           l'ampleur de la table CC, explicitement pas fait). Bug trouvé en
           vérifiant : plusieurs sorts réels ont un cooldown Data Dragon
           quasi nul (passifs modélisés comme un sort, effets on-hit) —
           `MIN_COOLDOWN` (4.0s, plancher générique) + correction d'un
           second bug où un cooldown ENTIÈREMENT à 0 tombait sur le mauvais
           repli (traité comme gratuit plutôt que plafonné).
        5. **Somme du kit entier plutôt que meilleur sort seul** (retour
           utilisateur, cas Samira : un seul vrai outil de poke dans un kit
           d'all-in scorait comme Vel'Koz/Ziggs/Zoe sous `max`) — chaque
           sort éligible s'additionne désormais.
        6. **Autoattaque pondérée par la vitesse d'attaque** (retour
           utilisateur : "ses auto attaques font 50 de dégâts" — Annie
           pesait 65 % de son score sur une distance jamais vraiment
           exploitée) — `attack_range × attackspeed` plutôt qu'une distance
           brute ajoutée telle quelle. `AD × AS` (DPS d'autoattaque réel)
           envisagé et rejeté : aux stats de base, un mage et un tireur ne
           se distinguent presque pas (l'écart vient de l'objet, hors
           périmètre du score).
      - **Limite assumée, pas corrigée** : les vrais dégâts par sort restent
        hors score (cooldown/portée seuls) — chantier ultérieur si besoin,
        même ampleur que la table CC.
      - Classement final vérifié sur le roster réel (top/bottom 50) :
        dominé par les mages/tireurs de poke reconnus (Jinx, Kog'Maw, Swain,
        Caitlyn, Xerath, Ziggs, Vel'Koz, Zoe...), les 25 derniers sont
        exclusivement des bruisers/tanks/assassins de mêlée (Sett, Master
        Yi, Riven, Garen, Darius...).
      - Déployé en prod le 2026-07-28 : migration 037 appliquée, sync
        initial (173 champions), refresh manuel de la fenêtre 16.14+16.13
        pour matérialisation immédiate (sinon le prochain cycle du
        collector s'en charge automatiquement, `refresh_scores` appelle
        déjà `draft_suggestions.refresh`).
      - Colonne "Portée" ajoutée à `/tierlist` et `/duos`
        (`web/queries.py`/templates), tooltip explicite sur le caractère
        100% théorique (jamais mesuré en jeu).
      - **Piège trouvé pendant le déploiement** : `compute.refresh()` fait un
        UPSERT gardé (`ON CONFLICT DO UPDATE ... WHERE score_duo.games IS
        DISTINCT FROM EXCLUDED.games`, optimisation pour éviter des écritures
        inutiles à chaque cycle du collector quand rien n'a changé). Un
        premier `python -m trio_lab.synergy --patches 16.14,16.13` a tourné
        sans erreur (1 028 283 lignes "rafraîchies" côté log) mais
        `range_theoretical_pct` restait NULL partout : la fenêtre était déjà
        matérialisée avec les mêmes `games`, donc la clause `WHERE` a
        silencieusement sauté TOUTES les mises à jour — le compte de lignes
        loggé est celui calculé en Python, pas celui réellement écrit en
        base. Corrigé par un backfill SQL direct (`UPDATE ... FROM
        champion_range_theoretical`, une seule passe, bypass l'UPSERT
        gardé). Ce piège se reproduira pour tout futur ajout de colonne sur
        une fenêtre déjà matérialisée sans nouvelles games — vérifier
        `count(colonne) FROM score_duo/score_trio` après un refresh de
        backfill, pas seulement le log de `refresh()`.
      - **Poids de l'archétype abaissé le jour même** (retour utilisateur :
        "40% ça me paraît un peu trop élevé") : `range` 0.40 → 0.34,
        delta reporté sur `cc`/`gold`/`drakes` (0.10 → 0.12 chacun) —
        `range_theoretical_pct` reste l'axe dominant mais un peu moins
        écrasant que "early" (gold à 0.455), cohérent avec le fait que
        c'est le seul axe 100% théorique du système (jamais recalé par le
        comportement réel des joueurs, contrairement aux autres). Draft
        suggestions re-matérialisées.

- [x] **Seuil de fiabilité choisi par l'utilisateur — "Compose à partir de
      tes champions" et "Personnalise tes poids" (2026-07-28, retour
      utilisateur : "choisir le niveau de fiabilité... en choisissant un
      nombre de games")** : ces 2 formulaires utilisaient un seuil FIXE
      (`MIN_TIER = "eleve"`, catégorie tier basée sur `games_eff`) — remplacé
      par `min_games`, un nombre de games RÉELLES choisi par l'utilisateur
      (même unité que le filtre `min_games` déjà présent sur `/tierlist` et
      `/duos`, plus lisible qu'un tier faible/moyen/élevé). "Compositions
      suggérées" (jamais configurable, précalculée par le collector) garde
      `MIN_GAMES_DEFAULT` (400, ex-seuil "eleve") sans changement de
      comportement.
      - Filtrage `tier = ANY(...)` → `games >= %(min_games)s` dans
        `_duo_pool`/`_best_partners` (`draft_suggestions.py`) — `min_tier:
        str` renommé `min_games: int` dans toute la chaîne
        (`_sum_synergy`, `_all_pairs_reliable`, `greedy_complete_draft`,
        `refine_draft`, `propose_for_weights`). `_TIER_AT_LEAST` (local à ce
        module — sans lien avec son homonyme de `web/queries.py`, le filtre
        de `/tierlist`, non touché) supprimé, devenu mort.
      - Champs `cw_min_games`/`w_min_games` (même préfixation que
        `cw_<axe>`/`w_<axe>`, 2 états indépendants comme le reste de ces 2
        formulaires) — `_get_pool_zstats` (web/app.py) mis en cache par
        VALEUR de seuil (dict, plus une liste 1 case) : "Compositions
        suggérées" (calcul en direct, seuil fixe) et les 2 formulaires
        peuvent chacun demander leur propre pool sans se marcher dessus.
      - Vérifié en direct sur la prod : `_best_partners` pour un jungler
        peu joué (Morgana jgl, une vraie paire à 50 games) renvoie 0
        candidat à 400 games mais 27 à 40 — et `/draft` bascule bien de
        "Pas assez de données fiables" à une composition complète en
        abaissant `cw_min_games`.

- [x] **6e archétype "Meilleur winrate" (2026-07-28, retour utilisateur :
      "le chemin inverse... la meilleure compo avec le meilleur winrate ou
      synergie et le score qui va avec")** : même construction que
      "Meilleure synergie" (`weights={"wr": 1.0}`, un seul axe à 100 % — le
      z-score d'un axe unique est une transformation affine, l'ordre est
      inchangé, donc un simple tri par winrate MOYEN brut sur les 10 vraies
      paires). Aucun changement SQL : `wr` était déjà sélectionné dans
      `_duo_pool`/`_best_partners` (colonne toujours présente, hors
      `_STAT_COLUMNS_SQL`), juste jamais exposé comme axe de pondération —
      2 lignes suffisent (`ARCHETYPE_STAT_COLUMNS["wr"] = "wr"` +
      nouvelle entrée `ARCHETYPES["winrate"]`) pour que tout le pipeline
      existant (seed, complétion gloutonne, raffinement, boutons 1/2/3,
      matérialisation collector) le prenne en charge. Le "score qui va
      avec" (winrate + IC) était déjà affiché sur chaque carte de
      composition — rien à ajouter côté rendu.
      - Volontairement PAS ajouté aux axes personnalisables de "Personnalise
        tes poids" (winrate est en grande partie ce que la synergie essaie
        déjà de prédire — un poids composite mélangeant les deux serait
        redondant sans usage concret identifié).
      - Passage de 5 à 6 archétypes fixes : plusieurs fixtures de test avec
        `wr` uniforme sur toutes les paires (ex. `_seed_suggest_scenario`,
        `_seed_scenario`, `_insert_pentad`) voient désormais "Meilleur
        winrate" compléter EN PLUS de "Meilleure synergie" et converger vers
        la même composition finale (aucun signal discriminant quand wr est
        identique partout) — comptes de cartes/lignes matérialisées ajustés
        en conséquence dans les tests concernés.
      - Vérifié en direct sur la prod (18 lignes matérialisées, 6 × 3) :
        carte "Meilleur winrate" affiche une vraie composition différente
        (Mordekaiser/Bel'Veth/Twisted Fate/Ziggs/Poppy) avec son winrate
        (55.6 % [52.1–59.1 %]) et sa synergie (+37.9 %) inline.

Phase 8 close pour l'instant (draft, insights, résilience, flex, poke)
— prochaine idée à définir.

**Gap constaté en marge de cette révision (2026-07-19)** : `agg_matchup`/
`score_matchup` étaient vides en prod alors que le code (`stats/aggregate.py`
+ `synergy/matchups.py`) est déployé depuis le commit `4762304` et que
`match_participants` avait bien 2,1M lignes retenues pour le patch courant
(16.14). Cause non confirmée (le service 24/24 tourne, `agg_trio`/`agg_duo`
du même patch étaient à jour — probable redéploiement Railway du
collecteur manquant après ce commit, à vérifier côté Railway par Célian).
**Corrigé le jour même par backfill manuel** : `stats.aggregate.refresh('16.14')`
(162 558 lignes `agg_matchup` — pas la peine sur 16.13, `match_participants`
déjà purgé pour ce patch, un refresh l'aurait effacé sans pouvoir le
reconstruire) puis `synergy.matchups.refresh` sur la fenêtre 16.14+16.13
(217 246 lignes `score_matchup`). À surveiller : si `agg_matchup` reste à 0
après le passage au patch suivant (16.15), c'est que le service ne
recalcule toujours pas ce agrégat tout seul — il faudra alors vraiment
creuser côté Railway plutôt que re-backfiller à la main à chaque patch.
