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
    # même durée pour les 2 matchs (builder) donc 1 seule tranche par combo.
    assert counts == {
        "agg_champion": 10,
        "agg_duo": 6,
        "agg_trio": 2,
        "agg_trio_duration": 2,
        "agg_duo_duration": 6,
        "agg_matchup": 10,  # 5 rôles × 2 perspectives (champ_a/champ_b inversés)
    }

    cur = await pg_conn.execute(
        "SELECT platform, games, wins FROM agg_trio"
        " WHERE jgl_champion = 2 AND mid_champion = 3 AND sup_champion = 5"
    )
    assert await cur.fetchall() == [("euw1", 2, 1)]

    # Sommes de stats (007) : le builder donne un score de vision déterministe
    # (participants 2/3/5 → 2+3+5 = 10 par match, durée 1800 s = 30 min, donc
    # 10/30 par minute, 2 matchs) — vision_sum est PAR MINUTE, pas cumulé
    # (2026-07-13), pour ne pas favoriser les trios aux games plus longues.
    cur = await pg_conn.execute(
        "SELECT vision_sum, vision_n FROM agg_trio"
        " WHERE jgl_champion = 2 AND mid_champion = 3 AND sup_champion = 5"
    )
    [(vision_sum, vision_n)] = await cur.fetchall()
    assert vision_sum == pytest.approx(2 * (10 / 30))
    assert vision_n == 2

    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_duo WHERE roles = 'jgl_mid' AND champ_a = 12 AND champ_b = 13"
    )
    assert await cur.fetchall() == [(2, 1)]

    # Ventilation CC par membre (migration 020) : builder cc=2×pid, jgl(pid2)=4,
    # mid(pid3)=6, sup(pid5)=10, durée 30 min, 2 matchs, PAR MINUTE (comme le total).
    cur = await pg_conn.execute(
        "SELECT jgl_cc_sum, jgl_cc_n, mid_cc_sum, mid_cc_n, sup_cc_sum, sup_cc_n FROM agg_trio"
        " WHERE jgl_champion = 2 AND mid_champion = 3 AND sup_champion = 5"
    )
    [(jgl_sum, jgl_n, mid_sum, mid_n, sup_sum, sup_n)] = await cur.fetchall()
    assert (jgl_sum, jgl_n) == (pytest.approx(2 * (4 / 30)), 2)
    assert (mid_sum, mid_n) == (pytest.approx(2 * (6 / 30)), 2)
    assert (sup_sum, sup_n) == (pytest.approx(2 * (10 / 30)), 2)

    # Duo jgl_mid (champ_a=jgl=2, champ_b=mid=3) : reprend les mêmes colonnes
    # source que le trio, pas le total 3 membres.
    cur = await pg_conn.execute(
        "SELECT champ_a_cc_sum, champ_a_cc_n, champ_b_cc_sum, champ_b_cc_n FROM agg_duo"
        " WHERE roles = 'jgl_mid' AND champ_a = 2 AND champ_b = 3"
    )
    [(a_sum, a_n, b_sum, b_n)] = await cur.fetchall()
    assert (a_sum, a_n) == (pytest.approx(2 * (4 / 30)), 2)
    assert (b_sum, b_n) == (pytest.approx(2 * (6 / 30)), 2)

    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_champion WHERE role = 'JUNGLE' AND champion_id = 2"
    )
    assert await cur.fetchall() == [(2, 1)]


async def test_refresh_agg_matchup_counts_both_perspectives(pg_conn):
    """Counter 1v1 même rôle (migration 026) : auto-jointure match_participants,
    aucune dimension trio. Builder : team100 jungle=champ2, team200 jungle=champ12,
    déterministe sur les 2 matchs → champ2 gagne A (team100), perd B (team200)."""
    await _ingest(pg_conn, "EUW1_A", winning_team=100)
    await _ingest(pg_conn, "EUW1_B", winning_team=200)
    aggregate.refresh("16.13", dsn=TEST_DSN)

    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_matchup WHERE role = 'JUNGLE' AND champ_a = 2 AND champ_b = 12"
    )
    assert await cur.fetchall() == [(2, 1)]
    # Perspective inverse : mêmes 2 games, l'autre gagnant.
    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_matchup WHERE role = 'JUNGLE' AND champ_a = 12 AND champ_b = 2"
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


# --- agg_duo étendu (Phase 7, duo généralisé) : paires hors trio jgl/mid/sup ---


async def _ingest_with_role_stats(conn, match_id: str, *, winning_team: int) -> None:
    """Comme `_ingest`, plus `match_role_stats` (5 rôles) — pas branché par défaut
    dans `_ingest` pour ne pas changer les comptes des tests existants."""
    detail = build_detail(match_id, winning_team=winning_team)
    timeline = build_timeline(match_id)
    trio_stats, events = extract.extract_match(detail, timeline)
    role_stats = extract.extract_role_stats(detail, timeline)
    await storage.insert_match(
        conn,
        parsing.match_row(detail, platform="euw1"),
        parsing.participant_rows(detail),
        trio_stats,
        events,
        role_stats,
    )


