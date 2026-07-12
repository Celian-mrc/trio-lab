-- 013_drop_atakhan.sql — Atakhan n'existe pas cette saison : retrait du suivi.
--
-- La colonne ne sera plus jamais écrite par le collector (extract.py ne
-- reconnaît plus l'event ATAKHAN) : on la supprime plutôt que de la laisser
-- NULL indéfiniment. Les events ATAKHAN déjà archivés dans
-- match_objective_events (matchs de saisons passées où il existait) restent
-- en place : ce sont des faits de match, pas une colonne de stat dérivée.

BEGIN;

ALTER TABLE match_trio_stats DROP COLUMN atakhan_taken;

INSERT INTO schema_migrations (version) VALUES (13);

COMMIT;
