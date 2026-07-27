-- 036_draft_suggestion_variants.sql — Jusqu'à 3 propositions par archétype
-- sur "Compositions suggérées" (Phase 9, retour utilisateur 2026-07-27 :
-- "à côté du nom de l'archétype, des boutons 1/2/3 pour 3 propositions").
--
-- `draft_suggestion`/`draft_suggestion_counter` stockaient 1 ligne par
-- archétype (clé (window_label, platform, archetype)). Passage à jusqu'à 3
-- variantes, sélectionnées par `synergy.draft_suggestions.propose_drafts` :
-- rang 0 = meilleur score, rang 1 = 2e meilleur score suffisamment
-- DIFFÉRENT du rang 0 (au plus DIVERSITY_MAX_SHARED_CHAMPIONS champions en
-- commun, peu importe le rôle), rang 2 = la composition la plus FIABLE
-- (games_eff le plus élevé sur son duo de départ, pas le score) parmi les
-- candidats restants encore suffisamment différents des rangs 0/1 — retour
-- utilisateur 2026-07-27 : "une des trois avec fiabilité très élevée,
-- plus de games que les autres". Jamais forcé à 3 : si la diversité
-- manque, moins de lignes sont écrites (cf. code, jamais de doublon).
--
-- `suggestion_rank` ajouté à la clé des 2 tables — jamais réutilisé le nom
-- `rank`, déjà pris par `draft_suggestion_counter` pour l'ordre des contres
-- primaire/secondaire au sein d'UNE composition. `selection` stocké à part
-- (pas dérivé de `suggestion_rank` à la lecture) : si le raffinement fait
-- converger le rang 1 ("diverse") vers le rang 0 après coup, la ligne est
-- supprimée et le rang 2 ("reliable") devient rang 1 — le rang seul ne
-- suffit alors plus à retrouver la méthode de sélection d'origine.

BEGIN;

ALTER TABLE draft_suggestion ADD COLUMN suggestion_rank SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE draft_suggestion
    ADD COLUMN selection TEXT NOT NULL DEFAULT 'score'
        CHECK (selection IN ('score', 'diverse', 'reliable'));
ALTER TABLE draft_suggestion_counter ADD COLUMN suggestion_rank SMALLINT NOT NULL DEFAULT 0;

-- La FK doit tomber avant les 2 PK (elle dépend de l'index unique du PK
-- de draft_suggestion) ; son propre PK tombe aussi avant d'être recréé.
ALTER TABLE draft_suggestion_counter
    DROP CONSTRAINT draft_suggestion_counter_window_label_platform_archetype_fkey;
ALTER TABLE draft_suggestion_counter DROP CONSTRAINT draft_suggestion_counter_pkey;
ALTER TABLE draft_suggestion DROP CONSTRAINT draft_suggestion_pkey;

ALTER TABLE draft_suggestion
    ADD PRIMARY KEY (window_label, platform, archetype, suggestion_rank);
ALTER TABLE draft_suggestion_counter
    ADD PRIMARY KEY (window_label, platform, archetype, suggestion_rank, direction, kind, rank);
ALTER TABLE draft_suggestion_counter
    ADD FOREIGN KEY (window_label, platform, archetype, suggestion_rank)
        REFERENCES draft_suggestion (window_label, platform, archetype, suggestion_rank)
        ON DELETE CASCADE;

INSERT INTO schema_migrations (version) VALUES (36);

COMMIT;
