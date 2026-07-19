"""Orchestrateur de collecte (Phase 1) : découverte → file de joueurs → Postgres.

Pipeline par plateforme, les trois plateformes (na1/euw1/kr) tournant en
concurrence dans le même process — les budgets de rate-limit restent séparés
car le limiteur pulsefire compte par région de routage (americas/europe/asia).

Boucle d'une plateforme :
1. **découverte** : apex (rafraîchie toutes les APEX_DISCOVERY_TTL_S, peu
   coûteux) + Emerald/Diamond paginé (ENTRIES_DISCOVERY_TTL_S, coûteux — cf.
   ladder.py) → `players` ;
2. **file** : joueur le moins récemment scanné (`matches_fetched_at NULLS FIRST`) ;
3. **fan-out** : match_ids SoloQ bornés au patch, filtrés du déjà-fait ;
4. **téléchargement** : detail → inclusion → parsing → timeline → Postgres
   (+ archive JSON.gz), échecs journalisés ;
5. **récolte des participants** (`_harvest_participants`, session du
   18/07/2026) : les 10 PUUIDs d'un match déjà téléchargé sont gratuits
   (déjà dans le détail) — les inconnus sont vérifiés (rang Emerald+) via
   league-v4-par-PUUID et ajoutés au pool. Best-effort, jamais bloquant.

Reprenable et idempotent : tout l'état (joueurs, matchs, journal) vit en base ;
relancer ignore le déjà-fait. Le débit est cadencé par le rate-limit API (~1
match = 2 appels), le téléchargement est donc séquentiel par plateforme — la
concurrence utile est entre régions, pas dans la région.

Toute erreur de boucle (429 résiduel, timeout, **connexion Postgres coupée**)
déclenche une reconnexion avant de retenter : sans ça, une coupure complète
(ex. resize/restart du Postgres géré) bloquerait la boucle indéfiniment sur
la même connexion morte — vécu en prod le 11/07/2026.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from pathlib import Path

import psycopg

from trio_lab import config, db
from trio_lab.ccref.reliability import CC_TIME_RELIABILITY
from trio_lab.collector import inclusion, ladder, parsing, patches, storage
from trio_lab.collector.client import RiotClient
from trio_lab.stats import extract

logger = logging.getLogger(__name__)

# Période de rafraîchissement des ladders apex (challenger/GM/master) : peu
# coûteux (3 appels/plateforme), reste horaire.
APEX_DISCOVERY_TTL_S = 3600
# Période de rafraîchissement de la découverte paginée Emerald/Diamond :
# coûteuse (max_pages × 8 divisions appels/plateforme, cf. ladder.py), passée
# de horaire à quotidienne le 17/07/2026 avec le relèvement du plafond de
# pages — le mouvement de promotion est de toute façon lent, un rescan
# horaire n'apportait rien face à ce coût.
ENTRIES_DISCOVERY_TTL_S = 86400
# Heartbeat de log : état de la collecte tous les N matchs traités.
LOG_EVERY = 50
# Pause après une erreur de boucle (429 résiduel ayant épuisé les retries du
# middleware, réseau, base indisponible) avant de reprendre — un collector
# 24/24 ne meurt pas sur une erreur transitoire.
RETRY_PAUSE_S = 30


async def run(
    *,
    platforms: list[str],
    patch: str,
    target: int | None = None,
    max_pages: int = ladder.DEFAULT_MAX_PAGES,
    max_attempts: int = storage.DEFAULT_MAX_ATTEMPTS,
    data_dir: Path | None = None,
    dsn: str | None = None,
    strict_patch_bounds: bool = True,
) -> dict[str, int]:
    """Collecte sur plusieurs plateformes en concurrence. Retourne les compteurs agrégés.

    `target` = nombre de matchs téléchargés **par plateforme** avant arrêt ;
    `None` = boucle sans fin. `strict_patch_bounds=False` (mode service) :
    un patch absent de PATCH_DATES reçoit des bornes de repli au lieu
    d'échouer — le filtre `gameVersion` reste l'autorité.
    """
    # Échec immédiat si le patch n'est pas renseigné (sauf mode service).
    bounds = patches.bounds_for(patch) if strict_patch_bounds else patches.service_bounds_for(patch)
    epoch_bounds = tuple(patches.to_epoch_seconds(b) for b in bounds)
    resolved_dir = data_dir if data_dir is not None else config.DATA_DIR
    results = await asyncio.gather(
        *(
            _collect_platform(
                platform=p,
                patch=patch,
                target=target,
                max_pages=max_pages,
                max_attempts=max_attempts,
                data_dir=resolved_dir,
                dsn=dsn,
                epoch_bounds=epoch_bounds,
            )
            for p in platforms
        )
    )
    totals: Counter[str] = Counter()
    for counts in results:
        totals.update(counts)
    logger.info("collecte terminée : %s", dict(totals))
    return dict(totals)


async def _collect_platform(
    *,
    platform: str,
    patch: str,
    target: int | None,
    max_pages: int,
    max_attempts: int,
    data_dir: Path,
    dsn: str | None,
    epoch_bounds: tuple[int, int],
) -> Counter[str]:
    """Boucle de collecte d'une plateforme. Une connexion Postgres dédiée par boucle."""
    counts: Counter[str] = Counter()
    start_s, end_s = epoch_bounds
    last_apex_discovery = float("-inf")
    last_entries_discovery = float("-inf")
    conn = await db.connect(dsn)
    try:
        async with RiotClient() as client:
            while target is None or counts["downloaded"] < target:
                try:
                    if time.monotonic() - last_apex_discovery > APEX_DISCOVERY_TTL_S:
                        rows = await ladder.discover_apex(client, platform=platform)
                        await storage.upsert_players(conn, rows)
                        last_apex_discovery = time.monotonic()
                        logger.info("%s : découverte apex → %d joueurs", platform, len(rows))

                    if time.monotonic() - last_entries_discovery > ENTRIES_DISCOVERY_TTL_S:
                        rows = await ladder.discover_entries(
                            client, platform=platform, max_pages=max_pages
                        )
                        await storage.upsert_players(conn, rows)
                        last_entries_discovery = time.monotonic()
                        logger.info(
                            "%s : découverte Emerald/Diamond → %d joueurs", platform, len(rows)
                        )

                    puuid = await storage.next_player(conn, platform=platform)
                    if puuid is None:
                        logger.warning("%s : aucun joueur en file, arrêt", platform)
                        break

                    match_ids = await client.get_match_ids_by_puuid(
                        puuid, platform=platform, start_time=start_s, end_time=end_s
                    )
                    todo = await storage.filter_new_match_ids(conn, match_ids)
                    for match_id in todo:
                        if target is not None and counts["downloaded"] >= target:
                            break
                        await _process_match(
                            client,
                            conn,
                            platform=platform,
                            patch=patch,
                            match_id=match_id,
                            max_attempts=max_attempts,
                            data_dir=data_dir,
                            counts=counts,
                        )
                        processed = counts["downloaded"] + counts["excluded"] + counts["errors"]
                        if processed % LOG_EVERY == 0:
                            logger.info(
                                "%s : %d ok / %d exclus / %d erreurs / %d récoltés"
                                " (rate restant : %s)",
                                platform,
                                counts["downloaded"],
                                counts["excluded"],
                                counts["errors"],
                                counts["harvested"],
                                client.rate.remaining,
                            )
                    await storage.mark_player_fetched(conn, puuid)
                    counts["players_scanned"] += 1
                except Exception as exc:  # noqa: BLE001 — boucle 24/24 : pause et reprise
                    # Le joueur n'est pas marqué scanné : il sera retenté.
                    counts["loop_errors"] += 1
                    logger.warning(
                        "%s : erreur de boucle (%s), reprise dans %d s",
                        platform,
                        exc,
                        RETRY_PAUSE_S,
                    )
                    await asyncio.sleep(RETRY_PAUSE_S)
                    # Reconnexion : une connexion coupée (ex. resize/restart
                    # Postgres) ne se répare pas toute seule — sans ceci, TOUTES
                    # les tentatives suivantes échoueraient indéfiniment sur la
                    # même connexion morte (vécu en prod le 11/07/2026, boucle
                    # d'erreurs sans fin malgré la base redevenue disponible).
                    with contextlib.suppress(Exception):  # connexion déjà morte, sans importance
                        await conn.close()
                    try:
                        conn = await db.connect(dsn)
                    except Exception as reconnect_exc:  # noqa: BLE001 — retenté au tour suivant
                        logger.warning("%s : reconnexion échouée (%s)", platform, reconnect_exc)
    finally:
        await conn.close()
    logger.info("%s : fin de collecte, %s", platform, dict(counts))
    return counts


