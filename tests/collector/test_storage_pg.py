"""Tests d'intégration Postgres du module `storage` (+ migrations).

Utilisent la fixture `pg_conn` (conftest) : sautés si `TEST_DATABASE_URL` est
absente, tables tronquées avant chaque test. Ces tests prouvent les sémantiques
SQL réelles (ON CONFLICT, file NULLS FIRST, transitions du journal) que les
fakes de `test_collect.py` reproduisent.
"""

from __future__ import annotations

import pytest

from trio_lab import db
from trio_lab.collector import parsing, storage
from trio_lab.collector.ladder import PlayerRow
from trio_lab.stats import extract

from ..conftest import TEST_DSN
from ..stats._builders import build_timeline, monster
from ._builders import build_detail

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


def _player(puuid: str, tier: str = "EMERALD", division: str | None = "I") -> PlayerRow:
    return PlayerRow(puuid, "euw1", "europe", tier, division)


def _parsed(match_id: str = "EUW1_1"):
    detail = build_detail(match_id)
    return parsing.match_row(detail, platform="euw1"), parsing.participant_rows(detail)


# --- migrations ---


def test_migrations_are_idempotent(pg_conn):
    # La fixture les a déjà appliquées : une seconde passe est un no-op.
    assert db.apply_migrations(TEST_DSN) == []


# --- players ---


async def test_upsert_players_preserves_fetch_cursor(pg_conn):
    await storage.upsert_players(pg_conn, [_player("p1")])
    await storage.mark_player_fetched(pg_conn, "p1")
    # Redécouverte avec un tier rafraîchi : le curseur de collecte survit.
    await storage.upsert_players(pg_conn, [_player("p1", tier="DIAMOND", division="IV")])

    cur = await pg_conn.execute("SELECT tier, division, matches_fetched_at FROM players")
    tier, division, fetched_at = await cur.fetchone()
    assert (tier, division) == ("DIAMOND", "IV")
    assert fetched_at is not None


async def test_next_player_serves_never_scanned_first(pg_conn):
    await storage.upsert_players(pg_conn, [_player("p1"), _player("p2")])
    await storage.mark_player_fetched(pg_conn, "p1")
    assert await storage.next_player(pg_conn, platform="euw1") == "p2"
    await storage.mark_player_fetched(pg_conn, "p2")
    # File recyclée : le plus anciennement scanné revient en tête.
    assert await storage.next_player(pg_conn, platform="euw1") == "p1"
    assert await storage.next_player(pg_conn, platform="kr") is None


# --- matches : idempotence ---


async def test_insert_match_writes_participants_once(pg_conn):
    row, participants = _parsed()
    assert await storage.insert_match(pg_conn, row, participants) is True
    assert await storage.insert_match(pg_conn, row, participants) is False  # déjà en base

    cur = await pg_conn.execute("SELECT count(*) FROM match_participants")
    assert (await cur.fetchone())[0] == 10
    cur = await pg_conn.execute("SELECT patch, winning_team FROM matches")
    assert await cur.fetchall() == [("16.13", 100)]
    # CC empirique par participant (migration 005) — builder : cc = 2×pid.
    cur = await pg_conn.execute(
        "SELECT cc_time_s, immobilizations FROM match_participants"
        " WHERE team_id = 100 AND role = 'JUNGLE'"
    )
    assert await cur.fetchone() == (4, 2)


async def test_insert_match_writes_trio_stats_and_events(pg_conn):
    detail = build_detail("EUW1_1")
    timeline = build_timeline("EUW1_1", events=[monster("DRAGON", 200, 500, subtype="FIRE_DRAGON")])
    trio_stats, events = extract.extract_match(detail, timeline)
    row, participants = _parsed("EUW1_1")
    await storage.insert_match(pg_conn, row, participants, trio_stats, events)

    cur = await pg_conn.execute(
        "SELECT team_id, jgl_champion, gold_diff_10, kill_participation_pre15, drakes_taken"
        " FROM match_trio_stats ORDER BY team_id"
    )
    rows = await cur.fetchall()
    assert len(rows) == 2
    team_100, team_200 = rows
    assert team_100 == (100, 2, -150, None, 0)  # builder : diff = −15×minute, 0 kill <15
    assert team_200 == (200, 12, 150, None, 1)
    cur = await pg_conn.execute("SELECT event_type, subtype, team_id FROM match_objective_events")
    assert await cur.fetchall() == [("DRAGON", "infernal", 200)]


