-- 041_player_queue_priority.sql — File de découverte priorisée par activité
-- (retour utilisateur 2026-08-13 : "je pensais qu'on pouvait en récupérer
-- plus [de games]").
--
-- Constaté en session : 1,3M joueurs connus, ~16K scannés/jour, 54% jamais
-- scannés une seule fois, ~1 mois pour qu'un joueur déjà vu revienne en tête
-- de file (FIFO pur sur `matches_fetched_at`) — alors qu'un patch dure ~2
-- semaines. Un joueur qui joue tous les jours et un joueur qui a joué une
-- fois il y a 2 mois avaient exactement la même cadence de recheck.
--
-- `next_check_at` remplace `matches_fetched_at` comme clé de tri de la file
-- (`storage.next_player`) : recalculé à chaque scan selon son rendement
-- (`storage.mark_player_fetched`, aucun appel API supplémentaire, le compte
-- de nouveaux matchs est déjà calculé) — actif : recheck dans 12h, rien de
-- neuf ce coup-ci : recheck dans 14j, libère la place pour les joueurs
-- actifs et le stock de jamais-scannés.

BEGIN;

ALTER TABLE players ADD COLUMN next_check_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Backfill : préserve l'ordre relatif actuel (jamais scanné = maintenant,
-- déjà scanné = sa date de dernier scan) plutôt que de déclencher une ruée
-- où tout le monde redevient "dû immédiatement" au même instant. La
-- priorisation adaptative ne s'applique qu'à partir du PROCHAIN scan réel
-- de chaque joueur.
UPDATE players SET next_check_at = coalesce(matches_fetched_at, now());

DROP INDEX idx_players_fetch_queue;
CREATE INDEX idx_players_fetch_queue ON players (platform, next_check_at);

INSERT INTO schema_migrations (version) VALUES (41);

COMMIT;
