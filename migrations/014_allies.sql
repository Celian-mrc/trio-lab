-- 014_allies.sql — Meilleurs alliés Top/ADC individuels (miroir de 006_counters.sql).
--
-- Même principe que les counters (WR face à un ennemi), mais côté allié : le
-- trio jgl/mid/sup est fixe, l'allié varie sur les 2 rôles hors trio (Top,
-- ADC/Bottom) — jamais de combinaison 5v5 (combinatoirement intraitable,
-- CLAUDE.md, même raisonnement que l'interdiction des counters trio vs trio).

BEGIN;

-- Brut : WR du trio quand il est accompagné de tel allié Top/ADC, grain
-- (patch, platform). Rafraîchissement idempotent par patch, comme les autres
-- agrégats (`python -m trio_lab.stats.aggregate --patch X`).
CREATE TABLE agg_trio_with_ally (
    patch          TEXT NOT NULL,
    platform       TEXT NOT NULL,
    jgl_champion   INT  NOT NULL,
    mid_champion   INT  NOT NULL,
    sup_champion   INT  NOT NULL,
    ally_role      TEXT NOT NULL,  -- teamPosition allié : TOP|BOTTOM (jamais jgl/mid/sup)
    ally_champion  INT  NOT NULL,
    games          INT  NOT NULL,
    wins           INT  NOT NULL,  -- victoires DU TRIO avec cet allié
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (patch, platform, jgl_champion, mid_champion, sup_champion,
                 ally_role, ally_champion)
);

-- Scores matérialisés par fenêtre multi-patchs (même mécanique que 006) :
-- uplift_raw = WR(trio + cet allié) − WR global du trio sur la même fenêtre,
-- positif = « cet allié tire ce trio vers le haut » ; uplift = rétréci vers 0
-- (prior neutre : pas d'effet allié tant que le volume ne le prouve pas).
CREATE TABLE score_trio_with_ally (
    window_label   TEXT NOT NULL,
    platform       TEXT NOT NULL,
    jgl_champion   INT  NOT NULL,
    mid_champion   INT  NOT NULL,
    sup_champion   INT  NOT NULL,
    ally_role      TEXT NOT NULL,
    ally_champion  INT  NOT NULL,
    games          INT  NOT NULL,  -- games bruts sur la fenêtre
    games_eff      REAL NOT NULL,  -- games pondérés par les poids de patch
    wr             REAL NOT NULL,  -- WR du trio avec cet allié (pondéré fenêtre)
    uplift_raw     REAL NOT NULL,  -- wr − WR global du trio (même fenêtre)
    uplift         REAL NOT NULL,  -- lissage vers 0 : n·uplift_raw / (n + k)
    ci_low         REAL NOT NULL,  -- IC de Wilson à 95 % sur le wr (n = games_eff)
    ci_high        REAL NOT NULL,
    tier           TEXT NOT NULL CHECK (tier IN ('faible', 'moyen', 'eleve')),
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, platform, jgl_champion, mid_champion, sup_champion,
                 ally_role, ally_champion)
);

-- Page détail trio : ses meilleurs alliés, triés par uplift.
CREATE INDEX idx_score_allies_trio ON score_trio_with_ally
    (window_label, platform, jgl_champion, mid_champion, sup_champion, uplift);

INSERT INTO schema_migrations (version) VALUES (14);

COMMIT;
