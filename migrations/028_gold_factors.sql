-- 028_gold_factors.sql — Régression linéaire "qu'est-ce qui CONSTRUIT
-- l'avantage au gold @15" (Phase 8, audit méthodologique 20/07/2026).
--
-- En amont de score_win_factors (qui prédit la victoire à partir de l'état
-- de jeu, gold_diff_15 y compris) : ce modèle prédit gold_diff_15 lui-même
-- (variable CONTINUE, régression OLS pas logistique) à partir de 2 blocs
-- temporellement ordonnés — draft (avant la partie) puis comportements
-- précoces 0-15 min — pour répondre à "qu'est-ce qui construit l'avantage",
-- pas seulement "avoir l'avantage prédit la victoire" (quasi tautologique,
-- le gold est un médiateur, pas un levier — cf. docs/ROADMAP.md).
--
-- Un seul modèle à 2 blocs (pas 2 modèles en cascade — biais des "generated
-- regressors", Pagan 1984, cf. audit). `block` distingue les features à
-- l'affichage ; NULL pour l'intercept et les lignes de diagnostic R²
-- (feature = '_r2_draft_only' / '_r2_full', coef porte alors le R², pas un
-- coefficient — même esprit que 'intercept' dans score_win_factors).

BEGIN;

CREATE TABLE score_gold_factors (
    window_label TEXT NOT NULL,
    block        TEXT CHECK (block IN ('draft', 'execution')),
    feature      TEXT NOT NULL,
    coef         REAL NOT NULL,
    n            INT  NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, feature)
);

INSERT INTO schema_migrations (version) VALUES (28);

COMMIT;
