-- 037_range_theoretical.sql — Score de portée théorique par champion
-- (Phase 8+, retour utilisateur 2026-07-28 : "il manque un archétype
-- poke avec de la range" + "récupérer la portée des sorts").
--
-- Même schéma que `champion_cc_theoretical` (migration 010) : 1 ligne par
-- champion, un score BRUT (normalisé 0-100 à l'agrégation duo/trio dans
-- `synergy/compute.py`, réutilise `ccref.score.theoretical_pct`).
--
-- Contrairement au CC, PAS de colonnes empirique/mélangé : aucune stat
-- Riot ne mesure la distance de poke réelle en jeu (vérifié — la Timeline
-- API n'expose aucun événement de sort lancé avec position), donc rien à
-- mélanger. Une seule colonne, nommée explicitement "theoretical" pour que
-- ce soit impossible à manquer en lisant une requête : ce chiffre n'est
-- JAMAIS recalé par le comportement réel des joueurs, contrairement à tous
-- les autres axes d'archétype (scaling/CC/gold/drakes/âme).
--
-- Peuplée par `python -m trio_lab.rangeref.sync` (Data Dragon, jamais de
-- scraping wiki — les portées sont déjà des champs numériques structurés),
-- à relancer seulement à la sortie d'un champion ou un rework de portée.

BEGIN;

CREATE TABLE champion_range_theoretical (
    champion_id INT PRIMARY KEY,
    score       REAL NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE score_duo ADD COLUMN range_theoretical_pct REAL;
ALTER TABLE score_trio ADD COLUMN range_theoretical_pct REAL;

INSERT INTO schema_migrations (version) VALUES (37);

COMMIT;
