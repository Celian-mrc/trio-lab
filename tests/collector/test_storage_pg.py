"""Tests d'intégration Postgres du module `storage` (+ migrations).

Sautés si `TEST_DATABASE_URL` n'est pas définie. Utiliser une base JETABLE :
chaque test tronque `players`, `matches` (cascade participants) et le journal.
Ces tests prouvent les sémantiques SQL réelles (ON CONFLICT, file NULLS FIRST,
transitions du journal) que les fakes de `test_collect.py` reproduisent.
"""

from __future__ import annotations

import os

import pytest

from trio_lab import db
from trio_lab.collector import parsing, storage
from trio_lab.collector.ladder import PlayerRow

from ._builders import build_detail

DSN = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


@pytest.fixture
async def conn():
    db.apply_migrations(DSN)
    aconn = await db.connect(DSN)
    await aconn.execute("TRUNCATE players, matches, match_fetch_journal CASCADE")
    try:
        yield aconn
    finally:
        await aconn.close()


def _player(puuid: str, tier: str = "EMERALD", division: str | None = "I") -> PlayerRow:
    return PlayerRow(puuid, "euw1", "europe", tier, division)


def _parsed(match_id: str = "EUW1_1"):
    detail = build_detail(match_id)
    return parsing.match_row(detail, platform="euw1"), parsing.participant_rows(detail)


# --- migrations ---


def test_migrations_are_idempotent():
    assert db.apply_migrations(DSN) == []  # déjà appliquées par la fixture ou un run précédent


# --- players ---


async def test_upsert_players_preserves_fetch_cursor(conn):
    await storage.upsert_players(conn, [_player("p1")])
    await storage.mark_player_fetched(conn, "p1")
    # Redécouverte avec un tier rafraîchi : le curseur de collecte survit.
    await storage.upsert_players(conn, [_player("p1", tier="DIAMOND", division="IV")])

    cur = await conn.execute("SELECT tier, division, matches_fetched_at FROM players")
    tier, division, fetched_at = await cur.fetchone()
    assert (tier, division) == ("DIAMOND", "IV")
    assert fetched_at is not None


async def test_next_player_serves_never_scanned_first(conn):
    await storage.upsert_players(conn, [_player("p1"), _player("p2")])
    await storage.mark_player_fetched(conn, "p1")
    assert await storage.next_player(conn, platform="euw1") == "p2"
    await storage.mark_player_fetched(conn, "p2")
    # File recyclée : le plus anciennement scanné revient en tête.
    assert await storage.next_player(conn, platform="euw1") == "p1"
    assert await storage.next_player(conn, platform="kr") is None


# --- matches : idempotence ---


async def test_insert_match_writes_participants_once(conn):
    row, participants = _parsed()
    assert await storage.insert_match(conn, row, participants) is True
    assert await storage.insert_match(conn, row, participants) is False  # déjà en base

    cur = await conn.execute("SELECT count(*) FROM match_participants")
    assert (await cur.fetchone())[0] == 10
    cur = await conn.execute("SELECT patch, winning_team FROM matches")
    assert await cur.fetchall() == [("16.13", 100)]


# --- dédoublonnage / journal ---


async def test_filter_new_match_ids_semantics(conn):
    row, participants = _parsed("EUW1_done")
    await storage.insert_match(conn, row, participants)
    await storage.journal_exclusion(conn, "EUW1_excl", platform="euw1", reason="queue")
    await storage.journal_failure(conn, "EUW1_retry", platform="euw1", error="boom")

    todo = await storage.filter_new_match_ids(
        conn, ["EUW1_done", "EUW1_excl", "EUW1_retry", "EUW1_new"]
    )
    # Restent : l'échec transitoire (retenté) et l'inconnu, dans l'ordre d'entrée.
    assert todo == ["EUW1_retry", "EUW1_new"]


async def test_journal_failure_becomes_permanent_at_threshold(conn):
    statuses = [
        await storage.journal_failure(
            conn, "EUW1_flaky", platform="euw1", error="boom", max_attempts=3
        )
        for _ in range(3)
    ]
    assert statuses == ["error_retryable", "error_retryable", "error_permanent"]

    cur = await conn.execute("SELECT attempts FROM match_fetch_journal")
    assert (await cur.fetchone())[0] == 3


async def test_insert_match_purges_retryable_journal_entry(conn):
    await storage.journal_failure(conn, "EUW1_1", platform="euw1", error="boom")
    row, participants = _parsed("EUW1_1")
    await storage.insert_match(conn, row, participants)

    cur = await conn.execute("SELECT count(*) FROM match_fetch_journal")
    assert (await cur.fetchone())[0] == 0
