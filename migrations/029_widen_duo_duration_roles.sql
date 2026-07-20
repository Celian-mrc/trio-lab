-- 029_widen_duo_duration_roles.sql — Élargit agg_duo_duration aux 10 paires
-- de rôles (retour utilisateur 2026-07-20 : Scaling toujours NULL pour les
-- 7 paires étendues, ex. bot_sup à 900+ games, malgré un volume largement
-- suffisant).
--
-- Bug d'oubli lors de la Phase 7 (migration 024) : agg_duo/score_duo ont
-- bien été élargis aux 10 paires, mais agg_duo_duration (source du score de
-- scaling, migration 015) a été laissé avec son CHECK d'origine limité aux
-- 3 paires internes au trio jgl/mid/sup — aucune ligne ne pouvait jamais
-- être insérée pour les 7 autres, donc `_scaling_slope` retombait toujours
-- sur NULL (pas assez de tranches de durée) quel que soit le volume réel.

BEGIN;

ALTER TABLE agg_duo_duration DROP CONSTRAINT agg_duo_duration_roles_check;
ALTER TABLE agg_duo_duration ADD CONSTRAINT agg_duo_duration_roles_check CHECK (roles IN (
    'jgl_mid', 'jgl_sup', 'mid_sup',
    'top_jgl', 'top_mid', 'top_bot', 'top_sup', 'jgl_bot', 'mid_bot', 'bot_sup'
));

INSERT INTO schema_migrations (version) VALUES (29);

COMMIT;
