-- 018_confidence_intervals.sql — IC à 95 % pour la synergie et le scaling.
--
-- Décision actée le 2026-07-13 après vérification empirique (retour
-- utilisateur) : même question que le Wilson déjà affiché sur le WR — la
-- synergie et le scaling affichés sont-ils statistiquement distinguables de
-- zéro, ou pourraient-ils être du bruit ? Vérifié sur les données réelles :
-- seuls ~9 % des duos/trios à fiabilité élevée ont une synergie dont l'IC
-- exclut 0, et ~7 % pour le scaling — même les combos les plus joués (2000+
-- games) n'excluent pas toujours 0. Cette proportion augmentera avec le
-- volume de collecte (largeur d'IC ∝ 1/√n), sans changement de code.
--
-- synergy_ci_low/high : méthode de Newcombe (1998) pour une différence de
-- proportions, combinant l'IC de Wilson du combo (déjà matérialisé,
-- ci_low/ci_high) avec un IC normal sur la baseline (moyenne de 2 ou 3 WR
-- individuels). Pour les trios, l'IC porte sur `synergy_raw` (la synergie
-- BRUTE), pas sur `synergy` (lissée vers la prédiction duo) — l'incertitude
-- statistique est une propriété de la mesure brute, pas du lissage bayésien.
--
-- scaling_ci_low/high : erreur-type de la pente de régression pondérée (loi
-- de Student, peu de tranches disponibles), cf. `scores.weighted_slope_ci`.

BEGIN;

ALTER TABLE score_duo ADD COLUMN synergy_ci_low REAL;
ALTER TABLE score_duo ADD COLUMN synergy_ci_high REAL;
ALTER TABLE score_duo ADD COLUMN scaling_ci_low REAL;
ALTER TABLE score_duo ADD COLUMN scaling_ci_high REAL;

ALTER TABLE score_trio ADD COLUMN synergy_ci_low REAL;
ALTER TABLE score_trio ADD COLUMN synergy_ci_high REAL;
ALTER TABLE score_trio ADD COLUMN scaling_ci_low REAL;
ALTER TABLE score_trio ADD COLUMN scaling_ci_high REAL;

INSERT INTO schema_migrations (version) VALUES (18);

COMMIT;
