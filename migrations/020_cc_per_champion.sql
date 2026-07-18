-- 020_cc_per_champion.sql — CC empirique (Σ timeCCingOthers/min) ventilé par
-- champion du trio/duo, en plus du total déjà affiché.
--
-- La donnée existe déjà par participant à l'ingestion (extract.py boucle sur
-- chaque membre individuellement avant de sommer) mais n'était jusqu'ici
-- jamais conservée après la sommation trio. Retour utilisateur du
-- 18/07/2026 : voir la contribution de chaque membre, pas seulement le total
-- d'équipe. Impact stockage négligeable (quelques colonnes REAL/INTEGER de
-- plus par ligne existante, pas de nouvelle ligne) — chiffré avant
-- implémentation : match_trio_stats ~1M lignes, agg_trio ~510K, score_trio
-- ~630K, quelques dizaines de Mo au total sur une base de plusieurs Go.
--
-- Duo : pas de rôles fixes (jgl_mid/jgl_sup/mid_sup selon `roles`), donc
-- champ_a_cc/champ_b_cc génériques comme champ_a/champ_b déjà en place —
-- `stats/aggregate.py` choisit la bonne colonne source par paire de rôles.

BEGIN;

ALTER TABLE match_trio_stats
    ADD COLUMN jgl_cc_time_s INTEGER,
    ADD COLUMN mid_cc_time_s INTEGER,
    ADD COLUMN sup_cc_time_s INTEGER;

ALTER TABLE agg_trio
    ADD COLUMN jgl_cc_sum DOUBLE PRECISION, ADD COLUMN jgl_cc_n INT,
    ADD COLUMN mid_cc_sum DOUBLE PRECISION, ADD COLUMN mid_cc_n INT,
    ADD COLUMN sup_cc_sum DOUBLE PRECISION, ADD COLUMN sup_cc_n INT;

ALTER TABLE agg_duo
    ADD COLUMN champ_a_cc_sum DOUBLE PRECISION, ADD COLUMN champ_a_cc_n INT,
    ADD COLUMN champ_b_cc_sum DOUBLE PRECISION, ADD COLUMN champ_b_cc_n INT;

ALTER TABLE score_trio
    ADD COLUMN jgl_cc_time_s REAL,
    ADD COLUMN mid_cc_time_s REAL,
    ADD COLUMN sup_cc_time_s REAL;

ALTER TABLE score_duo
    ADD COLUMN champ_a_cc_time_s REAL,
    ADD COLUMN champ_b_cc_time_s REAL;

INSERT INTO schema_migrations (version) VALUES (20);

COMMIT;
