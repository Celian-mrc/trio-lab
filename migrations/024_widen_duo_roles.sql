-- 024_widen_duo_roles.sql — Élargit agg_duo/score_duo aux 10 paires de rôles.
--
-- Jusqu'ici seules les 3 paires internes au trio jgl/mid/sup (jgl_mid,
-- jgl_sup, mid_sup) étaient acceptées. Phase 7 (duo généralisé) ajoute les
-- 7 paires impliquant top/bot, calculées depuis match_role_stats (migration
-- 023) plutôt que match_trio_stats — mêmes tables agg_duo/score_duo, `roles`
-- reste une simple colonne texte (CLAUDE.md #6), pas de nouvelle table.

BEGIN;

ALTER TABLE agg_duo DROP CONSTRAINT agg_duo_roles_check;
ALTER TABLE agg_duo ADD CONSTRAINT agg_duo_roles_check CHECK (roles IN (
    'jgl_mid', 'jgl_sup', 'mid_sup',
    'top_jgl', 'top_mid', 'top_bot', 'top_sup', 'jgl_bot', 'mid_bot', 'bot_sup'
));

ALTER TABLE score_duo DROP CONSTRAINT score_duo_roles_check;
ALTER TABLE score_duo ADD CONSTRAINT score_duo_roles_check CHECK (roles IN (
    'jgl_mid', 'jgl_sup', 'mid_sup',
    'top_jgl', 'top_mid', 'top_bot', 'top_sup', 'jgl_bot', 'mid_bot', 'bot_sup'
));

INSERT INTO schema_migrations (version) VALUES (24);

COMMIT;
