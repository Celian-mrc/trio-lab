"""Profil de ressources par (champion, rôle) pour le détecteur de picks flex
(`/flex`), matérialisé plutôt que calculé à la demande (retour utilisateur
2026-08-12, test de charge avant partage Discord).

La requête à la demande (ex `web/queries.role_resource_profile`/
`role_resource_baseline`) scanne intégralement `match_role_stats` (10M
lignes, 1,7 Go) : confirmé par `EXPLAIN ANALYZE` en session, ~17,7s par
appel, 2 appels par page — sous 25 requêtes concurrentes, ça sature l'I/O de
l'instance Supabase partagée (Loyalties v2) et fait expirer la quasi-totalité
des requêtes du site, pas seulement `/flex`. Payer ce coût une fois par
cycle collecteur plutôt qu'à chaque visite (même logique que
`resilience.refresh`) transforme la lecture `/flex` en requête sur une table
de quelques centaines de lignes (1 par (rôle, champion) réellement joué).

Pas de dimension `platform` : matérialise uniquement la vue "toutes régions"
(cas par défaut, l'écrasante majorité du trafic), même raisonnement que
`resilience`/`win_factors`/`gold_factors`. `/flex` sur une région précise
reste calculé à la demande dans `web/queries.py` (cas rare, comportement
inchangé)."""

from __future__ import annotations

import argparse
import logging

import psycopg
from psycopg.rows import dict_row

from trio_lab import config, db
from trio_lab.synergy.windows import PatchWindow, make_window

logger = logging.getLogger(__name__)

# Même seuil que le calcul à la demande (`FLEX_MIN_PROFILE_GAMES` côté
# web/app.py) : sous ce nombre de games, le profil n'est pas fiable. Filtré
# ici en SQL (HAVING) plutôt que côté web, pour ne matérialiser que des
# lignes déjà exploitables.
DEFAULT_MIN_GAMES = 30

_PROFILE_SQL = """
    SELECT mrs.role, mrs.champion_id, count(*) AS n,
           avg(mrs.gold_15) AS avg_gold_15, avg(mrs.dmg_per_gold) AS avg_dmg_per_gold
    FROM match_role_stats mrs
    JOIN matches m USING (match_id)
    WHERE m.patch = ANY(%(patches)s) AND mrs.gold_15 IS NOT NULL
    GROUP BY mrs.role, mrs.champion_id
    HAVING count(*) >= %(min_games)s
"""

_BASELINE_SQL = """
    SELECT mrs.role, count(*) AS n,
           avg(mrs.gold_15) AS avg_gold_15, avg(mrs.dmg_per_gold) AS avg_dmg_per_gold
    FROM match_role_stats mrs
    JOIN matches m USING (match_id)
    WHERE m.patch = ANY(%(patches)s) AND mrs.gold_15 IS NOT NULL
    GROUP BY mrs.role
"""

_PROFILE_INSERT_SQL = """
    INSERT INTO score_role_resource_profile
        (window_label, role, champion_id, n, avg_gold_15, avg_dmg_per_gold)
    VALUES
        (%(window_label)s, %(role)s, %(champion_id)s, %(n)s, %(avg_gold_15)s, %(avg_dmg_per_gold)s)
"""

_BASELINE_INSERT_SQL = """
    INSERT INTO score_role_resource_baseline
        (window_label, role, n, avg_gold_15, avg_dmg_per_gold)
    VALUES
        (%(window_label)s, %(role)s, %(n)s, %(avg_gold_15)s, %(avg_dmg_per_gold)s)
"""


def refresh(
    window: PatchWindow, *, dsn: str | None = None, min_games: int = DEFAULT_MIN_GAMES
) -> tuple[int, int]:
    """Matérialise les 2 tables pour la fenêtre. Retourne (lignes profil,
    lignes baseline) écrites. DELETE + INSERT (pas UPSERT), même
    raisonnement que `resilience.refresh`."""
    patches = list(window.patches)
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            profile_rows = cur.execute(
                _PROFILE_SQL, {"patches": patches, "min_games": min_games}
            ).fetchall()
            baseline_rows = cur.execute(_BASELINE_SQL, {"patches": patches}).fetchall()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "DELETE FROM score_role_resource_profile WHERE window_label = %s",
                (window.label,),
            )
            cur.execute(
                "DELETE FROM score_role_resource_baseline WHERE window_label = %s",
                (window.label,),
            )
            if profile_rows:
                cur.executemany(
                    _PROFILE_INSERT_SQL,
                    [{"window_label": window.label, **r} for r in profile_rows],
                )
            if baseline_rows:
                cur.executemany(
                    _BASELINE_INSERT_SQL,
                    [{"window_label": window.label, **r} for r in baseline_rows],
                )
    logger.info(
        "flex fenêtre %s rafraîchie : %d lignes profil, %d lignes baseline",
        window.label,
        len(profile_rows),
        len(baseline_rows),
    )
    return len(profile_rows), len(baseline_rows)


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.synergy.flex", description=__doc__)
    parser.add_argument(
        "--patches", required=True, help="fenêtre, du plus récent au plus ancien, ex. 16.14,16.13"
    )
    parser.add_argument("--min-games", type=int, default=DEFAULT_MIN_GAMES)
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    window = make_window([p.strip() for p in args.patches.split(",") if p.strip()])
    refresh(window, min_games=args.min_games)


if __name__ == "__main__":
    main()
