"""Validation du score CC théorique par corrélation avec le CC empirique.

Compare, champion par champion, le score théorique (kit, `ccref.score`) à la
moyenne de `timeCCingOthers` par partie (match_participants.cc_time_s,
collecté depuis la migration 005). Corrélations de Pearson (linéaire) et de
Spearman (rangs — la plus pertinente : les échelles diffèrent).

Usage : `python -m trio_lab.ccref.validate [--min-games 30]`
"""

from __future__ import annotations

import argparse
import logging
import math

import psycopg

from trio_lab import config, db
from trio_lab.ccref import champions, score

logger = logging.getLogger(__name__)

DEFAULT_MIN_GAMES = 30


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    var_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    var_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if var_x == 0.0 or var_y == 0.0:
        return 0.0
    return cov / (var_x * var_y)


def _ranks(values: list[float]) -> list[float]:
    """Rangs moyens (gestion des ex æquo)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = mean_rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    return pearson(_ranks(xs), _ranks(ys))


def real_cc_by_champion(conn: psycopg.Connection, min_games: int) -> dict[int, tuple[int, float]]:
    """`{champion_id: (games, moyenne de timeCCingOthers par partie)}`."""
    rows = conn.execute(
        """
        SELECT champion_id, count(*), avg(cc_time_s)
        FROM match_participants
        WHERE cc_time_s IS NOT NULL
        GROUP BY champion_id
        HAVING count(*) >= %s
        """,
        (min_games,),
    ).fetchall()
    return {champ: (games, float(avg)) for champ, games, avg in rows}


def run(*, min_games: int = DEFAULT_MIN_GAMES, dsn: str | None = None) -> dict:
    """Construit le rapport de corrélation. Retourne un dict (affiché par le CLI)."""
    theoretical_by_name = score.champion_scores()
    name_to_id = champions.fetch_name_to_id()
    theoretical: dict[int, tuple[str, float]] = {}
    unmatched: list[str] = []
    for name, value in theoretical_by_name.items():
        champ_id = champions.resolve(name, name_to_id)
        if champ_id is None:
            unmatched.append(name)
        else:
            theoretical[champ_id] = (name, value)
    if unmatched:
        logger.warning("noms sans championId Data Dragon : %s", unmatched)

    with psycopg.connect(db.require_dsn(dsn)) as conn:
        real = real_cc_by_champion(conn, min_games)

    common = sorted(set(theoretical) & set(real))
    xs = [theoretical[c][1] for c in common]
    ys = [real[c][1] for c in common]
    deltas = sorted(
        (
            {
                "champion": theoretical[c][0],
                "theorique": round(theoretical[c][1], 2),
                "reel_avg_s": round(real[c][1], 2),
                "games": real[c][0],
                # écart de rangs normalisé : + = théorique surestime, − = sous-estime
                "ecart_rangs": 0.0,
            }
            for c in common
        ),
        key=lambda d: d["theorique"],
    )
    rank_theo = _ranks([d["theorique"] for d in deltas])
    rank_real = _ranks([d["reel_avg_s"] for d in deltas])
    for d, rt, rr in zip(deltas, rank_theo, rank_real, strict=True):
        d["ecart_rangs"] = round((rt - rr) / len(deltas), 3)
    deltas.sort(key=lambda d: d["ecart_rangs"])

    return {
        "n_champions": len(common),
        "min_games": min_games,
        "pearson": round(pearson(xs, ys), 3) if len(common) >= 3 else None,
        "spearman": round(spearman(xs, ys), 3) if len(common) >= 3 else None,
        "sous_estimes": deltas[:5],  # réel ≫ théorique
        "surestimes": deltas[-5:][::-1],  # théorique ≫ réel
        "non_mappes": unmatched,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.ccref.validate", description=__doc__)
    parser.add_argument("--min-games", type=int, default=DEFAULT_MIN_GAMES)
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    report = run(min_games=args.min_games)
    print(f"champions comparés : {report['n_champions']} (≥ {report['min_games']} games)")
    print(f"corrélation de Pearson  : {report['pearson']}")
    print(f"corrélation de Spearman : {report['spearman']}")
    print("-- les plus SOUS-estimés par le score théorique (réel ≫ théorique) --")
    for d in report["sous_estimes"]:
        print(
            f"  {d['champion']:15} théo={d['theorique']:6} réel={d['reel_avg_s']:6}s"
            f" ({d['games']} games)"
        )
    print("-- les plus SURestimés par le score théorique --")
    for d in report["surestimes"]:
        print(
            f"  {d['champion']:15} théo={d['theorique']:6} réel={d['reel_avg_s']:6}s"
            f" ({d['games']} games)"
        )


if __name__ == "__main__":
    main()
