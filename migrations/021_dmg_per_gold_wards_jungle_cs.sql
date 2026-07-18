-- 021_dmg_per_gold_wards_jungle_cs.sql — 3 nouvelles stats de détail trio/duo,
-- demandées suite à la revue du CC par champion (18/07/2026) :
--
-- * jgl/mid/sup_dmg_per_gold : dégâts aux champions ÷ gold gagné, par membre
--   (met en avant les champions "efficaces" à faible ressource) ;
-- * wards_placed/wards_killed : décomposition du score de vision agrégé,
--   niveau trio (comme vision_score) ;
-- * jgl_cs_diff_15 : écart de CS jungle à la 15e minute vs le jungler adverse
--   (domination early jungle, avant que les rotations macro ne redistribuent
--   le farm) — lu depuis la timeline (`jungleMinionsKilled` par frame), même
--   mécanisme que `gold_diff_15`.
--
-- Ni `goldEarned`, ni `wardsPlaced/wardsKilled`, ni `jungleMinionsKilled` ne
-- sont stockés ailleurs en base (contrairement au CC, cf. migration 020) :
-- aucun backfill gratuit possible, ces colonnes ne se rempliront que pour les
-- matchs collectés après ce déploiement.
--
-- Détail (comme damage_share/kill_participation_pre15/plates_taken) : ces
-- stats ne sont PAS matérialisées dans agg_trio/score_trio, seulement dans
-- match_trio_stats — lues à la volée par `web.summary` pour la page détail
-- d'UN trio/duo (volume par trio modeste, pas besoin de matérialiser sur les
-- ~5M trios possibles).

BEGIN;

ALTER TABLE match_trio_stats
    ADD COLUMN jgl_dmg_per_gold REAL,
    ADD COLUMN mid_dmg_per_gold REAL,
    ADD COLUMN sup_dmg_per_gold REAL,
    ADD COLUMN wards_placed SMALLINT,
    ADD COLUMN wards_killed SMALLINT,
    ADD COLUMN jgl_cs_diff_15 SMALLINT;

INSERT INTO schema_migrations (version) VALUES (21);

COMMIT;
