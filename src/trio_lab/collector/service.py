"""Mode service 24/24 (Phase 6) : cycles batch → refresh des scores → rétention.

Orchestration SYNCHRONE : chaque cycle relance `asyncio.run` sur un batch fini
(`target` par plateforme), si bien qu'entre deux batchs le patch courant est
re-résolu via Data Dragon — le passage 16.13 → 16.14 ne demande aucune
intervention. Après chaque batch, AU PLUS une fois par `SCORE_REFRESH_THROTTLE_S`
(pas à chaque cycle, retour utilisateur 2026-08-21 — cf. commentaire de la
constante) : agrégats du patch courant + scores de synergie + matchups 1v1
sur la fenêtre des patchs présents dans `agg_trio` (≤ 3 — volontairement PAS
`matches`, pour que la profondeur statistique ne dépende pas de la
rétention brute), puis purge des scores à la fenêtre courante. La purge des
events (cadence courte, jamais relus après l'ingestion), elle, reste à
chaque cycle. Une fois par jour : purge à profondeur de patch (participants,
matchs bruts, agrégats — `trio_lab.maintenance.run_daily`).

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
from trio_lab.synergy import compute, draft_suggestions, flex, matchups, resilience
from trio_lab.synergy.windows import PatchWindow, make_window, patch_key

logger = logging.getLogger(__name__)

# Abaissé de 5000 à 500 le 2026-08-01 (retour utilisateur) : `collect.run`
# attend que TOUTES les plateformes atteignent `target` NOUVEAUX matchs
# (asyncio.gather) avant que `refresh_scores` tourne UNE SEULE FOIS en fin
# de cycle — une seule région lente (rate limit Riot, réserve de matchs
# neufs qui s'épuise à mesure que le "déjà connu" grossit) bloque tout le
# cycle, potentiellement pendant des heures, pendant que le site sert des
# scores de plus en plus périmés. Un batch 10× plus petit boucle ~10× plus
# vite : `refresh_scores` (agrégats + scores + suggestions, ~1-3 min mesuré
# sur la fenêtre courante) se déclenche bien plus souvent pour un coût
# cumulé négligeable — la fraîcheur du site ne dépend plus du pire cas
# d'une seule région.
DEFAULT_BATCH_TARGET = 500  # matchs par plateforme et par batch
CYCLE_ERROR_PAUSE_S = 60
PURGE_INTERVAL_S = 24 * 3600
# Abaissé de 3 à 2 le 2026-08-01 en mitigation temporaire pendant l'OOM en
# prod (`compute._load`/`compute.refresh` chargeaient `agg_duo`/`agg_trio`
# intégralement en dicts Python), remonté à 3 le 2026-08-02 une fois la
# lecture paginée (`compute._iter_agg_groups`) validée en prod sans crash.
MAX_WINDOW_PATCHES = 3
# Espacement de TOUT le pipeline de scores (agrégats + compute + matchups +
# résilience + flex + draft_suggestions) — retour utilisateur 2026-08-19/21,
# incident Disk IO Budget puis instance Supabase complètement figée. Un
# premier fix (throttle resilience/flex seules à 6h) n'a pas suffi :
# `aggregate.refresh` elle-même — le TOUT PREMIER appel de ce pipeline,
# jamais throttlé — s'est avérée la plus grosse dépense, mesurée à ~11 min
# de bout en bout sur la prod (DELETE+INSERT complet du patch courant sur 6
# tables, `agg_duo` seule à 376s à cause de 4 jointures vers
# `match_role_stats`, 12,2M lignes). Optimisée une première fois (`team_gold_agg`
# matérialisée une fois au lieu de 3x, filtrée par patch — cf.
# `stats/aggregate.py`), mais le total reste substantiel : retour utilisateur,
# 2x/jour de fraîcheur suffit largement (pas besoin de creuser plus le plan
# de requête d'agg_duo, plus risqué à toucher — `enable_nestloop = off` y
# est déjà nécessaire pour un AUTRE piège, cf. mémoire
# postgres-cte-selfjoin-nestloop-trap). Donc : tout le pipeline (pas
# seulement resilience/flex) throttlé ensemble à 12h — `compute`/`matchups`
# retraiteraient de toute façon les MÊMES données tant que `aggregate.refresh`
# n'a pas tourné, les faire à chaque cycle sans lui serait juste du travail
# perdu. Même mécanique TTL que
# `collect.APEX_DISCOVERY_TTL_S`/`ENTRIES_DISCOVERY_TTL_S`.
SCORE_REFRESH_THROTTLE_S = 12 * 3600


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


def refresh_scores(
    patch: str, dsn: str | None = None, *, refresh_state: dict[str, float] | None = None
) -> None:
    """Agrégats du patch courant + scores de synergie de la fenêtre glissante.

    Purge aussi les scores hors fenêtre courante : bon marché (DELETE simple)
    et empêche `score_*` d'accumuler un nouveau doublon à chaque rollover.

    `refresh_state` : horodatage du dernier refresh complet, MUTÉ EN PLACE et
    réutilisé entre cycles par `run_service` (même mécanique que
    `discovery_state` côté collecte) — tout le pipeline throttlé à
    `SCORE_REFRESH_THROTTLE_S` plutôt qu'à chaque cycle (retour utilisateur
    2026-08-19/21, cf. commentaire de la constante). `None` (défaut, ex.
    appel manuel/CLI/tests) : dict éphémère créé à chaque appel → toujours
    rafraîchi, comportement inchangé pour ces usages.
    """
    state = refresh_state if refresh_state is not None else {}
    last_refresh = state.get("scores", float("-inf"))
    if time.monotonic() - last_refresh <= SCORE_REFRESH_THROTTLE_S:
        return
    aggregate.refresh(patch, dsn=dsn)
    window = scoring_window(dsn)
    if window is not None:
        compute.refresh(window, dsn=dsn)
        matchups.refresh(window, dsn=dsn)
        # `min_rows` en dessous du seuil : no-op silencieux (résilience).
        resilience.refresh(window, dsn=dsn)
        flex.refresh(window, dsn=dsn)
        # Compositions suggérées + contres précalculées pour "toutes régions"
        # UNIQUEMENT (retour utilisateur 2026-07-25) : la région par défaut à
        # l'arrivée sur /draft (le plus de games) — les autres régions
        # restent en calcul à la demande (bouton), coût mesuré ~30-47s par
        # région, pas justifié pour un usage bien plus rare.
        draft_suggestions.refresh(window, "all", dsn=dsn)
        maintenance.purge_stale_scores(dsn=dsn)
    state["scores"] = time.monotonic()


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
    # Horodatages de découverte apex/entries PAR PLATEFORME, créés UNE FOIS
    # avant la boucle et réutilisés à chaque cycle (retour utilisateur
    # 2026-08-01) : `collect.run` reçoit le MÊME dict à chaque appel, sinon
    # chaque cycle réinitialiserait ces horodatages et refait la découverte
    # Emerald/Diamond (coûteuse) à chaque cycle au lieu d'une fois par
    # `ENTRIES_DISCOVERY_TTL_S` — cause de l'OOM vu en prod une fois les
    # cycles raccourcis par `DEFAULT_BATCH_TARGET`.
    discovery_state: dict[str, dict[str, float]] = {}
    # Même principe : créé UNE FOIS, muté en place par `refresh_scores` d'un
    # cycle à l'autre pour throttler tout le pipeline de scores (cf.
    # `SCORE_REFRESH_THROTTLE_S`).
    refresh_state: dict[str, float] = {}
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
                    discovery_state=discovery_state,
                )
            )
            refresh_scores(patch, dsn=dsn, refresh_state=refresh_state)
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
