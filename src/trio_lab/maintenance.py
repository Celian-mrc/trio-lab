"""Rétention par patch : purge des données brutes des patchs anciens (Phase 6).

Seule opération destructive du projet : DELETE des `matches` (cascade sur
participants, trio_stats, objective_events) des patchs au-delà des `keep` plus
récents présents en base. Les tables agrégées (`agg_*`) et les scores
(`score_*`) sont CONSERVÉS : leur volumétrie est négligeable et ils gardent
l'historique gratuit. Le journal est conservé aussi — il empêche de
re-télécharger un match purgé si un joueur y renvoie.

Usage : `python -m trio_lab.maintenance [--keep 3] [--dry-run]`
(appelé quotidiennement par le mode service du collector).
"""

from __future__ import annotations

import argparse
import logging

import psycopg

from trio_lab import config, db
from trio_lab.synergy.windows import patch_key

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 3  # profondeur max d'une fenêtre multi-patchs


def purge_old_patches(*, keep: int = DEFAULT_KEEP, dry_run: bool = False, dsn: str | None = None):
    """Supprime les matchs des patchs au-delà des `keep` plus récents.

    Retourne `{"purged_patches": [...], "matches_deleted": n}`.
    """
    if keep < 1:
        raise ValueError(f"keep doit être ≥ 1, reçu {keep}")
    with psycopg.connect(db.require_dsn(dsn)) as conn, conn.transaction():
        rows = conn.execute("SELECT DISTINCT patch FROM matches").fetchall()
        known = sorted((r[0] for r in rows), key=patch_key, reverse=True)
        old = known[keep:]
        deleted = 0
        if old and not dry_run:
            cur = conn.execute("DELETE FROM matches WHERE patch = ANY(%s)", (old,))
            deleted = cur.rowcount
    logger.info(
        "rétention (keep=%d%s) : patchs purgés %s, %d matchs supprimés",
        keep,
        ", dry-run" if dry_run else "",
        old or "aucun",
        deleted,
    )
    return {"purged_patches": old, "matches_deleted": deleted}


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.maintenance", description=__doc__)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--dry-run", action="store_true", help="liste sans supprimer")
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    purge_old_patches(keep=args.keep, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
