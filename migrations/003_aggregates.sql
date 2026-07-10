-- 003_aggregates.sql — Tables agrégées (Phase 2).
--
-- Grain : (patch, platform) + combinaison de champions. La région et le patch
-- restent des colonnes de données (règles CLAUDE.md #5/#6) : la fenêtre
-- multi-patchs et le lissage bayésien (Phase 3) sont des lectures par-dessus,
-- jamais des fusions au stockage. Rafraîchissement idempotent par patch via
-- `python -m trio_lab.stats.aggregate --patch X` (DELETE + INSERT).

BEGIN;

-- WR individuels (baseline du score de synergie, Phase 3) — tous rôles, pour
-- servir aussi de base aux counters par champion ennemi (Phase 4).
CREATE TABLE agg_champion (
    patch       TEXT NOT NULL,
    platform    TEXT NOT NULL,
    role        TEXT NOT NULL,
    champion_id INT  NOT NULL,
    games       INT  NOT NULL,
    wins        INT  NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (patch, platform, role, champion_id)
);

-- Les 3 duos du trio ; alimente aussi le prior bayésien du score trio.
-- champ_a = champion du premier rôle de `roles`, champ_b du second.
CREATE TABLE agg_duo (
    patch       TEXT NOT NULL,
    platform    TEXT NOT NULL,
    roles       TEXT NOT NULL CHECK (roles IN ('jgl_mid', 'jgl_sup', 'mid_sup')),
    champ_a     INT  NOT NULL,
    champ_b     INT  NOT NULL,
    games       INT  NOT NULL,
    wins        INT  NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (patch, platform, roles, champ_a, champ_b)
);

CREATE TABLE agg_trio (
    patch        TEXT NOT NULL,
    platform     TEXT NOT NULL,
    jgl_champion INT  NOT NULL,
    mid_champion INT  NOT NULL,
    sup_champion INT  NOT NULL,
    games        INT  NOT NULL,
    wins         INT  NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (patch, platform, jgl_champion, mid_champion, sup_champion)
);

INSERT INTO schema_migrations (version) VALUES (3);

COMMIT;
