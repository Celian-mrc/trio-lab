-- 011_cc_reliability.sql — Fiabilité empirique du CC par champion.
--
-- Certains champions (Nocturne confirmé en prod : ~172 s de CC moyen/game
-- contre 41-57 s pour les autres junglers à fort volume, avec un ratio
-- secondes-de-CC / immobilisation ~20x au-dessus de la médiane) gonflent
-- `timeCCingOthers` sans immobiliser réellement plus que la moyenne —
-- probablement un effet global multi-cible mal compté par Riot (même
-- famille de biais que `totalTimeCCDealt`, déjà écarté en migration 005).
-- Faute de détail par sort côté API (aucun event de CC dans le timeline),
-- impossible d'isoler la cause exacte : on détecte l'anomalie via ce ratio
-- et on l'atténue proportionnellement, jamais on ne la bonifie.
--
-- Peuplée/rafraîchie par `python -m trio_lab.ccref.sync_reliability` — à
-- relancer périodiquement (contrairement à `champion_cc_theoretical`, ce
-- ratio dépend de la méta/du patch courant, pas d'un fichier gelé).

BEGIN;

CREATE TABLE champion_cc_reliability (
    champion_id  INT PRIMARY KEY,
    reliability  REAL NOT NULL CHECK (reliability > 0 AND reliability <= 1),
    sec_per_immo REAL,  -- NULL si volume d'immobilisations insuffisant (bruit)
    games        INT NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES (11);

COMMIT;
