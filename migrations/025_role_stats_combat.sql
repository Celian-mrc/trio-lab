-- 025_role_stats_combat.sql — Combat par rôle individuel (Phase 7 suite).
--
-- Complète match_role_stats (023) pour que la page détail duo puisse afficher
-- les mêmes cartes Objectifs/Combat/Vision que la page trio, décomposées à
-- 2 membres pour les 7 paires hors trio jgl/mid/sup :
-- - `damage` brut (déjà lu pour dmg_per_gold, seul le total manquait) : permet
--   une part de dégâts d'équipe exacte pour la paire (somme de 2, pas
--   d'ambiguïté, contrairement au KP ci-dessous).
-- - `first_blood` : ce rôle a le kill ou l'assist du 1er sang — combinable en
--   OR exact pour une paire (un seul événement, un petit ensemble de
--   participants, pas de double-comptage possible).
-- - `kp_pre15` : kill participation INDIVIDUELLE (kills de l'équipe où ce
--   rôle est killer/assist ÷ kills totaux avant 15 min), PAS combinée en OR
--   pour une paire — combiner 2 ratios individuels en un seul indicateur
--   « au moins un des deux » demanderait de rejouer les events de kill par
--   paire (double-comptage sinon si les 2 membres partagent un même kill) ;
--   affichée par membre, comme le CC ou le dégâts/gold.

BEGIN;

ALTER TABLE match_role_stats ADD COLUMN damage INT;
ALTER TABLE match_role_stats ADD COLUMN first_blood BOOL;
ALTER TABLE match_role_stats ADD COLUMN kp_pre15 REAL;

INSERT INTO schema_migrations (version) VALUES (25);

COMMIT;
