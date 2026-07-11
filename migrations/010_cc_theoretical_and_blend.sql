-- 010_cc_theoretical_and_blend.sql — CC théorique matérialisé + score mélangé.
--
-- `champion_cc_theoretical` matérialise `ccref.score.champion_scores()`
-- (fichier gelé, Phase 2b) résolu en championId Riot : un simple SELECT ici,
-- pas d'appel réseau (Data Dragon) à chaque cycle de refresh du service.
-- Peuplée/rafraîchie par `python -m trio_lab.ccref.sync_theoretical`, à
-- relancer seulement quand le fichier gelé change (rework) ou qu'un nouveau
-- champion sort — jamais à chaque refresh.
--
-- score_trio/score_duo gagnent les 3 % normalisés (0-100) : théorique,
-- empirique (déjà présent en secondes via cc_time_s, ici en %), mélangé
-- (lissage bayésien, même mécanique que la synergie).

BEGIN;

CREATE TABLE champion_cc_theoretical (
    champion_id INT PRIMARY KEY,
    score       REAL NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE score_trio
    ADD COLUMN cc_theoretical_pct REAL,
    ADD COLUMN cc_empirical_pct   REAL,
    ADD COLUMN cc_blended_pct     REAL;

ALTER TABLE score_duo
    ADD COLUMN cc_theoretical_pct REAL,
    ADD COLUMN cc_empirical_pct   REAL,
    ADD COLUMN cc_blended_pct     REAL;

INSERT INTO schema_migrations (version) VALUES (10);

COMMIT;
