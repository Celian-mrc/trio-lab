"""Matérialisation des counters : agg_trio(_vs_champion) → score_trio_vs_champion.

Le score de counter est un delta : WR(trio face à l'ennemi X) − WR global du
trio, les deux pondérés sur la MÊME fenêtre multi-patchs (PROJECT.md : une
différence se calcule à termes comparables). Négatif = « ce trio souffre
contre X ». La coupure de rework s'applique aux 4 champions concernés — un
ennemi retravaillé n'est plus le même matchup.

Le delta publié est rétréci vers 0 (prior neutre : pas d'effet counter tant
que le volume ne le prouve pas) : `delta = n·delta_raw / (n + k)`, même
mécanique et même k que le lissage des synergies. Jamais de counters trio vs
trio (CLAUDE.md) : le grain ennemi est un champion dans un rôle.

Idempotent par fenêtre via UPSERT, pas DELETE+INSERT (mêmes raisons que
`synergy/compute.py` — l'ensemble des clés d'une fenêtre ne fait que
grossir d'un cycle à l'autre, cf. mémoire `supabase-disk-growth`).
"""

from __future__ import annotations

import logging
from collections import defaultdict

import psycopg

from trio_lab import db
from trio_lab.synergy import scores
from trio_lab.synergy.windows import PatchWindow

logger = logging.getLogger(__name__)

_PerPatch = list[tuple[str, int, int]]  # (patch, games, wins)

_PK = (
    "window_label",
    "platform",
    "jgl_champion",
    "mid_champion",
    "sup_champion",
    "enemy_role",
    "enemy_champion",
)
_UPDATE_COLUMNS = ("games", "games_eff", "wr", "delta_raw", "delta", "ci_low", "ci_high", "tier")
_UPDATE_SQL = ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATE_COLUMNS)


def _load(conn: psycopg.Connection, window: PatchWindow):
    """Charge les agrégats de la fenêtre, indexés par trio puis par matchup."""
    patches = list(window.patches)
    trios: dict[tuple, _PerPatch] = defaultdict(list)
    for platform, jgl, mid, sup, patch, games, wins in conn.execute(
        "SELECT platform, jgl_champion, mid_champion, sup_champion, patch, games, wins"
        " FROM agg_trio WHERE patch = ANY(%s)",
        (patches,),
    ):
        trios[(platform, jgl, mid, sup)].append((patch, games, wins))

    matchups: dict[tuple, _PerPatch] = defaultdict(list)
    for platform, jgl, mid, sup, role, enemy, patch, games, wins in conn.execute(
        "SELECT platform, jgl_champion, mid_champion, sup_champion,"
        " enemy_role, enemy_champion, patch, games, wins"
        " FROM agg_trio_vs_champion WHERE patch = ANY(%s)",
        (patches,),
    ):
        matchups[(platform, jgl, mid, sup, role, enemy)].append((patch, games, wins))

    # Vue « toutes régions », cohérente avec score_trio (platform='all').
    scores.add_combined_platform(trios)
    scores.add_combined_platform(matchups)
    return trios, matchups


def refresh(
    window: PatchWindow,
    *,
    dsn: str | None = None,
    k: float = scores.DEFAULT_PRIOR_K,
    thresholds: tuple[float, float] = scores.DEFAULT_TIER_THRESHOLDS,
) -> dict[str, int]:
    """Recalcule les counters d'une fenêtre. Retourne le nombre de lignes par table."""
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        trios, matchups = _load(conn, window)

        rows: list[dict] = []
        for (platform, jgl, mid, sup, role, enemy), per_patch in matchups.items():
            # Coupure de rework sur les 4 champions ; la baseline du trio est
            # pondérée avec les MÊMES poids pour rester comparable au matchup.
            weights = window.weights_for((jgl, mid, sup, enemy))
            vs = scores.weighted_wr(per_patch, weights)
            if vs is None:
                continue
            trio_per_patch = trios.get((platform, jgl, mid, sup))
            baseline = scores.weighted_wr(trio_per_patch, weights) if trio_per_patch else None
            if baseline is None:
                continue
            delta_raw = vs.wr - baseline.wr
            ci_low, ci_high = scores.wilson_interval(vs.wr, vs.games_eff)
            rows.append(
                {
                    "window_label": window.label,
                    "platform": platform,
                    "jgl_champion": jgl,
                    "mid_champion": mid,
                    "sup_champion": sup,
                    "enemy_role": role,
                    "enemy_champion": enemy,
                    "games": vs.games,
                    "games_eff": vs.games_eff,
                    "wr": vs.wr,
                    "delta_raw": delta_raw,
                    "delta": scores.smooth(delta_raw, vs.games_eff, 0.0, k),
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "tier": scores.reliability_tier(vs.games_eff, thresholds),
                }
            )

        if rows:
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(
                    f"""
                    INSERT INTO score_trio_vs_champion (
                        window_label, platform, jgl_champion, mid_champion, sup_champion,
                        enemy_role, enemy_champion, games, games_eff, wr, delta_raw, delta,
                        ci_low, ci_high, tier)
                    VALUES (%(window_label)s, %(platform)s, %(jgl_champion)s,
                            %(mid_champion)s, %(sup_champion)s, %(enemy_role)s,
                            %(enemy_champion)s, %(games)s, %(games_eff)s, %(wr)s,
                            %(delta_raw)s, %(delta)s, %(ci_low)s, %(ci_high)s, %(tier)s)
                    ON CONFLICT ({", ".join(_PK)}) DO UPDATE SET {_UPDATE_SQL}
                    WHERE score_trio_vs_champion.games IS DISTINCT FROM EXCLUDED.games
                    """,
                    rows,
                )
    counts = {"score_trio_vs_champion": len(rows)}
    logger.info("counters fenêtre %s rafraîchis : %s", window.label, counts)
    return counts