async def _process_match(
    client: RiotClient,
    conn: psycopg.AsyncConnection,
    *,
    platform: str,
    patch: str,
    match_id: str,
    max_attempts: int,
    data_dir: Path,
    counts: Counter[str],
) -> None:
    """Télécharge et ingère un match ; toute issue est journalisée, rien ne remonte."""
    try:
        detail = await client.get_match(match_id, platform=platform)
        included, reason = inclusion.is_included(detail, patch)
        if not included:
            await storage.journal_exclusion(conn, match_id, platform=platform, reason=reason)
            counts["excluded"] += 1
            return
        # Parsing AVANT l'appel timeline : ne pas dépenser une requête API sur
        # un match aux données dégradées (rôles incohérents → exclusion).
        row = parsing.match_row(detail, platform=platform)
        participants = parsing.participant_rows(detail)
        timeline = await client.get_match_timeline(match_id, platform=platform)
        trio_stats, objective_events = extract.extract_match(detail, timeline, CC_TIME_RELIABILITY)
        # Phase 7 (duo généralisé) : indépendant du trio, n'affecte jamais
        # match_trio_stats — alimente match_role_stats (5 rôles).
        role_stats = extract.extract_role_stats(detail, timeline, CC_TIME_RELIABILITY)
        # Archivage débrayable (ARCHIVE_TIMELINES=0) : sur Railway le
        # filesystem est éphémère, écrire des JSON.gz n'aurait aucun sens.
        if config.ARCHIVE_TIMELINES:
            storage.archive_timeline(data_dir, platform, patch, match_id, timeline)
        await storage.insert_match(
            conn, row, participants, trio_stats, objective_events, role_stats
        )
        counts["downloaded"] += 1
        # Récolte des participants (snowball, session du 18/07/2026) : les 10
        # PUUIDs du match sont déjà en main, gratuits — seule la vérification
        # de rang des inconnus coûte un appel. Best-effort : une panne ici ne
        # doit jamais faire échouer un match par ailleurs déjà ingéré.
        try:
            await _harvest_participants(client, conn, detail, platform=platform, counts=counts)
        except Exception as exc:  # noqa: BLE001 — récolte optionnelle, jamais bloquante
            logger.warning("%s : récolte participants échouée (%s)", match_id, exc)
    except parsing.ParseError as exc:
        await storage.journal_exclusion(conn, match_id, platform=platform, reason=f"parse: {exc}")
        counts["excluded"] += 1
    except Exception as exc:  # noqa: BLE001 — on journalise et on continue
        status = await storage.journal_failure(
            conn, match_id, platform=platform, error=str(exc), max_attempts=max_attempts
        )
        counts["errors"] += 1
        logger.warning("échec %s (%s) → %s", match_id, exc, status)


async def _harvest_participants(
    client: RiotClient,
    conn: psycopg.AsyncConnection,
    detail: dict,
    *,
    platform: str,
    counts: Counter[str],
) -> None:
    """Ajoute au pool les participants d'un match déjà téléchargé, encore inconnus et Emerald+.

    Un appel league-v4-par-PUUID par candidat inconnu (vérification de rang,
    cf. `ladder.player_row_from_entries`) ; les autres sont déjà filtrés sans
    appel API (`storage.unknown_puuids`). Une erreur sur un PUUID (compte
    supprimé, timeout) ne doit pas empêcher de traiter les autres.
    """
    candidates = await storage.unknown_puuids(conn, parsing.participant_puuids(detail))
    rows = []
    for puuid in candidates:
        try:
            entries = await client.get_league_entries_by_puuid(puuid, platform=platform)
        except Exception as exc:  # noqa: BLE001 — un PUUID en échec n'arrête pas les autres
            logger.debug("récolte %s : entrées league-v4 indisponibles (%s)", puuid, exc)
            continue
        row = ladder.player_row_from_entries(entries, puuid=puuid, platform=platform)
        if row is not None:
            rows.append(row)
    if rows:
        await storage.upsert_players(conn, rows)
        counts["harvested"] += len(rows)
