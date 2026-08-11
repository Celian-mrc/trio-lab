-- 039_drop_cc_blend.sql — Retire le CC lissé (retour utilisateur 2026-08-11 :
-- "éviter d'utiliser des données théoriques quand on a les vraies données").
--
-- cc_theoretical_pct/cc_empirical_pct/cc_blended_pct (migration 010)
-- mélangeaient une mesure réelle (empirique) et un score de kit théorique
-- (durée/zone/fiabilité des sorts) — le CC brut mesuré en jeu (`cc_time_s`,
-- 100% API, déjà présent) remplace ce mélange partout où le CC était
-- utilisé : affichage (tierlist/duos, pages détail) ET axe "cc" des
-- archétypes de draft (`synergy/draft_suggestions.py`).
--
-- `champion_cc_theoretical` (table de référence, migration 010) N'EST PAS
-- supprimée : donnée immuable, sans coût à garder, et `ccref.sync_theoretical`
-- reste utilisable si ce choix est reconsidéré plus tard.

BEGIN;

ALTER TABLE score_trio
    DROP COLUMN cc_theoretical_pct,
    DROP COLUMN cc_empirical_pct,
    DROP COLUMN cc_blended_pct;

ALTER TABLE score_duo
    DROP COLUMN cc_theoretical_pct,
    DROP COLUMN cc_empirical_pct,
    DROP COLUMN cc_blended_pct;

INSERT INTO schema_migrations (version) VALUES (39);

COMMIT;