async def test_refresh_agg_duo_ext_pairs_from_role_stats(pg_conn):
    # Un seul match : builder gold_at(minute, pid) = minute*(100+pid), team100
    # TOP=pid1/champ1, JUNGLE=pid2/champ2 ; team200 TOP=pid6/champ11,
    # JUNGLE=pid7/champ12 (cf. tests/collector/_builders.py).
    await _ingest_with_role_stats(pg_conn, "EUW1_A", winning_team=100)
    aggregate.refresh("16.13", dsn=TEST_DSN)

    cur = await pg_conn.execute(
        "SELECT games, wins, gold10_sum, gold10_n, cc_sum, cc_n,"
        " champ_a_cc_sum, champ_a_cc_n, champ_b_cc_sum, champ_b_cc_n"
        " FROM agg_duo WHERE roles = 'top_jgl' AND champ_a = 1 AND champ_b = 2"
    )
    row = await cur.fetchone()
    assert row is not None
    games, wins, gold10_sum, gold10_n, cc_sum, cc_n, a_sum, a_n, b_sum, b_n = row
    assert (games, wins) == (1, 1)  # team100 gagne
    # (1010+1020) − (1060+1070) = −100 (gold_10, cf. gold_at).
    assert (gold10_sum, gold10_n) == (pytest.approx(-100.0), 1)
    # cc_time_s = pid×2 (builder) : TOP(1)=2, JUNGLE(2)=4, durée 30 min.
    assert (cc_sum, cc_n) == (pytest.approx((2 + 4) / 30), 1)
    assert (a_sum, a_n) == (pytest.approx(2 / 30), 1)
    assert (b_sum, b_n) == (pytest.approx(4 / 30), 1)

    # Objectifs (team-level, lus depuis match_trio_stats) : le builder n'en pose
    # aucun, tout reste à 0/NULL — juste vérifier que la jointure ne plante pas.
    cur = await pg_conn.execute(
        "SELECT soul_sum, soul_n FROM agg_duo"
        " WHERE roles = 'bot_sup' AND champ_a = 4 AND champ_b = 5"
    )
    assert await cur.fetchone() == (0, 1)


async def test_refresh_duo_internal_pairs_use_pair_specific_gold_when_role_stats_available(
    pg_conn,
):
    """jgl_mid (paire INTERNE au trio, historiquement calculée sur le trio
    entier) : retour utilisateur 2026-07-20, doit désormais être
    pair-spécifique (2 membres seulement) dès que match_role_stats est
    disponible — même principe que top_jgl (paire étendue) juste au-dessus.
    Même match qu'au-dessus (team100 gagne)."""
    await _ingest_with_role_stats(pg_conn, "EUW1_A", winning_team=100)
    aggregate.refresh("16.13", dsn=TEST_DSN)

    cur = await pg_conn.execute(
        "SELECT gold10_sum, gold10_n, vision_sum, vision_n, cc_sum, cc_n"
        " FROM agg_duo WHERE roles = 'jgl_mid' AND champ_a = 2 AND champ_b = 3"
    )
    row = await cur.fetchone()
    assert row is not None
    gold10_sum, gold10_n, vision_sum, vision_n, cc_sum, cc_n = row
    # Jungle(pid2)+Mid(pid3) SEULS vs Jungle(pid7)+Mid(pid8) adverses :
    # (1020+1030) − (1070+1080) = −100 — PAS −150 (l'ancien calcul trio-wide
    # aurait inclus le support, pid5 vs pid10).
    assert (gold10_sum, gold10_n) == (pytest.approx(-100.0), 1)
    # visionScore (builder) = pid : jungle(2)+mid(3) = 5, pas 10 (avec le
    # support, pid5) — durée 30 min.
    assert (vision_sum, vision_n) == (pytest.approx(5 / 30), 1)
    # cc_time_s = pid×2 : jungle(4)+mid(6) = 10, pas 20 (avec le support,
    # pid5×2=10).
    assert (cc_sum, cc_n) == (pytest.approx(10 / 30), 1)


async def test_refresh_duo_internal_pairs_fall_back_to_trio_wide_gold_without_role_stats(
    pg_conn,
):
    """Sans match_role_stats (games antérieures à son déploiement, ou patch
    dont l'historique brut a été purgé) : retombe sur l'ancien calcul
    trio-wide (COALESCE côté SQL), ne perd JAMAIS les données historiques —
    volontairement pas d'INNER JOIN sur match_role_stats (cf. mémoire
    agg-matchup-backfill-gap : un INNER JOIN effacerait silencieusement
    tous les patchs antérieurs à son déploiement au prochain refresh)."""
    await _ingest(pg_conn, "EUW1_A", winning_team=100)  # pas de role_stats
    aggregate.refresh("16.13", dsn=TEST_DSN)

    cur = await pg_conn.execute(
        "SELECT gold10_sum, gold10_n FROM agg_duo"
        " WHERE roles = 'jgl_mid' AND champ_a = 2 AND champ_b = 3"
    )
    # (1020+1030+1050) − (1070+1080+1100) = −150 : le trio ENTIER (avec le
    # support), pas −100 (2 membres seuls) — comportement historique
    # préservé quand match_role_stats n'existe pas pour ce match.
    assert await cur.fetchone() == (pytest.approx(-150.0), 1)


async def test_refresh_agg_duo_duration_ext_pairs_from_role_stats(pg_conn):
    """Régression (retour utilisateur 2026-07-20) : agg_duo_duration
    (source du score de scaling) ne couvrait QUE les 3 paires historiques
    jgl_mid/jgl_sup/mid_sup — les 7 paires étendues (ex. bot_sup) restaient
    à 0 ligne quel que soit le volume de games, donc `scaling` toujours
    NULL pour elles. `_DUO_DURATION_EXT_SQL` corrige ça."""
    await _ingest_with_role_stats(pg_conn, "EUW1_A", winning_team=100)
    aggregate.refresh("16.13", dsn=TEST_DSN)

    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_duo_duration"
        " WHERE roles = 'bot_sup' AND champ_a = 4 AND champ_b = 5"
    )
    assert await cur.fetchall() == [(1, 1)]  # team100 gagne (builder)
    cur = await pg_conn.execute(
        "SELECT games, wins FROM agg_duo_duration"
        " WHERE roles = 'top_jgl' AND champ_a = 1 AND champ_b = 2"
    )
    assert await cur.fetchall() == [(1, 1)]
