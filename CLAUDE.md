# Trio Lab — Synergies Jungle / Mid / Support (LoL)

Projet perso : interface exposant les winrates et scores de synergie par **trio
de champions jungle/mid/support**, avec stats détaillées (gold, objectifs,
vision, dégâts). Collector + interface 24/24 sur Railway →
Postgres **Supabase** (schéma dédié `trio_lab` sur le projet "Loyalties v2",
migré depuis Railway le 11/07/2026 — cf. memory `railway-deployment`).

Projet frère de `C:\macro-lab` : le client Riot (throttling, back-off) et les
mappings d'events timeline en sont dérivés — ne pas réinventer, adapter.

## Lecture obligatoire avant de coder

- `@docs/PROJECT.md` — vision, score de synergie, stratégie statistique
  (lissage bayésien), liste des stats. Lire avant tout travail sur les scores.
- `@docs/ROADMAP.md` — phases et avancement. Lire au début de chaque session.

## Règles non-négociables

1. **Ne jamais committer de clé API.** Secrets via `.env` uniquement (jamais
   hard-codé, jamais loggé).
2. **Respecter les rate limits Riot.** Tout appel API passe par le client
   centralisé du module `collector/` (throttling + back-off 429, budget séparé
   par région de routage). Pas de `requests.get` direct ailleurs.
3. **Phase par phase.** Pas de phase N+1 avant que la phase N soit verte
   (tests, commit, case cochée dans ROADMAP.md). En début de phase, proposer
   l'architecture en pseudo-code/arbre et attendre validation avant de coder.
4. **Données de match uniquement via l'API Riot.** Pas de scraping de sites
   tiers. Exception scopée : les tables de *propriétés intrinsèques du jeu*
   (ex. `data/external/cc_reference.csv`) peuvent être importées via l'**API
   MediaWiki du wiki LoL** (contenu CC BY-SA, attribution requise) par script
   **one-shot avec relecture humaine avant gel** — jamais de scraping HTML
   récurrent. Fichiers immuables, jamais mélangés aux données de match.
5. **Les patchs ne sont jamais fusionnés au stockage.** `patch` est une
   colonne obligatoire ; la fenêtre multi-patchs est un filtre de lecture.
6. **Jamais de `if region == ...` dans le code d'analyse.** La région, le
   patch et le rang sont des colonnes de données, pas des branches de code.

## Conventions

- Python 3.11+, `ruff format` + `ruff check`, tests `pytest` (pas de test =
  pas de merge), `logging` standard (pas de `print` en prod).
- Docstrings en français OK, code et noms de variables en anglais.
- Commits en français, format court : `phase X: <description>` ou
  `fix: <description>`.
- Postgres pour le stockage, hébergé sur **Supabase** (schéma `trio_lab`,
  projet partagé avec une autre appli — jamais toucher aux autres schémas).
  SQL versionné en migrations simples (fichiers numérotés), pas d'ORM lourd
  tant que ce n'est pas justifié.

## Ce qu'il ne faut PAS faire

- Ne pas réinventer un client Riot : adapter celui de macro-lab.
- Pas de dépendance lourde sans justification (pas de Spark/Dask).
- Pas de front « joli » tant que le pipeline de stats n'est pas solide
  (Phase 5 et pas avant).
- Pas de counters **trio** vs champion individuel ni vs trio adverse
  (combinatoirement intraitable : implémenté en Phase 4, abandonné le
  2026-07-19, cf. `docs/ROADMAP.md`). Un counter **1v1 même rôle** (champion
  vs champion, ex. top vs top) reste OK — c'est `agg_matchup`/`score_matchup`
  (migration 026) : combinatoire borné (5 rôles × champions²), pas de
  dimension trio, la vraie source du problème initial.

## Workflow de session

1. Lire `docs/ROADMAP.md` pour identifier la phase en cours.
2. Confirmer l'objectif de la session en une phrase avant de coder.
3. Coder par petits incréments, tester au fur et à mesure.
4. En fin de session : cocher ROADMAP.md, commit, résumé d'une ligne.
