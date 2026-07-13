-- 017_drakes_cc_per_minute.sql — drakes_sum et cc_sum passent de "cumulés" à
-- "par minute", même correction que 016_vision_per_minute.sql.
--
-- Décision actée le 2026-07-13 après vérification empirique (retour
-- utilisateur) : drakes_taken et cc_time_s sont eux aussi mécaniquement
-- gonflés par la durée de la partie (Pearson durée↔valeur, niveau match,
-- patch 16.13 : drakes +0.41, CC +0.64 — le pire des trois stats vérifiées).
-- `drakes_sum`/`cc_sum` accumulent désormais `valeur / (durée_min)` par match
-- (cf. `stats/aggregate.py`) — les types entiers ne supportent plus ces
-- valeurs fractionnaires.

BEGIN;

ALTER TABLE agg_trio ALTER COLUMN drakes_sum TYPE DOUBLE PRECISION;
ALTER TABLE agg_duo ALTER COLUMN drakes_sum TYPE DOUBLE PRECISION;
ALTER TABLE agg_trio ALTER COLUMN cc_sum TYPE DOUBLE PRECISION;
ALTER TABLE agg_duo ALTER COLUMN cc_sum TYPE DOUBLE PRECISION;

INSERT INTO schema_migrations (version) VALUES (17);

COMMIT;
