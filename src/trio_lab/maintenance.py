"""Rétention : purge différenciée par table (révisée le 2026-07-11).

Chaque table brute n'est utile que pour un usage précis, borné dans le temps —
les traiter toutes pareil (comme la version initiale de ce module) fait
exploser le stockage bien avant la volumétrie visée par le projet :

- **match_objective_events** : lu UNE SEULE FOIS, à l'ingestion
  (`stats/extract.py`), jamais relu après. Rétention courte (quelques
  heures), pas un patch entier — c'est de très loin le plus gros poste brut.
- **match_participants** : relu à CHAQUE rafraîchissement des agrégats du
  patch EN COURS (`aggregate.refresh` re-scanne tout le patch à chaque
  cycle). Inutile dès que le patch n'est plus le plus récent.
- **matches + match_trio_stats** : lus en direct par la page détail trio
  (`web/queries.trio_match_rows`) sur toute la fenêtre de synergie (jusqu'à
  3 patchs) — rétention la plus longue des tables brutes.
- **agg_\\***  : sommes par patch, bon marché à l'unité, mais la version
  initiale ne les purgeait JAMAIS → accumulation sur tous les patchs jamais
  collectés. Bornées ici à une fenêtre légèrement plus large que celle des
  scores (marge de sécurité pendant un rollover de patch).
- **score_\\*** : matérialisées par `window_label`. Chaque rollover de patch
  crée un nouveau label sans supprimer l'ancien → accumulation illimitée.
  Purgées à la fenêtre courante uniquement : l'interface n'affiche que la
  plus récente (`available_windows` la liste toujours en premier).

Seule famille d'opérations destructives du projet. Le journal
(`match_fetch_journal`) n'est jamais purgé : il empêche de re-télécharger un
match déjà exclu/échoué si un joueur y renvoie plus tard.

Usage : `python -m trio_lab.maintenance [--dry-run]` (appelé par le mode
service : `purge_stale_objective_events` à chaque cycle, le reste
quotidiennement — voir `collector/service.py`).
"""

from __future__ import annotations

import argparse
import logging

import psycopg

from trio_lab import config, db
from trio_lab.synergy.windows import patch_key

logger = logging.getLogger(__name__)

# Profondeur de la fenêtre de synergie (PatchWindow, 3 patchs pondérés).
RAW_KEEP = 3
# Marge au-delà de RAW_KEEP : un agrégat de plus reste disponible pendant la
# bascule d'un rollover, avant que la fenêtre de scoring ne s'y adapte.
AGG_KEEP = RAW_KEEP + 1
# Le patch en cours de collecte suffit : match_participants n'est relu que
# pour re-agréger CE patch, jamais un patch déjà clos.
PARTICIPANTS_KEEP = 1
# match_objective_events n'est jamais relu après l'ingestion (extraction déjà
# faite dans match_trio_stats) : une rétention courte, pas par patch.
EVENTS_RETENTION_HOURS = 24
# L'interface n'expose que la fenêtre la plus récente (available_windows).
SCORE_KEEP_LABELS = 1

DEFAULT_KEEP = RAW_KEEP  # rétro-compatibilité (ancien nom du paramètre CLI)


def _known_patches(conn: psycopg.Connection, table: str) -> list[str]:
    """Patchs distincts d'une table, du plus récent au plus ancien."""
    rows = conn.execute(f"SELECT DISTINCT patch FROM {table}").fetchall()  # noqa: S608
    return sorted((r[0] for r in rows), key=patch_key, reverse=True)


def purge_stale_objective_events(
    *,
    older_than_hours: int = EVENTS_RETENTION_HOURS,
    dry_run: bool = False,
    dsn: str | None = None,
) -> dict:
    """Purge `match_objective_events` au-delà de `older_than_hours` (jamais relu ensuite)."""
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        deleted = 0
        if not dry_run:
            cur = conn.execute(
                "DELETE FROM match_objective_events e USING matches m"
                " WHERE e.match_id = m.match_id"
                " AND m.collected_at < now() - (%s || ' hours')::interval",
                (older_than_hours,),
            )
            deleted = cur.rowcount
    logger.info(
        "rétention events (>%dh%s) : %d lignes supprimées",
        older_than_hours,
        ", dry-run" if dry_run else "",
        deleted,
    )
    return {"events_deleted": deleted}


