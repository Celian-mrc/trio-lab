-- 016_vision_per_minute.sql — vision_sum passe de "cumulé" à "par minute".
--
-- Décision actée le 2026-07-13 après vérification empirique (retour
-- utilisateur) : le score de vision cumulé est mécaniquement gonflé par la
-- durée de la partie (Pearson durée↔vision cumulé = +0.22 sur 346 duos
-- fiables). `vision_sum` accumule désormais `vision_score / (durée_min)` par
-- match (cf. `stats/aggregate.py`), pas `vision_score` brut — BIGINT ne
-- supporte plus ces valeurs fractionnaires.

BEGIN;

ALTER TABLE agg_trio ALTER COLUMN vision_sum TYPE DOUBLE PRECISION;
ALTER TABLE agg_duo ALTER COLUMN vision_sum TYPE DOUBLE PRECISION;

INSERT INTO schema_migrations (version) VALUES (16);

COMMIT;