async def test_insert_match_writes_role_stats(pg_conn):
    detail = build_detail("EUW1_1")
    timeline = build_timeline("EUW1_1")
    trio_stats, events = extract.extract_match(detail, timeline)
    role_stats = extract.extract_role_stats(detail, timeline)
    row, participants = _parsed("EUW1_1")
    await storage.insert_match(pg_conn, row, participants, trio_stats, events, role_stats)

    cur = await pg_conn.execute("SELECT count(*) FROM match_role_stats")
    assert (await cur.fetchone())[0] == 10  # 5 rôles × 2 équipes

    cur = await pg_conn.execute(
        "SELECT champion_id, gold_10, cc_time_s FROM match_role_stats"
        " WHERE team_id = 100 AND role = 'JUNGLE'"
    )
    assert await cur.fetchone() == (2, 1020, 4)

    # Idempotent (ON CONFLICT) : rejouer ne duplique rien.
    await storage.insert_match(pg_conn, row, participants, trio_stats, events, role_stats)
    cur = await pg_conn.execute("SELECT count(*) FROM match_role_stats")
    assert (await cur.fetchone())[0] == 10


async def test_insert_trio_stats_backfills_existing_match(pg_conn):
    row, participants = _parsed("EUW1_1")
    await storage.insert_match(pg_conn, row, participants)  # sans stats trio (pré-Phase 2)

    trio_stats, events = extract.extract_match(build_detail("EUW1_1"), build_timeline("EUW1_1"))
    await storage.insert_trio_stats(pg_conn, trio_stats, events)
    await storage.insert_trio_stats(pg_conn, trio_stats, events)  # idempotent (ON CONFLICT)

    cur = await pg_conn.execute("SELECT count(*) FROM match_trio_stats")
    assert (await cur.fetchone())[0] == 2


# --- dédoublonnage / journal ---


async def test_filter_new_match_ids_semantics(pg_conn):
    row, participants = _parsed("EUW1_done")
    await storage.insert_match(pg_conn, row, participants)
    await storage.journal_exclusion(pg_conn, "EUW1_excl", platform="euw1", reason="queue")
    await storage.journal_failure(pg_conn, "EUW1_retry", platform="euw1", error="boom")

    todo = await storage.filter_new_match_ids(
        pg_conn, ["EUW1_done", "EUW1_excl", "EUW1_retry", "EUW1_new"]
    )
    # Restent : l'échec transitoire (retenté) et l'inconnu, dans l'ordre d'entrée.
    assert todo == ["EUW1_retry", "EUW1_new"]


async def test_journal_failure_becomes_permanent_at_threshold(pg_conn):
    statuses = [
        await storage.journal_failure(
            pg_conn, "EUW1_flaky", platform="euw1", error="boom", max_attempts=3
        )
        for _ in range(3)
    ]
    assert statuses == ["error_retryable", "error_retryable", "error_permanent"]

    cur = await pg_conn.execute("SELECT attempts FROM match_fetch_journal")
    assert (await cur.fetchone())[0] == 3


async def test_insert_match_purges_retryable_journal_entry(pg_conn):
    await storage.journal_failure(pg_conn, "EUW1_1", platform="euw1", error="boom")
    row, participants = _parsed("EUW1_1")
    await storage.insert_match(pg_conn, row, participants)

    cur = await pg_conn.execute("SELECT count(*) FROM match_fetch_journal")
    assert (await cur.fetchone())[0] == 0
