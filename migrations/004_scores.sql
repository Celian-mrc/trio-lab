-- 004_scores.sql — Scores de synergie matérialisés (Phase 3).
--
-- Une ligne par combinaison et par fenêtre multi-patchs. `window_label` est
-- l'étiquette de la fenêtre (patchs du plus récent au plus ancien, joints par
-- '+', ex. '16.13+16.12') : les patchs restent des colonnes de données dans
-- les tables sources, la fenêtre n'est qu'une lecture matérialisée —
-- rafraîchissement idempotent par fenêtre (DELETE + INSERT,
-- `python -m trio_lab.synergy`).

BEGIN;

CREATE TABLE score_duo (
    window_label TEXT NOT NULL,
    platform     TEXT NOT NULL,
    roles        TEXT NOT NULL CHECK (roles IN ('jgl_mid', 'jgl_sup', 'mid_sup')),
    champ_a      INT  NOT NULL,
    champ_b      INT  NOT NULL,
    games        INT  NOT NULL,  -- games bruts sur la fenêtre
    games_eff    REAL NOT NULL,  -- games pondérés par les poids de patch
    wr           REAL NOT NULL,  -- winrate pondéré fenêtre
    synergy      REAL NOT NULL,  -- wr − moyenne des WR individuels (brut)
    ci_low       REAL NOT NULL,  -- IC de Wilson à 95 % sur le wr (n = games_eff)
    ci_high      REAL NOT NULL,
    tier         TEXT NOT NULL CHECK (tier IN ('faible', 'moyen', 'eleve')),
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, platform, roles, champ_a, champ_b)
);

CREATE TABLE score_trio (
    window_label TEXT NOT NULL,
    platform     TEXT NOT NULL,
    jgl_champion INT  NOT NULL,
    mid_champion INT  NOT NULL,
    sup_champion INT  NOT NULL,
    games        INT  NOT NULL,
    games_eff    REAL NOT NULL,
    wr           REAL NOT NULL,
    synergy_raw  REAL NOT NULL,  -- wr − moyenne des 3 WR individuels
    synergy_pred REAL NOT NULL,  -- prédiction : moyenne des 3 synergies de duo
    synergy      REAL NOT NULL,  -- lissage bayésien : (n·raw + k·pred) / (n + k)
    ci_low       REAL NOT NULL,
    ci_high      REAL NOT NULL,
    tier         TEXT NOT NULL CHECK (tier IN ('faible', 'moyen', 'eleve')),
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, platform, jgl_champion, mid_champion, sup_champion)
);

-- Tier lists : tri par synergie (ou wr) au sein d'une fenêtre/plateforme.
CREATE INDEX idx_score_trio_rank ON score_trio (window_label, platform, synergy DESC);
CREATE INDEX idx_score_duo_rank ON score_duo (window_label, platform, roles, synergy DESC);

INSERT INTO schema_migrations (version) VALUES (4);

COMMIT;
