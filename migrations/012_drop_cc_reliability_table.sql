-- 012_drop_cc_reliability_table.sql — retour à un coefficient Nocturne codé en dur.
--
-- La détection générique par ratio (migration 011, barrière de Tukey) ne
-- flaggait en pratique que Nocturne sur les données réelles : la mécanique
-- (table + script de sync périodique) était disproportionnée pour un seul
-- champion. Coefficient gelé en constante (`ccref.reliability.CC_TIME_RELIABILITY`),
-- cf. sa docstring pour la justification complète.

BEGIN;

DROP TABLE champion_cc_reliability;

INSERT INTO schema_migrations (version) VALUES (12);

COMMIT;
