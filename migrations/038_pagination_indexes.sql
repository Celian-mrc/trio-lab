-- 038_pagination_indexes.sql — Index pour la lecture paginée de agg_duo/
-- agg_trio/agg_duo_duration/agg_trio_duration (retour utilisateur
-- 2026-08-11 : navigation devenue très lente, 90+ secondes en pointe).
--
-- Cause : la pagination par clé introduite dans compute.py (`_iter_agg_groups`,
-- `_load_duration_buckets`, retour utilisateur 2026-08-02 sur l'incident OOM)
-- trie/paginate sur (colonnes du combo, platform, patch) — un ordre qui ne
-- correspond à AUCUN index existant (les clés primaires trient
-- (patch, platform, ...), patch en tête). Sans index correspondant, chaque
-- page relance un scan + tri complet de la table filtrée au lieu d'un
-- parcours d'index — sur agg_duo_duration (560k+ lignes, ~110 pages à 5000
-- lignes), ça sature l'IO disque partagé de Supabase et ralentit TOUTES les
-- requêtes du site (constaté en direct via pg_stat_activity : une requête
-- score_trio du site web bloquée >70s en DataFileRead pendant un cycle
-- compute.refresh du collector).
--
-- PAS de BEGIN/COMMIT (contrairement aux autres migrations) : CONCURRENTLY
-- ne peut pas tourner dans un bloc de transaction. Chaque CREATE INDEX
-- s'exécute donc comme sa propre transaction auto-commitée (la connexion de
-- `db.apply_migrations` est déjà en autocommit) — plus lent qu'un index
-- classique mais ne bloque jamais les lectures/écritures concurrentes,
-- important vu que ces tables sont activement lues par le service 24/24 en
-- prod au moment où cette migration est appliquée.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agg_duo_pagination
    ON agg_duo (roles, champ_a, champ_b, platform, patch);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agg_trio_pagination
    ON agg_trio (jgl_champion, mid_champion, sup_champion, platform, patch);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agg_duo_duration_pagination
    ON agg_duo_duration (platform, roles, champ_a, champ_b, duration_bucket, patch);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agg_trio_duration_pagination
    ON agg_trio_duration (platform, jgl_champion, mid_champion, sup_champion, duration_bucket, patch);

INSERT INTO schema_migrations (version) VALUES (38);
