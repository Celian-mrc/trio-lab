-- 023_match_role_stats.sql — Stats par rôle individuel (Phase 7, duo généralisé).
--
-- Grain (match_id, team_id, role) : une ligne par joueur, pour les 5 rôles
-- (TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY), contrairement à match_trio_stats qui ne
-- couvre que jgl/mid/sup et somme déjà les 3 ensemble. Le trio jgl/mid/sup
-- reste le produit principal et son pipeline n'est pas modifié
-- (match_trio_stats inchangée) : cette table sert uniquement à généraliser le
-- duo à n'importe quelle paire de rôles (ex. top+jungle, bot+support).
--
-- Valeurs BRUTES par rôle (pas de diff précalculée comme gold_diff_X de
-- match_trio_stats) : le diff d'une paire donnée (ses 2 rôles vs les 2 mêmes
-- rôles de l'équipe adverse) se calcule par auto-jointure à l'agrégation
-- (`stats.aggregate`), pas à l'extraction — ça évite de precalculer les
-- 10 combinaisons de paires à l'extraction.
--
-- Les objectifs (drakes, herald, tours...) restent UNIQUEMENT dans
-- match_trio_stats : ce sont des stats d'équipe, identiques quelle que soit
-- la paire de rôles regardée — inutile de les dupliquer ici, l'agrégation
-- rejoint match_trio_stats sur (match_id, team_id) pour les récupérer.
--
-- Pas de backfill historique possible : ces valeurs viennent de la timeline
-- brute, jamais conservée après extraction (CLAUDE.md, pas de JSON brut en
-- base) — la table démarre vide et grossit à partir du déploiement.

BEGIN;

CREATE TABLE match_role_stats (
    match_id     TEXT NOT NULL REFERENCES matches ON DELETE CASCADE,
    team_id      SMALLINT NOT NULL CHECK (team_id IN (100, 200)),
    role         TEXT NOT NULL CHECK (role IN ('TOP', 'JUNGLE', 'MIDDLE', 'BOTTOM', 'UTILITY')),
    champion_id  INT NOT NULL,
    win          BOOL NOT NULL,
    gold_5       INT,
    gold_10      INT,
    gold_15      INT,
    gold_20      INT,
    gold_25      INT,
    gold_30      INT,
    gold_35      INT,
    cc_time_s    SMALLINT,  -- déjà corrigé par cc_reliability, comme match_trio_stats
    dmg_per_gold REAL,
    wards_placed SMALLINT,
    wards_killed SMALLINT,
    vision_score SMALLINT,
    PRIMARY KEY (match_id, team_id, role)
);

-- Auto-jointure « même équipe, 2 rôles » et « équipe adverse, même rôle » à
-- l'agrégation : les deux passent par la PK, pas d'index supplémentaire
-- nécessaire au volume actuel.

INSERT INTO schema_migrations (version) VALUES (23);

COMMIT;
