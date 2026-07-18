"""Écritures Postgres du collector + archivage local des timelines brutes.

Toutes les fonctions async prennent une `psycopg.AsyncConnection` en
**autocommit** (cf. `db.connect`) : chaque instruction est atomique, et
l'écriture multi-tables d'un match ouvre explicitement sa transaction.

Idempotence : les PK (`puuid`, `match_id`, …) + `ON CONFLICT` rendent chaque
écriture rejouable — relancer une collecte ne duplique rien.

Les timelines brutes ne vont **jamais** en base (volumétrie Railway, invariant
du schéma 001) : elles sont archivées en JSON.gz local pour que la Phase 2
puisse extraire les stats trio sans re-payer l'appel API.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

import psycopg

from trio_lab.collector.ladder import PlayerRow

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 3


# --- players ---


async def upsert_players(conn: psycopg.AsyncConnection, rows: list[PlayerRow]) -> int:
    """Insère/rafraîchit les joueurs découverts. Retourne le nombre de lignes traitées.

    En cas de conflit on met à jour le tier/division (snapshot le plus récent)
    mais on préserve `matches_fetched_at` : le curseur de collecte survit aux
    redécouvertes périodiques.
    """
    if not rows:
        return 0
    async with conn.cursor() as cur:
        await cur.executemany(
            """
            INSERT INTO players (puuid, platform, routing, tier, division)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (puuid) DO UPDATE
              SET tier = EXCLUDED.tier, division = EXCLUDED.division
            """,
            [(r.puuid, r.platform, r.routing, r.tier, r.division) for r in rows],
        )
    return len(rows)


async def next_player(conn: psycopg.AsyncConnection, *, platform: str) -> str | None:
    """PUUID suivant dans la file de collecte : jamais scannés d'abord, puis les plus anciens."""
    cur = await conn.execute(
        """
        SELECT puuid FROM players
        WHERE platform = %s
        ORDER BY matches_fetched_at ASC NULLS FIRST, discovered_at ASC
        LIMIT 1
        """,
        (platform,),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def mark_player_fetched(conn: psycopg.AsyncConnection, puuid: str) -> None:
    """Repousse le joueur en fin de file après le scan de son historique."""
    await conn.execute("UPDATE players SET matches_fetched_at = now() WHERE puuid = %s", (puuid,))


async def unknown_puuids(conn: psycopg.AsyncConnection, puuids: list[str]) -> list[str]:
    """Parmi `puuids`, ceux absents de `players` — candidats à la récolte de participants.

    Filtre avant d'appeler league-v4-par-PUUID (`collect.py`) : évite de
    dépenser un appel API de vérification de rang sur un joueur déjà connu."""
    if not puuids:
        return []
    cur = await conn.execute("SELECT puuid FROM players WHERE puuid = ANY(%s)", (puuids,))
    known = {row[0] for row in await cur.fetchall()}
    return [p for p in puuids if p not in known]


# --- dédoublonnage / journal ---


async def filter_new_match_ids(conn: psycopg.AsyncConnection, match_ids: list[str]) -> list[str]:
    """Retire les match_ids déjà collectés ou définitivement jugés.

    Restent : les inconnus et les `error_retryable` (à retenter). L'ordre
    d'entrée est préservé.
    """
    if not match_ids:
        return []
    cur = await conn.execute(
        """
        SELECT match_id FROM matches WHERE match_id = ANY(%s)
        UNION
        SELECT match_id FROM match_fetch_journal
        WHERE match_id = ANY(%s) AND status IN ('excluded', 'error_permanent')
        """,
        (match_ids, match_ids),
    )
    done = {row[0] for row in await cur.fetchall()}
    return [m for m in match_ids if m not in done]


async def journal_exclusion(
    conn: psycopg.AsyncConnection, match_id: str, *, platform: str, reason: str
) -> None:
    """Marque un match exclu (critères d'inclusion ou parsing) — jamais retenté."""
    await conn.execute(
        """
        INSERT INTO match_fetch_journal (match_id, platform, status, reason)
        VALUES (%s, %s, 'excluded', %s)
        ON CONFLICT (match_id) DO UPDATE
          SET status = 'excluded', reason = EXCLUDED.reason, last_attempt = now()
        """,
        (match_id, platform, reason),
    )


async def journal_failure(
    conn: psycopg.AsyncConnection,
    match_id: str,
    *,
    platform: str,
    error: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    """Incrémente le compteur d'échecs ; bascule en permanent au seuil.

    Retourne le statut résultant (`error_retryable` ou `error_permanent`).
    """
    cur = await conn.execute(
        """
        INSERT INTO match_fetch_journal (match_id, platform, status, reason, attempts)
        VALUES (%s, %s, CASE WHEN %s <= 1 THEN 'error_permanent' ELSE 'error_retryable' END, %s, 1)
        ON CONFLICT (match_id) DO UPDATE
          SET attempts = match_fetch_journal.attempts + 1,
              status = CASE
                  WHEN match_fetch_journal.attempts + 1 >= %s THEN 'error_permanent'
                  ELSE 'error_retryable'
              END,
              reason = EXCLUDED.reason,
              last_attempt = now()
        RETURNING status
        """,
        (match_id, platform, max_attempts, error[:500], max_attempts),
    )
    row = await cur.fetchone()
    return row[0]


# --- matches ---


_TRIO_STATS_SQL = """
    INSERT INTO match_trio_stats (
        match_id, team_id, jgl_champion, mid_champion, sup_champion, win,
        gold_diff_5, gold_diff_10, gold_diff_15, gold_diff_20,
        gold_diff_25, gold_diff_30, gold_diff_35,
        grubs_taken, herald_taken, drakes_taken, soul_taken,
        nashor_first, nashor_first_s, first_tower, towers_destroyed, plates_taken,
        first_blood_trio, kill_participation_pre15, damage_share, vision_score, cc_time_s
    ) VALUES (
        %(match_id)s, %(team_id)s, %(jgl_champion)s, %(mid_champion)s, %(sup_champion)s,
        %(win)s,
        %(gold_diff_5)s, %(gold_diff_10)s, %(gold_diff_15)s, %(gold_diff_20)s,
        %(gold_diff_25)s, %(gold_diff_30)s, %(gold_diff_35)s,
        %(grubs_taken)s, %(herald_taken)s, %(drakes_taken)s,
        %(soul_taken)s,
        %(nashor_first)s, %(nashor_first_s)s, %(first_tower)s, %(towers_destroyed)s,
        %(plates_taken)s,
        %(first_blood_trio)s, %(kill_participation_pre15)s, %(damage_share)s,
        %(vision_score)s, %(cc_time_s)s
    )
    ON CONFLICT (match_id, team_id) DO NOTHING
"""

_OBJECTIVE_EVENTS_SQL = """
    INSERT INTO match_objective_events (match_id, seq, ts_s, event_type, subtype,
                                        team_id, pos_x, pos_y)
    VALUES (%(match_id)s, %(seq)s, %(ts_s)s, %(event_type)s, %(subtype)s,
            %(team_id)s, %(pos_x)s, %(pos_y)s)
    ON CONFLICT (match_id, seq) DO NOTHING
"""


async def _write_trio_stats(
    conn: psycopg.AsyncConnection,
    trio_stats: list[dict[str, Any]],
    objective_events: list[dict[str, Any]],
) -> None:
    """Écrit les lignes trio + events (à appeler dans une transaction ouverte)."""
    async with conn.cursor() as cur:
        if trio_stats:
            await cur.executemany(_TRIO_STATS_SQL, trio_stats)
        if objective_events:
            await cur.executemany(_OBJECTIVE_EVENTS_SQL, objective_events)


async def insert_match(
    conn: psycopg.AsyncConnection,
    row: dict[str, Any],
    participants: list[dict[str, Any]],
    trio_stats: list[dict[str, Any]] | None = None,
    objective_events: list[dict[str, Any]] | None = None,
) -> bool:
    """Insère un match complet (matches, participants, stats trio, events) en une transaction.

    Retourne False si le match était déjà en base (no-op, idempotence). Purge
    l'éventuelle entrée `error_retryable` du journal : l'échec est résolu.
    """
    async with conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO matches (match_id, platform, patch, game_version, queue_id,
                                 game_creation, game_duration_s, winning_team)
            VALUES (%(match_id)s, %(platform)s, %(patch)s, %(game_version)s, %(queue_id)s,
                    %(game_creation)s, %(game_duration_s)s, %(winning_team)s)
            ON CONFLICT (match_id) DO NOTHING
            RETURNING match_id
            """,
            row,
        )
        inserted = await cur.fetchone() is not None
        if not inserted:
            return False
        async with conn.cursor() as pcur:
            await pcur.executemany(
                """
                INSERT INTO match_participants (match_id, team_id, role, champion_id, win,
                                                cc_time_s, immobilizations)
                VALUES (%(match_id)s, %(team_id)s, %(role)s, %(champion_id)s, %(win)s,
                        %(cc_time_s)s, %(immobilizations)s)
                """,
                participants,
            )
        await _write_trio_stats(conn, trio_stats or [], objective_events or [])
        await conn.execute(
            "DELETE FROM match_fetch_journal WHERE match_id = %s", (row["match_id"],)
        )
    return True


async def insert_trio_stats(
    conn: psycopg.AsyncConnection,
    trio_stats: list[dict[str, Any]],
    objective_events: list[dict[str, Any]],
) -> None:
    """Ajoute les stats trio d'un match déjà en base (backfill Phase 2)."""
    async with conn.transaction():
        await _write_trio_stats(conn, trio_stats, objective_events)


# --- archive timeline (fichiers locaux, jamais en base) ---


def timeline_path(data_dir: Path, platform: str, patch: str, match_id: str) -> Path:
    """Chemin d'archive d'une timeline : data/raw/<platform>/<patch>/<id>.timeline.json.gz."""
    return data_dir / "raw" / platform / patch / f"{match_id}.timeline.json.gz"


def archive_timeline(
    data_dir: Path, platform: str, patch: str, match_id: str, timeline: dict[str, Any]
) -> Path:
    """Archive une timeline brute en JSON.gz (no-op si déjà présente)."""
    path = timeline_path(data_dir, platform, patch, match_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(timeline, fh, separators=(",", ":"))
    return path
