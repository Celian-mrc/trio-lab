-- 007_trio_stat_columns.sql — Stats détaillées matérialisées pour la tier list.
--
-- La tier list affiche gold/objectifs/vision sans agrégation à la volée :
-- agg_trio porte des SOMMES par (patch, platform) — sommables entre patchs
-- (pondération fenêtre) et entre plateformes (option « toutes régions ») —
-- et score_trio les moyennes finales de la fenêtre. Les paires sum/n sont
-- nécessaires car chaque stat a son propre dénominateur (NULL si la partie
-- finit avant 25 min pour gold_diff_25, etc.).

BEGIN;

ALTER TABLE agg_trio
    ADD COLUMN gold10_sum BIGINT,
    ADD COLUMN gold10_n   INT,
    ADD COLUMN gold25_sum BIGINT,
    ADD COLUMN gold25_n   INT,
    ADD COLUMN vision_sum BIGINT,
    ADD COLUMN vision_n   INT,
    ADD COLUMN drakes_sum INT,
    ADD COLUMN drakes_n   INT,
    ADD COLUMN soul_sum   INT,  -- parties avec l'âme prise
    ADD COLUMN soul_n     INT,  -- parties où soul_taken est renseigné
    ADD COLUMN herald_sum INT,
    ADD COLUMN herald_n   INT,
    ADD COLUMN tower1_sum INT,
    ADD COLUMN tower1_n   INT;

ALTER TABLE score_trio
    ADD COLUMN gold_diff_10     REAL,  -- moyennes pondérées fenêtre, NULL si
    ADD COLUMN gold_diff_25     REAL,  -- aucune partie ne renseigne la stat
    ADD COLUMN vision_score     REAL,
    ADD COLUMN drakes           REAL,
    ADD COLUMN soul_rate        REAL,
    ADD COLUMN herald_rate      REAL,
    ADD COLUMN first_tower_rate REAL;

INSERT INTO schema_migrations (version) VALUES (7);

COMMIT;
