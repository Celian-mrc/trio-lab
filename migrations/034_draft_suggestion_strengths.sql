-- 034_draft_suggestion_strengths.sql — Points FORTS d'une composition, en
-- plus des points faibles (Phase 9, retour utilisateur 2026-07-26 : "est-ce
-- qu'en plus d'avoir le contre on peut aussi avoir contre qui cette
-- composition est forte ?").
--
-- Contexte : `draft_suggestion_counter` (migration 033) ne stockait que les
-- points FAIBLES (les champions adverses qui punissent la composition,
-- `score_matchup` avec le champion de la composition en `champ_b`). Même
-- table, même forme, juste la direction du 1v1 qui s'inverse pour les
-- points forts (`champ_a` = le champion de la composition, `champ_b` = le
-- champion qu'il bat le mieux) — `direction` distingue les 2 sans dupliquer
-- le schéma. Données entièrement recalculées à chaque `refresh` (jamais
-- d'historique), le DEFAULT ne sert qu'à la transition de cette migration.

BEGIN;

ALTER TABLE draft_suggestion_counter
    ADD COLUMN direction TEXT NOT NULL DEFAULT 'weakness'
        CHECK (direction IN ('weakness', 'strength'));

ALTER TABLE draft_suggestion_counter DROP CONSTRAINT draft_suggestion_counter_pkey;
ALTER TABLE draft_suggestion_counter
    ADD PRIMARY KEY (window_label, platform, archetype, direction, kind, rank);

INSERT INTO schema_migrations (version) VALUES (34);

COMMIT;
