-- 005_participant_cc.sql — CC empirique par participant (Phase 2b).
--
-- Le score CC « réel » par champion complète le score CC théorique (kit) :
-- * cc_time_s        = timeCCingOthers (le « CC score » de fin de partie,
--                      durée de CC efficace en secondes) ;
-- * immobilizations  = challenges.enemyChampionImmobilizations (compte).
-- NULL pour les matchs ingérés avant cette migration (backfill possible en
-- re-fetchant les details, 1 appel/match — à décider selon le besoin).
-- `totalTimeCCDealt` est volontairement ignoré (métrique gonflée : cumule
-- chaque slow sur chaque cible).

BEGIN;

ALTER TABLE match_participants
    ADD COLUMN cc_time_s INT,
    ADD COLUMN immobilizations SMALLINT;

INSERT INTO schema_migrations (version) VALUES (5);

COMMIT;
