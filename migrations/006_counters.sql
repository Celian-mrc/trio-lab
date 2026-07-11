-- 006_counters.sql — Counters par champion ennemi individuel (Phase 4).
--
-- Jamais de counters trio vs trio (combinatoirement intraitable, CLAUDE.md) :
-- le grain ennemi est UN champion dans UN rôle. Volumétrie : ~5 lignes par
-- ligne d'agg_trio (5 ennemis par game de trio), trivial au volume actuel.

BEGIN;

-- Brut : WR du trio face à chaque champion adverse, grain (patch, platform).
-- Rafraîchissement idempotent par patch avec les autres agrégats
-- (`python -m trio_lab.stats.aggregate --patch X`).
CREATE TABLE agg_trio_vs_champion (
    patch          TEXT NOT NULL,
    platform       TEXT NOT NULL,
    jgl_champion   INT  NOT NULL,
    mid_champion   INT  NOT NULL,
    sup_champion   INT  NOT NULL,
    enemy_role     TEXT NOT NULL,  -- teamPosition adverse : TOP|JUNGLE|MIDDLE|BOTTOM|UTILITY
    enemy_champion INT  NOT NULL,
    games          INT  NOT NULL,
    wins           INT  NOT NULL,  -- victoires DU TRIO face à cet ennemi
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (patch, platform, jgl_champion, mid_champion, sup_champion,
                 enemy_role, enemy_champion)
);

-- Scores matérialisés par fenêtre multi-patchs (même mécanique que 004) :
-- delta_raw = WR(trio vs ennemi) − WR global du trio sur la même fenêtre,
-- négatif = « ce trio souffre contre cet ennemi » ; delta = rétréci vers 0
-- (prior neutre : pas d'effet counter tant que le volume ne le prouve pas).
CREATE TABLE score_trio_vs_champion (
    window_label   TEXT NOT NULL,
    platform       TEXT NOT NULL,
    jgl_champion   INT  NOT NULL,
    mid_champion   INT  NOT NULL,
    sup_champion   INT  NOT NULL,
    enemy_role     TEXT NOT NULL,
    enemy_champion INT  NOT NULL,
    games          INT  NOT NULL,  -- games bruts sur la fenêtre
    games_eff      REAL NOT NULL,  -- games pondérés par les poids de patch
    wr             REAL NOT NULL,  -- WR du trio face à cet ennemi (pondéré fenêtre)
    delta_raw      REAL NOT NULL,  -- wr − WR global du trio (même fenêtre)
    delta          REAL NOT NULL,  -- lissage vers 0 : n·delta_raw / (n + k)
    ci_low         REAL NOT NULL,  -- IC de Wilson à 95 % sur le wr (n = games_eff)
    ci_high        REAL NOT NULL,
    tier           TEXT NOT NULL CHECK (tier IN ('faible', 'moyen', 'eleve')),
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, platform, jgl_champion, mid_champion, sup_champion,
                 enemy_role, enemy_champion)
);

-- Page détail trio : ses pires/meilleurs matchups, triés par delta.
CREATE INDEX idx_score_counters_trio ON score_trio_vs_champion
    (window_label, platform, jgl_champion, mid_champion, sup_champion, delta);

INSERT INTO schema_migrations (version) VALUES (6);

COMMIT;
