-- 002_collector_journal.sql — Journal de collecte (Phase 1).
--
-- État par match_id pour le dédoublonnage et la reprise :
-- * 'excluded'        : match rejeté par les critères d'inclusion (queue, durée,
--                       early surrender, mauvais patch, parsing) — jamais retenté ;
-- * 'error_retryable' : échec transitoire (réseau, 5xx) — retenté au prochain
--                       passage, `attempts` incrémenté ;
-- * 'error_permanent' : échec au-delà du seuil de tentatives — jamais retenté.
--
-- Les matchs réussis ne figurent PAS ici : leur présence dans `matches` suffit
-- (le PK match_id assure le dédoublonnage cross-joueurs et cross-runs).

BEGIN;

CREATE TABLE match_fetch_journal (
    match_id     TEXT PRIMARY KEY,
    platform     TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('excluded', 'error_retryable', 'error_permanent')),
    reason       TEXT,
    attempts     SMALLINT NOT NULL DEFAULT 0,
    last_attempt TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES (2);

COMMIT;
