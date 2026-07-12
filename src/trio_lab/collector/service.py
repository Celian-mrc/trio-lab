"""Mode service 24/24 (Phase 6) : cycles batch → refresh des scores → rétention.

Orchestration SYNCHRONE : chaque cycle relance `asyncio.run` sur un batch fini
(`target` par plateforme), si bien qu'entre deux batchs le patch courant est
re-résolu via Data Dragon — le passage 16.13 → 16.14 ne demande aucune
intervention. Après chaque batch : agrégats du patch courant + scores de
synergie + counters + meilleurs alliés sur la fenêtre des patchs présents
dans `agg_trio` (≤ 3 —
volontairement PAS `matches`, pour que la profondeur statistique ne dépende
pas de la rétention brute), puis purge des scores à la fenêtre courante
(bon marché, faite à chaque cycle) et des events (cadence courte, jamais
relus après l'ingestion). Une fois par jour : purge à profondeur de patch
(participants, matchs bruts, agrégats — `trio_lab.maintenance.run_daily`).

Un cycle en échec (Data Dragon injoignable, base indisponible…) est loggé puis
retenté après une pause : le service ne meurt pas — même philosophie que la
boucle de collecte elle-même.
"""

from __future__ import annotations

import asyncio
import logging
import time

import psycopg

from trio_lab import db, maintenance
from trio_lab.collector import collect, patches
from trio_lab.stats import aggregate
from trio_lab.synergy import allies, compute, counters
from trio_lab.synergy.windows import PatchWindow, make_window, patch_key

logger = logging.getLogger(__name__)

DEFAULT_BATCH_TARGET = 5000  # matchs par plateforme et par batch
CYCLE_ERROR_PAUSE_S = 60
PURGE_INTERVAL_S = 24 * 3600
MAX_WINDOW_PATCHES = 3


def scoring_window(dsn: str | None = None) -> PatchWindow | None:
    """Fenêtre des patchs agrégés (≤ 3 plus récents), None si base vide.

    Lue depuis `agg_trio`, pas `matches` : la profondeur statistique de la
    fenêtre de synergie ne doit pas dépendre de la rétention des données
    brutes (`match_participants` peut être purgé bien avant `agg_trio`).
    """
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        rows = conn.execute("SELECT DISTINCT patch FROM agg_trio").fetchall()
    known = sorted((r[0] for r in rows), key=patch_key, reverse=True)[:MAX_WINDOW_PATCHES]
    return make_window(known) if known else None


def refresh_scores(patch: str, dsn: str | None = None) -> None:
    """Agrégats du patch courant + scores/counters/alliés de la fenêtre glissante.

    Purge aussi les scores hors fenêtre courante : bon marché (DELETE simple)
    et empêche `score_*` d'accumuler un nouveau doublon à chaque rollover.
    """
    aggregate.refresh(patch, dsn=dsn)
    window = scoring_window(dsn)
    if window is None:
        return
    compute.refresh(window, dsn=dsn)
    counters.refresh(window, dsn=dsn)
    allies.refresh(window, dsn=dsn)
    maintenance.purge_stale_scores(dsn=dsn)


def run_service(
    *,
    platforms: list[str],
    batch_target: int = DEFAULT_BATCH_TARGET,
    dsn: str | None = None,
    max_cycles: int | None = None,
) -> int:
    """Boucle de service. `max_cycles` (tests) : None = sans fin. Retourne les cycles."""
    last_daily_purge = float("-inf")
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            patch = patches.current_patch()
            logger.info("cycle %d : batch %s (%d/plateforme)", cycles, patch, batch_target)
            asyncio.run(
                collect.run(
                    platforms=platforms,
                    patch=patch,
                    target=batch_target,
                    dsn=dsn,
                    strict_patch_bounds=False,
                )
            )
            refresh_scores(patch, dsn=dsn)
            # events : jamais relus après l'ingestion, purge à chaque cycle
            # (cadence courte, indépendante de la purge quotidienne).
            maintenance.purge_stale_objective_events(dsn=dsn)
            if time.monotonic() - last_daily_purge > PURGE_INTERVAL_S:
                maintenance.run_daily(dsn=dsn)
                last_daily_purge = time.monotonic()
        except Exception:  # noqa: BLE001 — service 24/24 : log, pause, reprise
            logger.exception("cycle %d en échec, reprise dans %d s", cycles, CYCLE_ERROR_PAUSE_S)
            time.sleep(CYCLE_ERROR_PAUSE_S)
    return cycles
