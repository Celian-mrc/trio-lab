-- 015_scaling.sql — Score de scaling (Phase 5, ajout) : WR par tranche de
-- durée de partie.
--
-- Décision actée le 2026-07-12 après validation empirique : une tentative de
-- mélange avec la trajectoire de gold (pente gold_diff ~ minute) a été
-- écartée — corrélation quasi nulle (Pearson/Spearman < 0.1, y compris sur
-- 195 duos à 500+ games chacun, cf. script d'investigation jetable). Le score
-- de scaling est donc UNIQUEMENT empirique, sans lissage vers un prior : NULL
-- tant que le volume ne permet pas au moins 3 tranches de durée exploitables.
--
-- Grain : tranches de 5 min (15/20/25/.../40, 40 = catch-all "40 min et
-- plus" pour éviter des tranches quasi vides sur les très longues games).

BEGIN;

CREATE TABLE agg_trio_duration (
    patch            TEXT NOT NULL,
    platform         TEXT NOT NULL,
    jgl_champion     INT  NOT NULL,
    mid_champion     INT  NOT NULL,
    sup_champion     INT  NOT NULL,
    duration_bucket  SMALLINT NOT NULL,  -- LEAST(40, 5 * (game_duration_s / 300))
    games            INT  NOT NULL,
    wins             INT  NOT NULL,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (patch, platform, jgl_champion, mid_champion, sup_champion, duration_bucket)
);

CREATE TABLE agg_duo_duration (
    patch            TEXT NOT NULL,
    platform         TEXT NOT NULL,
    roles            TEXT NOT NULL CHECK (roles IN ('jgl_mid', 'jgl_sup', 'mid_sup')),
    champ_a          INT  NOT NULL,
    champ_b          INT  NOT NULL,
    duration_bucket  SMALLINT NOT NULL,
    games            INT  NOT NULL,
    wins             INT  NOT NULL,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (patch, platform, roles, champ_a, champ_b, duration_bucket)
);

-- Pente de régression linéaire pondérée WR ~ tranche (unité : points de WR
-- par tranche de 5 min), NULL si moins de 3 tranches exploitables (≥3 games
-- bruts chacune) — pas de lissage vers un prior, cf. commentaire d'en-tête.
ALTER TABLE score_trio ADD COLUMN scaling REAL;
ALTER TABLE score_duo ADD COLUMN scaling REAL;

INSERT INTO schema_migrations (version) VALUES (15);

COMMIT;
