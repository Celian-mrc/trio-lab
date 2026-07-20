-- 030_pre15_objectives.sql — Héraut/dragon/wards AVANT 15 min (Phase 8,
-- extension du modèle gold_factors, retour utilisateur 2026-07-20).
--
-- herald_taken/soul_taken/drakes_taken/vision_score existants sur
-- match_trio_stats n'ont AUCUNE coupure temporelle (calculés sur la partie
-- entière) — inutilisables comme "comportement précoce" dans gold_factors
-- sans risquer une causalité inversée (un héraut pris à 22 min ne peut pas
-- causer un avantage mesuré à 15 min).
--
-- `herald_taken_pre15`/`dragons_taken_pre15` : dérivables des events de
-- timeline (déjà timestampés, RIFT_HERALD/DRAGON) — coupure exacte à 15 min.
-- Pas de `soul_taken_pre15` : 4 drakes non-elder avant 15 min n'arrive
-- essentiellement jamais, `dragons_taken_pre15` (compte exact 0/1/2) suffit.
--
-- `wards_pre15` : Riot n'expose `visionScore` qu'en cumulé fin de partie,
-- à AUCUN timestamp intermédiaire (ni dans `detail`, ni dans les frames de
-- la timeline) — impossible d'en dériver une version "avant 15 min" fidèle.
-- Proxy le plus honnête : nombre de wards posées + détruites avant 15 min,
-- lu directement dans les events bruts WARD_PLACED/WARD_KILL de la timeline
-- (absents de `match_objective_events`, qui ne garde que les objectifs de
-- map) — pas identique à vision_score, mais un vrai signal borné à 15 min.
--
-- Comme les 7 paires de duo étendues (Phase 7) : PAS de backfill possible,
-- `timeline` brute jamais conservée après extraction (CLAUDE.md) — ces 3
-- colonnes démarrent à NULL sur tout l'historique déjà collecté, ne se
-- peuplent qu'à partir du déploiement.

BEGIN;

ALTER TABLE match_trio_stats ADD COLUMN herald_taken_pre15 BOOL;
ALTER TABLE match_trio_stats ADD COLUMN dragons_taken_pre15 SMALLINT;
ALTER TABLE match_trio_stats ADD COLUMN wards_pre15 SMALLINT;

INSERT INTO schema_migrations (version) VALUES (30);

COMMIT;
