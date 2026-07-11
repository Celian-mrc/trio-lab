-- 008_duo_stats_gold_buckets.sql — Stats duos + gold recalé sur @5/@10/@15.
--
-- Retour de relecture Célian (11/07/2026) : une partie dure au minimum
-- 15 min, donc les jalons gold utiles en tier list sont 5/10/15 (le @25
-- introduit en 007 est retiré) ; et la tier list des duos doit porter les
-- mêmes colonnes que celle des trios. Les stats d'un duo sont les stats
-- d'équipe des parties où ce duo apparaît (quel que soit le 3e membre).

BEGIN;

ALTER TABLE agg_trio
    ADD COLUMN gold5_sum  BIGINT,
    ADD COLUMN gold5_n    INT,
    ADD COLUMN gold15_sum BIGINT,
    ADD COLUMN gold15_n   INT,
    DROP COLUMN gold25_sum,
    DROP COLUMN gold25_n;

ALTER TABLE score_trio
    ADD COLUMN gold_diff_5  REAL,
    ADD COLUMN gold_diff_15 REAL,
    DROP COLUMN gold_diff_25;

ALTER TABLE agg_duo
    ADD COLUMN gold5_sum  BIGINT,
    ADD COLUMN gold5_n    INT,
    ADD COLUMN gold10_sum BIGINT,
    ADD COLUMN gold10_n   INT,
    ADD COLUMN gold15_sum BIGINT,
    ADD COLUMN gold15_n   INT,
    ADD COLUMN vision_sum BIGINT,
    ADD COLUMN vision_n   INT,
    ADD COLUMN drakes_sum INT,
    ADD COLUMN drakes_n   INT,
    ADD COLUMN soul_sum   INT,
    ADD COLUMN soul_n     INT,
    ADD COLUMN herald_sum INT,
    ADD COLUMN herald_n   INT,
    ADD COLUMN tower1_sum INT,
    ADD COLUMN tower1_n   INT;

ALTER TABLE score_duo
    ADD COLUMN gold_diff_5      REAL,
    ADD COLUMN gold_diff_10     REAL,
    ADD COLUMN gold_diff_15     REAL,
    ADD COLUMN vision_score     REAL,
    ADD COLUMN drakes           REAL,
    ADD COLUMN soul_rate        REAL,
    ADD COLUMN herald_rate      REAL,
    ADD COLUMN first_tower_rate REAL;

INSERT INTO schema_migrations (version) VALUES (8);

COMMIT;
