-- 033_draft_suggestions.sql — Compositions suggérées PRÉCALCULÉES (retour
-- utilisateur 2026-07-25 : "je voulais quand même garder les drafts
-- proposées... c'était bien sans avoir à cliquer sur proposer des
-- compositions").
--
-- Contexte : `/draft` proposait 4 compositions (une par archétype) calculées
-- à la demande (~30-47s, contres + conseils inclus) derrière un bouton
-- explicite — trop lent pour tourner à chaque chargement de page. Ces 2
-- tables matérialisent le résultat pour la région par défaut
-- (`platform = 'all'`, le plus de games — les autres régions gardent le
-- calcul à la demande via le bouton, moins consulté), rafraîchies par
-- `synergy.draft_suggestions.refresh` dans le même pipeline que
-- score_duo/score_trio/score_matchup/résilience (`collector/service.py`,
-- `refresh_scores`) — pas de cron séparé.
--
-- `draft_suggestion` : 1 ligne par archétype, les 5 champions, la synergie
-- totale, la paire de départ (toujours UNE seule pour une composition
-- auto-suggérée, contrairement à "Compose à partir de tes champions" qui
-- peut en avoir plusieurs) et les moyennes scaling/CC/gold@15 sur les 10
-- vraies paires du draft complet (sert aux conseils de jeu, texte généré à
-- la lecture — jamais stocké tout fait).
-- `draft_suggestion_counter` : 1 ligne par contre 1v1 suggéré (toujours du
-- 1v1 par rôle, jamais un contre de la draft entière — combinatoire
-- intraitable, cf. Phase 4 ❌ ci-dessus dans ROADMAP.md), `kind`
-- primary/secondary + `rank` pour l'ordre d'affichage.

BEGIN;

CREATE TABLE draft_suggestion (
    window_label   TEXT NOT NULL,
    platform       TEXT NOT NULL,
    archetype      TEXT NOT NULL,
    label          TEXT NOT NULL,
    top_champion   INT NOT NULL,
    jgl_champion   INT NOT NULL,
    mid_champion   INT NOT NULL,
    bot_champion   INT NOT NULL,
    sup_champion   INT NOT NULL,
    total_synergy  REAL NOT NULL,
    seed_roles     TEXT NOT NULL,  -- ex. 'jgl_mid' (roles de score_duo)
    seed_champ_a   INT NOT NULL,
    seed_champ_b   INT NOT NULL,
    seed_synergy   REAL NOT NULL,
    seed_games     INT NOT NULL,
    seed_tier      TEXT NOT NULL,
    advice_scaling REAL,  -- moyenne sur les 10 vraies paires, NULL si indisponible
    advice_cc      REAL,
    advice_gold15  REAL,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, platform, archetype)
);

CREATE TABLE draft_suggestion_counter (
    window_label     TEXT NOT NULL,
    platform         TEXT NOT NULL,
    archetype        TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('primary', 'secondary')),
    rank             INT NOT NULL,  -- ordre d'affichage au sein de kind (0 = 1er)
    role             TEXT NOT NULL,
    against_champion INT NOT NULL,
    champion_id      INT NOT NULL,
    delta            REAL NOT NULL,
    PRIMARY KEY (window_label, platform, archetype, kind, rank),
    FOREIGN KEY (window_label, platform, archetype)
        REFERENCES draft_suggestion (window_label, platform, archetype) ON DELETE CASCADE
);

INSERT INTO schema_migrations (version) VALUES (33);

COMMIT;
