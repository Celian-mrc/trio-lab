"""Tests d'intégration des tables agrégées (migration 003 + refresh idempotent)."""

from __future__ import annotations

import pytest

from trio_lab.collector import parsing, storage
from trio_lab.stats import aggregate, extract

from ..collector._builders import build_detail
from ..conftest import TEST_DSN
from ._builders import build_timeline

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


async def _ingest(conn, match_id: str, *, winning_team: int) -> None:
    """Ingestion complète d'un match synthétique (même layout de champions)."""
    detail = build_detail(match_id, winning_team=winning_team)
    timeline = build_timeline(match_id)
    trio_stats, events = extract.extract_match(detail, timeline)
    await storage.insert_match(
        conn,
        parsing.match_row(detail, platform="euw1"),
        parsing.participant_rows(detail),
        trio_stats,
        events,
    )


async def test_refresh_counts_games_and_wins(pg_conn):
    # Deux matchs, mêmes trios (builder), une victoire par équipe.
    await _ingest(pg_conn, "EUW1_A", winning_team=100)
    await _ingest(pg_conn, "EUW1_B", winning_team=200)
    counts = aggregate.refresh("16.13", dsn=TEST_DSN)
    # 10 champions uniques (un par rôle/équipe), 3 duos × 2 équipes, 1 trio × 2 équipes,
    # 5 ennemis × 2 trios pour les counters, 2 alliés (Top/Bottom) × 2 trios pour les alliés.
    assert counts == {
        "agg_champion": 10,
        "agg_duo": 6,
        "agg_trio": 2,
        "agg_trio_vs_champion": 10,
        "agg_trio_with_ally": 4,
    }

    cur = await pg_conn.execute(
        "SELECT platform, games, wins FROM agg_trio"
        " WHERE jgl_champion = 2 AND mid_champion = 3 AND sup_champion = 5"
    )
    assert await cur.fetchall() == [("euw1", 2, 1)]

    # Sommes de stats (007) : le builder donne un score de vision déterministe
    # (participants 2/3/5 → 2+3+5 = 10 par match, 2 matchs).
    cur = await pg_conn.execute(
        "SELECT vision_sum, vision_n FROM agg_trio"
        " WHERE jgl_champion = 2 AND mid_champion = 3 AND sup_champion = 5"
    )
    assert await cur.fetchall() == [(20, 2)]

    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_duo WHERE roles = 'jgl_mid' AND champ_a = 12 AND champ_b = 13"
    )
    assert await cur.fetchall() == [(2, 1)]

    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_champion WHERE role = 'JUNGLE' AND champion_id = 2"
    )
    assert await cur.fetchall() == [(2, 1)]

    # Counters : le trio 100 (2/3/5) affronte le jungler adverse 12 dans les 2 matchs
    # et en gagne 1 ; les wins sont bien celles DU TRIO, pas de l'ennemi.
    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_trio_vs_champion"
        " WHERE jgl_champion = 2 AND enemy_role = 'JUNGLE' AND enemy_champion = 12"
    )
    assert await cur.fetchall() == [(2, 1)]

    # Alliés : le trio 100 (2/3/5) est accompagné du même Top (1) et ADC (4)
    # dans les 2 matchs (builder déterministe) et en gagne 1.
    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_trio_with_ally"
        " WHERE jgl_champion = 2 AND ally_role = 'TOP' AND ally_champion = 1"
    )
    assert await cur.fetchall() == [(2, 1)]


async def test_refresh_is_idempotent_per_patch(pg_conn):
    await _ingest(pg_conn, "EUW1_A", winning_team=100)
    first = aggregate.refresh("16.13", dsn=TEST_DSN)
    second = aggregate.refresh("16.13", dsn=TEST_DSN)  # DELETE + re-INSERT, pas de doublons
    assert first == second

    cur = await pg_conn.execute("SELECT count(*) FROM agg_trio")
    assert (await cur.fetchone())[0] == 2


async def test_refresh_other_patch_untouched(pg_conn):
    await _ingest(pg_conn, "EUW1_A", winning_team=100)
    aggregate.refresh("16.13", dsn=TEST_DSN)
    # Rafraîchir un autre patch ne touche pas les lignes du 16.13.
    aggregate.refresh("16.12", dsn=TEST_DSN)
    cur = await pg_conn.execute("SELECT count(*) FROM agg_trio WHERE patch = '16.13'")
    assert (await cur.fetchone())[0] == 2
