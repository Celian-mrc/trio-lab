-- 040_flex_role_resource.sql — Matérialisation des profils de ressources par
-- (champion, rôle) pour le détecteur de picks flex (/flex), retour
-- utilisateur 2026-08-12 : test de charge avant partage Discord, la requête
-- à la demande (`web/queries.role_resource_profile`/`role_resource_baseline`)
-- scanne intégralement `match_role_stats` (10M lignes, 1,7 Go, ~17,7s par
-- appel × 2 par page) — sous 25 requêtes concurrentes, sature l'I/O de
-- l'instance Supabase partagée et fait expirer 44 à 47 requêtes sur 50
-- (confirmé par EXPLAIN ANALYZE + test de charge en session).
--
-- Pas de colonne `platform` : portée "toutes régions" uniquement, même
-- raisonnement que `score_champion_resilience`/win_factors/gold_factors —
-- `/flex` sur une région précise reste calculé à la demande (cas rare,
-- comportement inchangé), seul le cas par défaut (`platform=all`,
-- l'écrasante majorité du trafic) est matérialisé.
--
-- 2 tables plutôt qu'une avec `champion_id` nullable (NULL interdit dans une
-- clé primaire) : `_profile` = 1 ligne par (rôle, champion), `_baseline` =
-- 1 ligne par rôle (moyenne tous champions confondus, le point de
-- comparaison). Rafraîchies ensemble par `synergy.flex.refresh`, appelé
-- dans le même cycle collecteur que `resilience.refresh` (coût mesuré
-- similaire, absorbé dans un cycle déjà long).

BEGIN;

CREATE TABLE score_role_resource_profile (
    window_label     TEXT NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('TOP', 'JUNGLE', 'MIDDLE', 'BOTTOM', 'UTILITY')),
    champion_id      INT  NOT NULL,
    n                INT  NOT NULL,
    avg_gold_15      REAL,
    avg_dmg_per_gold REAL,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, role, champion_id)
);

CREATE TABLE score_role_resource_baseline (
    window_label     TEXT NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('TOP', 'JUNGLE', 'MIDDLE', 'BOTTOM', 'UTILITY')),
    n                INT  NOT NULL,
    avg_gold_15      REAL,
    avg_dmg_per_gold REAL,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, role)
);

INSERT INTO schema_migrations (version) VALUES (40);

COMMIT;
