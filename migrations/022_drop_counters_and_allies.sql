-- 022_drop_counters_and_allies.sql — Retrait counters/alliés (abandon, 2026-07-19).
--
-- score_trio_vs_champion et score_trio_with_ally étaient déjà identifiées
-- comme le pire poste de volumétrie du schéma (cf. maintenance.py) : le
-- combinatoire trio × champion ennemi/allié individuel grossit trop vite
-- pour un signal jugé peu fiable (peu de games par combo, prior bayésien qui
-- ne suffit pas à compenser). Décision : abandon plutôt que retuning.

BEGIN;

DROP TABLE IF EXISTS score_trio_vs_champion;
DROP TABLE IF EXISTS agg_trio_vs_champion;
DROP TABLE IF EXISTS score_trio_with_ally;
DROP TABLE IF EXISTS agg_trio_with_ally;

INSERT INTO schema_migrations (version) VALUES (22);

COMMIT;
