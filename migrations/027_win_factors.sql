-- 027_win_factors.sql — Régression logistique multi-variables (Phase 8, coach).
--
-- Matérialise l'analyse "qu'est-ce qui fait gagner" (recherche menée en
-- session le 2026-07-19) : deux populations — toutes les games, et celles où
-- le trio est derrière au gold à 15 min (leviers de comeback, différents de
-- la population complète : vision/efficacité ressources pèsent 2-3x plus).
--
-- Rafraîchissement MANUEL (`python -m trio_lab.synergy.win_factors`), pas
-- dans le cycle service : contrairement à score_duo/score_trio/score_matchup
-- (qui doivent suivre chaque nouveau match), une régression sur facteurs de
-- victoire est un signal de patch, pas de cycle — même philosophie que
-- `ccref.sync_theoretical` (enrichissement one-shot, pas automatique).

BEGIN;

CREATE TABLE score_win_factors (
    window_label TEXT NOT NULL,
    population   TEXT NOT NULL CHECK (population IN ('all', 'behind_gold15')),
    feature      TEXT NOT NULL,
    coef         REAL NOT NULL,       -- coefficient logit (variables continues : par écart-type)
    odds_ratio   REAL NOT NULL,       -- exp(coef), lecture directe : "x fois plus de chances de gagner"
    n            INT  NOT NULL,       -- games utilisées pour l'ajustement (cas complets)
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, population, feature)
);

INSERT INTO schema_migrations (version) VALUES (27);

COMMIT;
