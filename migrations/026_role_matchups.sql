-- 026_role_matchups.sql — Counter 1v1 même rôle (retour, redesigné).
--
-- Les counters trio-vs-champion (score_trio_vs_champion, migration 006) ont
-- été abandonnés le 2026-07-19 (migration 022) : la dimension TRIO du côté
-- « nous » faisait exploser le combinatoire (jgl×mid×sup×ennemi×rôle) pour
-- un signal peu fiable (quelques games par combo). Le vrai contre-pick, tel
-- que le font les outils de draft du marché (METAsrc Counter Picker etc.),
-- c'est un duel 1 contre 1 dans le MÊME rôle (top vs top, jungle vs jungle),
-- pas trio vs champion individuel. Espace combinatoire : 5 rôles × ~170²
-- champions au maximum, du même ordre de grandeur que agg_duo/score_duo —
-- rien à voir avec l'explosion de l'ancien système.
--
-- Source : match_participants (5 rôles, historique complet), pas
-- match_trio_stats — aucune dépendance au trio jgl/mid/sup.

BEGIN;

CREATE TABLE agg_matchup (
    patch       TEXT NOT NULL,
    platform    TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('TOP', 'JUNGLE', 'MIDDLE', 'BOTTOM', 'UTILITY')),
    champ_a     INT  NOT NULL,
    champ_b     INT  NOT NULL,  -- adversaire du même rôle
    games       INT  NOT NULL,
    wins        INT  NOT NULL,  -- victoires DE champ_a
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (patch, platform, role, champ_a, champ_b)
);

-- Scores matérialisés par fenêtre multi-patchs (même mécanique que 004/006) :
-- delta_raw = WR(champ_a vs champ_b, même rôle) − WR baseline de champ_a
-- dans ce rôle (agg_champion, toutes compositions confondues), négatif =
-- « champ_a souffre contre champ_b ». Rétréci vers 0 (prior neutre), même k
-- que synergy/compute.py.
CREATE TABLE score_matchup (
    window_label TEXT NOT NULL,
    platform     TEXT NOT NULL,
    role         TEXT NOT NULL,
    champ_a      INT  NOT NULL,
    champ_b      INT  NOT NULL,
    games        INT  NOT NULL,
    games_eff    REAL NOT NULL,
    wr           REAL NOT NULL,
    delta_raw    REAL NOT NULL,
    delta        REAL NOT NULL,
    ci_low       REAL NOT NULL,
    ci_high      REAL NOT NULL,
    tier         TEXT NOT NULL CHECK (tier IN ('faible', 'moyen', 'eleve')),
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, platform, role, champ_a, champ_b)
);

-- Page détail champion : ses pires/meilleurs matchups, triés par delta.
CREATE INDEX idx_score_matchup_rank ON score_matchup (window_label, platform, role, champ_a, delta);

INSERT INTO schema_migrations (version) VALUES (26);

COMMIT;
