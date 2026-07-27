-- 035_draft_suggestion_winrate.sql — Winrate + intervalle de confiance sur
-- une composition suggérée, en plus de la synergie totale (Phase 9, retour
-- utilisateur 2026-07-27 : "en plus du taux de synergie, le winrate avec
-- son IC ?").
--
-- Contexte : `score_duo.wr`/`ci_low`/`ci_high` (intervalle de Wilson sur le
-- winrate, cf. `synergy/compute.py`) existent déjà par PAIRE — ces 3
-- colonnes stockent la moyenne sur les 10 vraies paires de la composition
-- complète, même principe que `advice_scaling`/`advice_cc`/`advice_gold15`
-- (migration 033) : une moyenne simple sur les 10 paires, pas une
-- combinaison statistique rigoureuse des IC — assez pour donner un ordre de
-- grandeur, pas un test d'hypothèse.

BEGIN;

ALTER TABLE draft_suggestion
    ADD COLUMN wr         REAL,
    ADD COLUMN wr_ci_low  REAL,
    ADD COLUMN wr_ci_high REAL;

INSERT INTO schema_migrations (version) VALUES (35);

COMMIT;
