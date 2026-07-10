-- 001_init.sql — Schéma Postgres v0 (Phase 0, validé le 2026-07-10).
--
-- Couche brute écrite par le collector (Phase 1). Les tables d'agrégats
-- (agg_champion, agg_duo, agg_trio, agg_trio_vs_champion) arriveront en
-- migrations séparées aux Phases 2-3.
--
-- Invariants (CLAUDE.md) :
-- * `patch` est une colonne, jamais fusionnée au stockage — la fenêtre
--   multi-patchs est un filtre de lecture.
-- * `platform` / `tier` sont des données, pas des branches de code.
-- * Pas de JSON brut en base (volumétrie Railway) : extraction à l'ingestion,
--   timelines de test archivées en fixtures versionnées.

BEGIN;

CREATE TABLE schema_migrations (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- File de découverte des joueurs Emerald+ et curseur de collecte.
CREATE TABLE players (
    puuid              TEXT PRIMARY KEY,
    platform           TEXT NOT NULL,  -- 'na1' | 'euw1' | 'kr'
    routing            TEXT NOT NULL,  -- 'americas' | 'europe' | 'asia' (budget rate-limit)
    tier               TEXT NOT NULL,  -- 'EMERALD'..'CHALLENGER', snapshot à la découverte
    division           TEXT,
    discovered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    matches_fetched_at TIMESTAMPTZ    -- NULL = historique jamais scanné
);

CREATE INDEX idx_players_fetch_queue ON players (platform, matches_fetched_at NULLS FIRST);

CREATE TABLE matches (
    match_id        TEXT PRIMARY KEY,  -- 'EUW1_73…' → dédoublonnage naturel inter-joueurs
    platform        TEXT NOT NULL,
    patch           TEXT NOT NULL,     -- 'major.minor' dérivé de gameVersion
    game_version    TEXT NOT NULL,     -- version complète, pour audit
    queue_id        INT  NOT NULL,     -- 420 (ranked soloQ), filtré à la collecte
    game_creation   TIMESTAMPTZ NOT NULL,
    game_duration_s INT  NOT NULL,
    winning_team    SMALLINT NOT NULL CHECK (winning_team IN (100, 200)),
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_matches_patch_platform ON matches (patch, platform);

-- 10 lignes/match : baseline des WR individuels (Phase 3) et counters par
-- champion ennemi via jointure sur l'équipe adverse (Phase 4).
CREATE TABLE match_participants (
    match_id    TEXT NOT NULL REFERENCES matches ON DELETE CASCADE,
    team_id     SMALLINT NOT NULL CHECK (team_id IN (100, 200)),
    role        TEXT NOT NULL,  -- teamPosition : 'TOP'|'JUNGLE'|'MIDDLE'|'BOTTOM'|'UTILITY'
    champion_id INT  NOT NULL,
    win         BOOL NOT NULL,  -- dénormalisé pour agréger sans jointure
    PRIMARY KEY (match_id, team_id, role)
);

CREATE INDEX idx_participants_champion ON match_participants (champion_id, role);

-- 2 lignes/match (une par équipe) : les stats du trio jgl/mid/supp.
-- Colonnes résumées calculées à l'ingestion ; le détail ordonné (quel drake,
-- quelle tour, quand) vit dans match_objective_events.
CREATE TABLE match_trio_stats (
    match_id     TEXT NOT NULL REFERENCES matches ON DELETE CASCADE,
    team_id      SMALLINT NOT NULL CHECK (team_id IN (100, 200)),
    jgl_champion INT NOT NULL,
    mid_champion INT NOT NULL,
    sup_champion INT NOT NULL,
    win          BOOL NOT NULL,
    -- Avantage gold du trio vs trio adverse (NULL si la partie finit avant).
    gold_diff_5  INT,
    gold_diff_10 INT,
    gold_diff_15 INT,
    gold_diff_20 INT,
    gold_diff_25 INT,
    gold_diff_30 INT,
    gold_diff_35 INT,
    -- Objectifs (niveau équipe).
    grubs_taken      SMALLINT,
    herald_taken     BOOL,
    atakhan_taken    BOOL,
    drakes_taken     SMALLINT,
    soul_taken       BOOL,
    nashor_first     BOOL,
    nashor_first_s   INT,      -- timing du 1er Nashor s'il est pris par cette équipe
    first_tower      BOOL,
    towers_destroyed SMALLINT,
    plates_taken     SMALLINT, -- Σ challenges.turretPlatesTaken de l'équipe
    -- Combat & vision (sommes/parts sur les 3 membres du trio).
    first_blood_trio         BOOL,
    kill_participation_pre15 REAL,
    damage_share             REAL, -- part du trio dans les dégâts de l'équipe
    vision_score             INT,
    cc_time_s                INT,  -- Σ timeCCingOthers
    PRIMARY KEY (match_id, team_id)
);

CREATE INDEX idx_trio_champions ON match_trio_stats (jgl_champion, mid_champion, sup_champion);

-- Events d'objectifs ordonnés (mappings et attribution d'équipe repris de
-- l'adapter riot.py de macro-lab) : ordre des drakes et leur type, ordre et
-- emplacement des tours, grubs, héraut, Atakhan, Nashor, first blood.
CREATE TABLE match_objective_events (
    match_id   TEXT NOT NULL REFERENCES matches ON DELETE CASCADE,
    seq        SMALLINT NOT NULL,  -- ordre chronologique dans le match
    ts_s       INT NOT NULL,       -- timestamp en secondes de jeu
    event_type TEXT NOT NULL,      -- 'DRAGON'|'ELDER_DRAGON'|'BARON'|'RIFT_HERALD'
                                   -- |'VOID_GRUB'|'ATAKHAN'|'TOWER'|'FIRST_BLOOD'
    subtype    TEXT,               -- type de drake, towerType…
    team_id    SMALLINT NOT NULL CHECK (team_id IN (100, 200)),  -- équipe qui prend
    pos_x      INT,
    pos_y      INT,
    PRIMARY KEY (match_id, seq)
);

INSERT INTO schema_migrations (version) VALUES (1);

COMMIT;
