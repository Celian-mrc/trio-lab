"""Fiabilité empirique du CC par champion — ratio secondes-de-CC / immobilisation.

Complément de `ccref.score` (kit théorique) : certains champions gonflent
`timeCCingOthers` (Nocturne confirmé, cf. migration 011) sans immobiliser
réellement plus que la moyenne. Faute de détail par sort côté API Riot, on ne
peut pas isoler la cause exacte — on détecte l'anomalie via ce ratio et on
l'atténue proportionnellement (jamais de bonus, seulement un frein).
"""

from __future__ import annotations

import logging
import statistics

import psycopg

logger = logging.getLogger(__name__)

MIN_GAMES = 100
MIN_AVG_IMMO = 1.0  # évite le bruit des champions à immobilisations quasi nulles (ex. Teemo)
# Barrière de Tukey « far out » (3×IQR, plus stricte que le seuil usuel 1.5×IQR
# des outliers « légers ») : un simple écart à la médiane pénaliserait la
# moitié des champions pour un profil de kit normal (Ashe, Malzahar, Lulu...
# slows/silences non comptés comme immobilisation, ratio élevé mais légitime).
# Validé sur données prod (2026-07-12, 134 champions) : p95 = 4.6, Nocturne
# seul à 21.4 — un vrai outlier isolé, pas la simple queue de distribution.
OUTLIER_IQR_MULTIPLIER = 3.0


def compute_reliability(
    conn: psycopg.Connection, *, min_games: int = MIN_GAMES, min_avg_immo: float = MIN_AVG_IMMO
) -> dict[int, dict[str, float | int | None]]:
    """`{champion_id: {"reliability", "sec_per_immo", "games"}}` depuis `match_participants`.

    `reliability` ∈ (0, 1] : 1.0 = ratio dans la distribution normale (aucune
    correction) ou volume d'immobilisations insuffisant pour juger (bénéfice
    du doute) ; < 1.0 = ratio en dehors de la barrière de Tukey (outlier
    statistique confirmé, pas juste « au-dessus de la moyenne »).
    """
    rows = conn.execute(
        """
        SELECT champion_id, count(*) AS games,
               avg(cc_time_s) AS avg_cc, avg(immobilizations) AS avg_immo
        FROM match_participants
        WHERE cc_time_s IS NOT NULL AND immobilizations IS NOT NULL
        GROUP BY champion_id
        HAVING count(*) >= %s
        """,
        (min_games,),
    ).fetchall()

    ratios: dict[int, float] = {}
    games_by_champ: dict[int, int] = {}
    for champion_id, games, avg_cc, avg_immo in rows:
        games_by_champ[champion_id] = games
        if avg_immo >= min_avg_immo:
            ratios[champion_id] = float(avg_cc) / float(avg_immo)

    fence = None
    if len(ratios) >= 4:
        q1, _, q3 = statistics.quantiles(ratios.values(), n=4, method="inclusive")
        fence = q3 + OUTLIER_IQR_MULTIPLIER * (q3 - q1)

    result: dict[int, dict[str, float | int | None]] = {}
    for champion_id, games in games_by_champ.items():
        ratio = ratios.get(champion_id)
        no_correction = ratio is None or fence is None or ratio <= fence
        reliability = 1.0 if no_correction else fence / ratio
        result[champion_id] = {"reliability": reliability, "sec_per_immo": ratio, "games": games}
    return result


def backfill_trio_cc(conn: psycopg.Connection, *, patch: str | None = None) -> int:
    """Recalcule `match_trio_stats.cc_time_s` depuis `match_participants` corrigé.

    Limité aux matchs dont `match_participants` est encore présent (rétention
    plus courte que `match_trio_stats`) — au-delà, la donnée source est
    purgée et le biais s'estompe naturellement via la rétention des tables
    agrégées. `patch=None` : tous les matchs éligibles."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE match_trio_stats t
            SET cc_time_s = corrected.total
            FROM (
                SELECT p.match_id, p.team_id,
                       round(sum(p.cc_time_s * coalesce(r.reliability, 1.0)))::int AS total
                FROM match_participants p
                JOIN matches m ON m.match_id = p.match_id
                LEFT JOIN champion_cc_reliability r ON r.champion_id = p.champion_id
                WHERE p.role IN ('JUNGLE', 'MIDDLE', 'UTILITY')
                  AND p.cc_time_s IS NOT NULL
                  AND (%(patch)s::text IS NULL OR m.patch = %(patch)s::text)
                GROUP BY p.match_id, p.team_id
            ) corrected
            WHERE t.match_id = corrected.match_id AND t.team_id = corrected.team_id
            """,
            {"patch": patch},
        )
        n = cur.rowcount
    logger.info("match_trio_stats.cc_time_s recalculé pour %d équipes (patch=%s)", n, patch)
    return n
