-- 032_team_gold15.sql — Diff gold@15 de l'ÉQUIPE ENTIÈRE (5 joueurs) sur les
-- pages trios/duos, en plus du gold@ du trio/duo lui-même (retour
-- utilisateur 2026-07-20).
--
-- Sourcé sur match_role_stats (5 rôles bruts, comme les 7 paires de duo
-- étendues) : cette table ne couvre QUE le patch 16.14+ (pas de backfill
-- possible, la timeline brute n'est jamais conservée après extraction —
-- cf. migrations 023/030). Colonnes dédiées team_gold15_sum/n, séparées de
-- gold15_sum (qui reste le gold du trio/duo, pas de l'équipe) : NULL sur les
-- patchs antérieurs à 16.14, se peuplent tout seul au fil des patchs
-- suivants. Même calcul que `synergy.resilience`/team_gold_diff_15.

BEGIN;

ALTER TABLE agg_trio
    ADD COLUMN team_gold15_sum DOUBLE PRECISION, ADD COLUMN team_gold15_n INT;

ALTER TABLE agg_duo
    ADD COLUMN team_gold15_sum DOUBLE PRECISION, ADD COLUMN team_gold15_n INT;

ALTER TABLE score_trio
    ADD COLUMN team_gold_diff_15 REAL;

ALTER TABLE score_duo
    ADD COLUMN team_gold_diff_15 REAL;

INSERT INTO schema_migrations (version) VALUES (32);

COMMIT;
