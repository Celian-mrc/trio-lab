-- 031_champion_resilience.sql — Profil de résilience par champion (Phase 8,
-- retour utilisateur 2026-07-20).
--
-- Contexte : un coefficient global ("×4 de chances de gagner en avance au
-- gold@15") moyenne des chemins vers la victoire très différents selon le
-- champion — Nasus jungle est mené au gold@15 dans 60 % de ses games (WR
-- 34 % dans cet état) mais son WR global reste ~52 %, contrairement à un
-- lane bully dont le WR s'effondre hors lead. Pas de "combinaison parfaite"
-- universelle : cette table matérialise, PAR CHAMPION/RÔLE, l'écart de WR
-- entre "en avance" et "en retard" sur 3 facteurs choisis pour leur
-- signal réel et leur indépendance mutuelle (vérifié empiriquement en
-- session, corrélations de Pearson sur echantillon prod) :
-- - team_gold_diff_15 (r=0.535 avec la victoire, l'axe le plus fort)
-- - jgl_cs_diff_15 (r=0.279 avec la victoire, r=0.456 avec le gold — recoupe
--   partiellement mais reste informatif : "l'équipe" au sens jungle)
-- - first_blood_team (r=0.123 avec la victoire, largement indépendant)
-- CC/min écarté : indépendant du gold (r=0.084) mais trop faiblement
-- corrélé à la victoire (r=0.092) pour produire des écarts par champion
-- fiables plutôt que du bruit — candidat d'extension si le volume grandit.

BEGIN;

CREATE TABLE score_champion_resilience (
    window_label TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('TOP', 'JUNGLE', 'MIDDLE', 'BOTTOM', 'UTILITY')),
    champion_id  INT  NOT NULL,
    factor       TEXT NOT NULL CHECK (factor IN
                     ('team_gold_diff_15', 'jgl_cs_diff_15', 'first_blood_team')),
    games_ahead  INT  NOT NULL,  -- état favorable : diff ≥ 0, ou booléen vrai
    wins_ahead   INT  NOT NULL,
    games_behind INT  NOT NULL,  -- état défavorable : diff < 0, ou booléen faux
    wins_behind  INT  NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (window_label, role, champion_id, factor)
);

INSERT INTO schema_migrations (version) VALUES (31);

COMMIT;
