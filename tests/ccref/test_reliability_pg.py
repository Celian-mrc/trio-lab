"""Tests d'intégration de `ccref.reliability` (Postgres de test réel, connexion sync).

`compute_reliability`/`backfill_trio_cc` prennent une connexion psycopg SYNC
(cohérent avec `ccref.score`/`synergy.compute`) — fixture locale plutôt que
`pg_conn` (async, cf. conftest), même pattern que `tests/web/test_app_pg.py`.
"""

from __future__ import annotations

import statistics

import psycopg
import pytest

from trio_lab import db
from trio_lab.ccref import reliability

from ..conftest import TEST_DSN

pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL absente (Postgres de test requis)"
)


@pytest.fixture
def pg_sync():
    db.apply_migrations(TEST_DSN)
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE players, matches, match_fetch_journal,"
            " agg_champion, agg_duo, agg_trio, agg_trio_vs_champion,"
            " score_duo, score_trio, score_trio_vs_champion,"
            " champion_cc_theoretical, champion_cc_reliability CASCADE"
        )
        yield conn


def _seed_participants(conn, rows: list[tuple[int, int, float, float]]) -> None:
    """`rows` : (champion_id, n_games, avg_cc, avg_immo) — un match_id dédié par ligne
    de champion, `n_games` participants identiques pour atteindre le volume voulu."""
    match_n = 0
    for champion_id, n_games, avg_cc, avg_immo in rows:
        for _ in range(n_games):
            match_n += 1
            match_id = f"EUW1_{champion_id}_{match_n}"
            conn.execute(
                "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
                " game_creation, game_duration_s, winning_team)"
                " VALUES (%s, 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)",
                (match_id,),
            )
            conn.execute(
                "INSERT INTO match_participants (match_id, team_id, role, champion_id, win,"
                " cc_time_s, immobilizations)"
                " VALUES (%s, 100, 'JUNGLE', %s, true, %s, %s)",
                (match_id, champion_id, round(avg_cc), round(avg_immo)),
            )


def test_compute_reliability_flags_outlier_ratio(pg_sync):
    """Barrière de Tukey (Q3 + 3×IQR) : il faut un échantillon assez large pour
    une estimation de quartiles stable (4 champions seulement rend Q3 instable
    et absorbe l'outlier lui-même — vécu en développant ce test). 8 champions
    « normaux » (ratio 0.5 à 2.3, distribution resserrée et réaliste) + 1
    Nocturne extrême (21.5, cf. donnée prod du 2026-07-12) : seul Nocturne
    doit être atténué, tous les autres restent à 1.0 (aucun faux positif sur
    un profil de kit légitimement CC-heavy, ex. Ashe/Malzahar en prod)."""
    normal_ratios = (0.5, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.3)
    # avg_cc constant (100) pour tous les champions « normaux » : seul
    # avg_immo varie, ce qui limite le bruit d'arrondi (SMALLINT en base) sur
    # le ratio stocké par rapport à `normal_ratios` — la barrière attendue
    # est ensuite recalculée depuis les ratios réellement observés en base,
    # pas depuis les valeurs d'entrée, pour rester robuste à cet arrondi.
    normal = [(i, 200, 100.0, 100.0 / ratio) for i, ratio in enumerate(normal_ratios, start=1)]
    _seed_participants(pg_sync, [*normal, (56, 200, 2150.0, 100.0)])  # 56 = Nocturne, ratio 21.5
    result = reliability.compute_reliability(pg_sync, min_games=100)
    for champ_id, _, _, _ in normal:
        assert result[champ_id]["reliability"] == pytest.approx(1.0)
    # Quartiles calculés sur TOUS les champions vus (56 inclus, comme le fait
    # compute_reliability lui-même) — sinon la barrière attendue diverge.
    observed_ratios = [v["sec_per_immo"] for v in result.values()]
    q1, _, q3 = statistics.quantiles(observed_ratios, n=4, method="inclusive")
    fence = q3 + 3 * (q3 - q1)
    assert result[56]["reliability"] == pytest.approx(fence / result[56]["sec_per_immo"], rel=1e-3)


def test_compute_reliability_ignores_low_volume_and_low_immo(pg_sync):
    # Volume insuffisant (< min_games) : absent du résultat.
    _seed_participants(pg_sync, [(99, 10, 50.0, 1.0)])
    result = reliability.compute_reliability(pg_sync, min_games=50)
    assert result == {}

    # Volume suffisant mais immo quasi nulle (bruit, type Teemo) : reliability
    # par défaut (bénéfice du doute), pas de ratio calculé.
    _seed_participants(pg_sync, [(17, 50, 40.0, 0.1)])
    result = reliability.compute_reliability(pg_sync, min_games=50)
    assert result[17]["reliability"] == pytest.approx(1.0)
    assert result[17]["sec_per_immo"] is None


def test_backfill_trio_cc_applies_reliability_to_stored_trio_sum(pg_sync):
    # Trio jgl(56)=Nocturne, mid(2), sup(3) — un seul match, on vérifie que
    # match_trio_stats.cc_time_s est recalculé à partir de match_participants
    # corrigé, pas juste copié.
    pg_sync.execute(
        "INSERT INTO matches (match_id, platform, patch, game_version, queue_id,"
        " game_creation, game_duration_s, winning_team)"
        " VALUES ('EUW1_BF1', 'euw1', '16.13', '16.13.1', 420, now(), 1800, 100)"
    )
    for role, champ, cc in (("JUNGLE", 56, 200), ("MIDDLE", 2, 40), ("UTILITY", 3, 20)):
        pg_sync.execute(
            "INSERT INTO match_participants (match_id, team_id, role, champion_id, win,"
            " cc_time_s, immobilizations) VALUES ('EUW1_BF1', 100, %s, %s, true, %s, 5)",
            (role, champ, cc),
        )
    pg_sync.execute(
        "INSERT INTO match_trio_stats (match_id, team_id, jgl_champion, mid_champion,"
        " sup_champion, win, cc_time_s) VALUES ('EUW1_BF1', 100, 56, 2, 3, true, 260)"
    )
    pg_sync.execute(
        "INSERT INTO champion_cc_reliability (champion_id, reliability, sec_per_immo, games)"
        " VALUES (56, 0.25, 40.0, 500)"
    )
    n = reliability.backfill_trio_cc(pg_sync)
    assert n == 1
    corrected = pg_sync.execute(
        "SELECT cc_time_s FROM match_trio_stats WHERE match_id = 'EUW1_BF1' AND team_id = 100"
    ).fetchone()[0]
    # 200×0.25 + 40×1.0 + 20×1.0 = 110 (arrondi).
    assert corrected == 110
