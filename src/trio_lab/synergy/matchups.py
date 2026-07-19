"""Matérialisation des counters 1v1 même rôle : agg_matchup → score_matchup.

Redesign du 2026-07-19 (migration 026) : contrairement à l'ancien
`score_trio_vs_champion` (Phase 4, abandonné en migration 022 — le combinatoire
trio×champion_ennemi×rôle explosait pour un signal peu fiable), le grain est
un DUEL même rôle (champ_a vs champ_b), pas trio vs champion — borné comme
`agg_duo`/`score_duo`, pas la même classe de problème.

Le delta est un uplift/downlift : WR(champ_a vs champ_b, même rôle) − WR
baseline de champ_a dans ce rôle (`agg_champion`, toutes compositions
confondues), les deux pondérés sur la MÊME fenêtre multi-patchs. Rétréci vers
0 (prior neutre : pas d'effet de matchup tant que le volume ne le prouve
pas), même mécanique et même k que `synergy/compute.py`.

Volumétrie bornée (5 rôles × ~170² champions maximum, pas de dimension
trio) : tout tient en mémoire, pas besoin du curseur streaming de l'ancien
`counters.py` (qui gérait un espace bien plus grand).
"""

from __future__ import annotations

import logging

import psycopg

from trio_lab import db
from trio_lab.synergy import scores
from trio_lab.synergy.windows import PatchWindow

logger = logging.getLogger(__name__)

_PerPatch = list[tuple[str, int, int]]  # (patch, games, wins)

_PK = ("window_label", "platform", "role", "champ_a", "champ_b")
_UPDATE_COLUMNS = ("games", "games_eff", "wr", "delta_raw", "delta", "ci_low", "ci_high", "tier")
_UPDATE_SQL = ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATE_COLUMNS)
_INSERT_SQL = f"""
    INSERT INTO score_matchup (
        window_label, platform, role, champ_a, champ_b,
        games, games_eff, wr, delta_raw, delta, ci_low, ci_high, tier)
    VALUES (%(window_label)s, %(platform)s, %(role)s, %(champ_a)s, %(champ_b)s,
            %(games)s, %(games_eff)s, %(wr)s, %(delta_raw)s, %(delta)s,
            %(ci_low)s, %(ci_high)s, %(tier)s)
    ON CONFLICT ({", ".join(_PK)}) DO UPDATE SET {_UPDATE_SQL}
    WHERE score_matchup.games IS DISTINCT FROM EXCLUDED.games
"""


def _load_baselines(conn: psycopg.Connection, patches: list[str]) -> dict[tuple, _PerPatch]:
    """WR baseline par (platform, role, champion), depuis `agg_champion`."""
    baselines: dict[tuple, _PerPatch] = {}
    for platform, role, champ, patch, games, wins in conn.execute(
        "SELECT platform, role, champion_id, patch, games, wins"
        " FROM agg_champion WHERE patch = ANY(%s)",
        (patches,),
    ):
        baselines.setdefault((platform, role, champ), []).append((patch, games, wins))
    scores.add_combined_platform(baselines)
    return baselines


def _load_matchups(conn: psycopg.Connection, patches: list[str]) -> dict[tuple, _PerPatch]:
    matchups: dict[tuple, _PerPatch] = {}
    for platform, role, a, b, patch, games, wins in conn.execute(
        "SELECT platform, role, champ_a, champ_b, patch, games, wins"
        " FROM agg_matchup WHERE patch = ANY(%s)",
        (patches,),
    ):
        matchups.setdefault((platform, role, a, b), []).append((patch, games, wins))
    scores.add_combined_platform(matchups)
    return matchups


def refresh(
    window: PatchWindow,
    *,
    dsn: str | None = None,
    k: float = scores.DEFAULT_PRIOR_K,
    thresholds: tuple[float, float] = scores.DEFAULT_TIER_THRESHOLDS,
) -> dict[str, int]:
    """Recalcule les matchups 1v1 d'une fenêtre. Retourne le nombre de lignes écrites."""
    patches = list(window.patches)
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        baselines = _load_baselines(conn, patches)
        matchups = _load_matchups(conn, patches)

        rows: list[dict] = []
        for (platform, role, a, b), per_patch in matchups.items():
            weights = window.weights_for((a, b))
            matchup = scores.weighted_wr(per_patch, weights)
            if matchup is None:
                continue
            baseline_rows = baselines.get((platform, role, a))
            baseline = scores.weighted_wr(baseline_rows, weights) if baseline_rows else None
            if baseline is None:
                continue  # baseline incalculable sur la fenêtre (rework)
            delta_raw = matchup.wr - baseline.wr
            ci_low, ci_high = scores.wilson_interval(matchup.wr, matchup.games_eff)
            rows.append(
                {
                    "window_label": window.label,
                    "platform": platform,
                    "role": role,
                    "champ_a": a,
                    "champ_b": b,
                    "games": matchup.games,
                    "games_eff": matchup.games_eff,
                    "wr": matchup.wr,
                    "delta_raw": delta_raw,
                    "delta": scores.smooth(delta_raw, matchup.games_eff, 0.0, k),
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "tier": scores.reliability_tier(matchup.games_eff, thresholds),
                }
            )

        with conn.transaction(), conn.cursor() as cur:
            if rows:
                cur.executemany(_INSERT_SQL, rows)

    counts = {"score_matchup": len(rows)}
    logger.info("matchups fenêtre %s rafraîchis : %s", window.label, counts)
    return counts