def purge_stale_participants(
    *, keep: int = PARTICIPANTS_KEEP, dry_run: bool = False, dsn: str | None = None
) -> dict:
    """Purge `match_participants` des patchs au-delà des `keep` plus récents."""
    if keep < 1:
        raise ValueError(f"keep doit être ≥ 1, reçu {keep}")
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        known = _known_patches(conn, "matches")
        old = known[keep:]
        deleted = 0
        if old and not dry_run:
            cur = conn.execute(
                "DELETE FROM match_participants p USING matches m"
                " WHERE p.match_id = m.match_id AND m.patch = ANY(%s)",
                (old,),
            )
            deleted = cur.rowcount
    logger.info(
        "rétention participants (keep=%d%s) : patchs purgés %s, %d lignes supprimées",
        keep,
        ", dry-run" if dry_run else "",
        old or "aucun",
        deleted,
    )
    return {"purged_patches": old, "participants_deleted": deleted}


def purge_old_patches(
    *, keep: int = RAW_KEEP, dry_run: bool = False, dsn: str | None = None
) -> dict:
    """Supprime les `matches` (cascade `match_trio_stats` + résidus) au-delà de `keep` patchs."""
    if keep < 1:
        raise ValueError(f"keep doit être ≥ 1, reçu {keep}")
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        known = _known_patches(conn, "matches")
        old = known[keep:]
        deleted = 0
        if old and not dry_run:
            cur = conn.execute("DELETE FROM matches WHERE patch = ANY(%s)", (old,))
            deleted = cur.rowcount
    logger.info(
        "rétention matchs (keep=%d%s) : patchs purgés %s, %d matchs supprimés",
        keep,
        ", dry-run" if dry_run else "",
        old or "aucun",
        deleted,
    )
    return {"purged_patches": old, "matches_deleted": deleted}


_AGG_TABLES = (
    "agg_champion",
    "agg_duo",
    "agg_trio",
    "agg_trio_duration",
    "agg_duo_duration",
    "agg_matchup",
)


def purge_stale_aggregates(
    *, keep: int = AGG_KEEP, dry_run: bool = False, dsn: str | None = None
) -> dict:
    """Purge `agg_*` des patchs au-delà des `keep` plus récents (source : `agg_trio`).

    Indépendant de la rétention brute : `agg_trio` reste la trace de tous les
    patchs jamais collectés tant que cette fonction n'a pas tourné.
    """
    if keep < 1:
        raise ValueError(f"keep doit être ≥ 1, reçu {keep}")
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        known = _known_patches(conn, "agg_trio")
        old = known[keep:]
        deleted = 0
        if old and not dry_run:
            for table in _AGG_TABLES:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE patch = ANY(%s)",  # noqa: S608 — liste blanche fixe
                    (old,),
                )
                deleted += cur.rowcount
    logger.info(
        "rétention agrégats (keep=%d%s) : patchs purgés %s, %d lignes supprimées",
        keep,
        ", dry-run" if dry_run else "",
        old or "aucun",
        deleted,
    )
    return {"purged_patches": old, "agg_rows_deleted": deleted}


_SCORE_TABLES = (
    "score_duo",
    "score_trio",
    "score_matchup",
    "score_win_factors",
    "score_champion_resilience",
)


def purge_stale_scores(
    *, keep: int = SCORE_KEEP_LABELS, dry_run: bool = False, dsn: str | None = None
) -> dict:
    """Purge `score_*` des `window_label` au-delà des `keep` plus récents.

    L'ordre de récence d'un label se lit sur son premier patch (le plus
    récent de la fenêtre, avant le premier "+").
    """
    if keep < 1:
        raise ValueError(f"keep doit être ≥ 1, reçu {keep}")
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        rows = conn.execute("SELECT DISTINCT window_label FROM score_trio").fetchall()
        known = sorted(
            (r[0] for r in rows), key=lambda lbl: patch_key(lbl.split("+")[0]), reverse=True
        )
        old = known[keep:]
        deleted = 0
        if old and not dry_run:
            for table in _SCORE_TABLES:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE window_label = ANY(%s)",  # noqa: S608
                    (old,),
                )
                deleted += cur.rowcount
    logger.info(
        "rétention scores (keep=%d%s) : fenêtres purgées %s, %d lignes supprimées",
        keep,
        ", dry-run" if dry_run else "",
        old or "aucune",
        deleted,
    )
    return {"purged_window_labels": old, "score_rows_deleted": deleted}


def run_daily(*, dry_run: bool = False, dsn: str | None = None) -> dict:
    """Toutes les purges à profondeur de patch (appelées une fois par jour)."""
    return {
        "participants": purge_stale_participants(dry_run=dry_run, dsn=dsn),
        "matches": purge_old_patches(dry_run=dry_run, dsn=dsn),
        "aggregates": purge_stale_aggregates(dry_run=dry_run, dsn=dsn),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.maintenance", description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="liste sans supprimer")
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    purge_stale_objective_events(dry_run=args.dry_run)
    run_daily(dry_run=args.dry_run)
    purge_stale_scores(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
