-- 019_autovacuum_tuning.sql — autovacuum plus agressif sur les tables
-- score_* à fort churn.
--
-- Contexte (14/07/2026, cf. mémoire supabase-disk-growth) : ces tables sont
-- réécrites en UPSERT à chaque cycle du collector (phase N+1, commit du même
-- jour) au lieu de DELETE+INSERT complet, mais restent les plus grosses du
-- schéma (score_trio_vs_champion ~5,8M lignes, score_trio_with_ally ~2,3M) :
-- le seuil par défaut (autovacuum_vacuum_scale_factor = 0.2, soit 20 % de la
-- table) laisse s'accumuler des millions de tuples morts avant de déclencher
-- un vacuum sur des tables de cette taille. On l'abaisse pour ces trois
-- tables spécifiquement plutôt que globalement (pas d'impact sur les tables
-- brutes/agrégats, déjà couvertes par les purges de maintenance.py).

BEGIN;

ALTER TABLE score_trio SET (autovacuum_vacuum_scale_factor = 0.02);
ALTER TABLE score_trio_vs_champion SET (autovacuum_vacuum_scale_factor = 0.02);
ALTER TABLE score_trio_with_ally SET (autovacuum_vacuum_scale_factor = 0.02);

INSERT INTO schema_migrations (version) VALUES (19);

COMMIT;
