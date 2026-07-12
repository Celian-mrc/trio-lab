"""Matérialisation des meilleurs alliés : agg_trio(_with_ally) → score_trio_with_ally.

Miroir de `synergy/counters.py`, côté allié plutôt qu'ennemi : le grain allié
est UN champion Top ou ADC (jamais une combinaison 5v5, CLAUDE.md — même
raisonnement que l'absence de counters trio vs trio).

Le score d'allié est un uplift : WR(trio + cet allié) − WR global du trio, les
deux pondérés sur la MÊME fenêtre multi-patchs. Positif = « cet allié tire ce
trio vers le haut ». La coupure de rework s'applique aux 4 champions concernés
(jgl/mid/sup + allié) — un allié retravaillé n'est plus le même partenaire.

L'uplift publié est rétréci vers 0 (prior neutre : pas d'effet allié tant que
le volume ne le prouve pas) : `uplift = n·uplift_raw / (n + k)`, même
mécanique et même k que le lissage des synergies/counters.
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


def _load(conn: psycopg.Connection, window: PatchWindow):
    """Charge les agrégats de la fenêtre, indexés par trio puis par allié."""
    patches = list(window.patches)
    trios: dict[tuple, _PerPatch] = defaultdict(list)
    for platform, jgl, mid, sup, patch, games, wins in conn.execute(
        "SELECT platform, jgl_champion, mid_champion, sup_champion, patch, games, wins"
        " FROM agg_trio WHERE patch = ANY(%s)",
        (patches,),
    ):
        trios[(platform, jgl, mid, sup)].append((patch, games, wins))

    allies: dict[tuple, _PerPatch] = defaultdict(list)
    for platform, jgl, mid, sup, role, ally, patch, games, wins in conn.execute(
        "SELECT platform, jgl_champion, mid_champion, sup_champion,"
        " ally_role, ally_champion, patch, games, wins"
        " FROM agg_trio_with_ally WHERE patch = ANY(%s)",
        (patches,),
    ):
        allies[(platform, jgl, mid, sup, role, ally)].append((patch, games, wins))

    # Vue « toutes régions », cohérente avec score_trio (platform='all').
    scores.add_combined_platform(trios)
    scores.add_combined_platform(allies)
    return trios, allies


def refresh(
    window: PatchWindow,
    *,
    dsn: str | None = None,
    k: float = scores.DEFAULT_PRIOR_K,
    thresholds: tuple[float, float] = scores.DEFAULT_TIER_THRESHOLDS,
) -> dict[str, int]:
    """Recalcule les meilleurs alliés d'une fenêtre. Retourne le nombre de lignes."""
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        trios, allies = _load(conn, window)

        rows: list[dict] = []
        for (platform, jgl, mid, sup, role, ally), per_patch in allies.items():
            # Coupure de rework sur les 4 champions ; la baseline du trio est
            # pondérée avec les MÊMES poids pour rester comparable à l'allié.
            weights = window.weights_for((jgl, mid, sup, ally))
            with_ally = scores.weighted_wr(per_patch, weights)
            if with_ally is None:
                continue
            trio_per_patch = trios.get((platform, jgl, mid, sup))
            baseline = scores.weighted_wr(trio_per_patch, weights) if trio_per_patch else None
            if baseline is None:
                continue
            uplift_raw = with_ally.wr - baseline.wr
            ci_low, ci_high = scores.wilson_interval(with_ally.wr, with_ally.games_eff)
            rows.append(
                {
                    "window_label": window.label,
                    "platform": platform,
                    "jgl_champion": jgl,
                    "mid_champion": mid,
                    "sup_champion": sup,
                    "ally_role": role,
                    "ally_champion": ally,
                    "games": with_ally.games,
                    "games_eff": with_ally.games_eff,
                    "wr": with_ally.wr,
                    "uplift_raw": uplift_raw,
                    "uplift": scores.smooth(uplift_raw, with_ally.games_eff, 0.0, k),
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "tier": scores.reliability_tier(with_ally.games_eff, thresholds),
                }
            )

        with conn.transaction():
            conn.execute(
                "DELETE FROM score_trio_with_ally WHERE window_label = %s", (window.label,)
            )
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO score_trio_with_ally (
                            window_label, platform, jgl_champion, mid_champion, sup_champion,
                            ally_role, ally_champion, games, games_eff, wr, uplift_raw, uplift,
                            ci_low, ci_high, tier)
                        VALUES (%(window_label)s, %(platform)s, %(jgl_champion)s,
                                %(mid_champion)s, %(sup_champion)s, %(ally_role)s,
                                %(ally_champion)s, %(games)s, %(games_eff)s, %(wr)s,
                                %(uplift_raw)s, %(uplift)s, %(ci_low)s, %(ci_high)s, %(tier)s)
                        """,
                        rows,
                    )
    counts = {"score_trio_with_ally": len(rows)}
    logger.info("alliés fenêtre %s rafraîchis : %s", window.label, counts)
    return counts
