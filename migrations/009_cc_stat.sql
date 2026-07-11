-- 009_cc_stat.sql — CC empirique (cc_time_s) matérialisé dans les tier lists.
--
-- Déjà collecté dans match_trio_stats.cc_time_s (Σ timeCCingOthers du trio,
-- Phase 2) mais absent des colonnes matérialisées 007/008 : la tier list
-- n'affichait ni ne triait dessus. Même mécanique que gold/vision/drakes.

BEGIN;

ALTER TABLE agg_trio
    ADD COLUMN cc_sum BIGINT,
    ADD COLUMN cc_n   INT;

ALTER TABLE agg_duo
    ADD COLUMN cc_sum BIGINT,
    ADD COLUMN cc_n   INT;

ALTER TABLE score_trio
    ADD COLUMN cc_time_s REAL;

ALTER TABLE score_duo
    ADD COLUMN cc_time_s REAL;

INSERT INTO schema_migrations (version) VALUES (9);

COMMIT;
