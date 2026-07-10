"""Matérialisation des scores de synergie : agg_* → score_duo / score_trio.

Idempotent par fenêtre (DELETE + INSERT dans une transaction, comme
`stats.aggregate`). Tout tient en mémoire : les agrégats sont déjà compactés
par (patch, platform, combinaison) — à re-profiler quand le volume de trios
distincts explosera (des millions de lignes possibles à ~1M matchs/patch).

La baseline individuelle d'un combo est pondérée avec les MÊMES poids de
fenêtre que le combo (coupure de rework incluse) : la synergie est une
différence, ses deux termes doivent couvrir la même fenêtre (PROJECT.md).
"""

from __future__ import annotations

import logging
from collections import defaultdict

import psycopg

from trio_lab import db
from trio_lab.synergy import scores
from trio_lab.synergy.windows import PatchWindow

logger = logging.getLogger(__name__)

# `roles` de agg_duo/score_duo → rôles agg_champion de (champ_a, champ_b).
DUO_ROLES: dict[str, tuple[str, str]] = {
    "jgl_mid": ("JUNGLE", "MIDDLE"),
    "jgl_sup": ("JUNGLE", "UTILITY"),
    "mid_sup": ("MIDDLE", "UTILITY"),
}

_PerPatch = list[tuple[str, int, int]]  # (patch, games, wins)


def _load(conn: psycopg.Connection, window: PatchWindow):
    """Charge les agrégats de la fenêtre, indexés par combinaison."""
    patches = list(window.patches)
    indiv: dict[tuple, _PerPatch] = defaultdict(list)
    for platform, role, champ, patch, games, wins in conn.execute(
        "SELECT platform, role, champion_id, patch, games, wins"
        " FROM agg_champion WHERE patch = ANY(%s)",
        (patches,),
    ):
        indiv[(platform, role, champ)].append((patch, games, wins))

    duos: dict[tuple, _PerPatch] = defaultdict(list)
    for platform, roles, a, b, patch, games, wins in conn.execute(
        "SELECT platform, roles, champ_a, champ_b, patch, games, wins"
        " FROM agg_duo WHERE patch = ANY(%s)",
        (patches,),
    ):
        duos[(platform, roles, a, b)].append((patch, games, wins))

    trios: dict[tuple, _PerPatch] = defaultdict(list)
    for platform, jgl, mid, sup, patch, games, wins in conn.execute(
        "SELECT platform, jgl_champion, mid_champion, sup_champion, patch, games, wins"
        " FROM agg_trio WHERE patch = ANY(%s)",
        (patches,),
    ):
        trios[(platform, jgl, mid, sup)].append((patch, games, wins))
    return indiv, duos, trios


def refresh(
    window: PatchWindow,
    *,
    dsn: str | None = None,
    k: float = scores.DEFAULT_PRIOR_K,
    thresholds: tuple[float, float] = scores.DEFAULT_TIER_THRESHOLDS,
) -> dict[str, int]:
    """Recalcule les scores d'une fenêtre. Retourne le nombre de lignes par table."""
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        indiv, duos, trios = _load(conn, window)

        def member_wr(platform: str, role: str, champ: int, weights) -> scores.WeightedWR | None:
            rows = indiv.get((platform, role, champ))
            return scores.weighted_wr(rows, weights) if rows else None

        duo_rows: list[dict] = []
        duo_synergies: dict[tuple, float] = {}
        for (platform, roles, a, b), per_patch in duos.items():
            weights = window.weights_for((a, b))
            combo = scores.weighted_wr(per_patch, weights)
            if combo is None:
                continue
            role_a, role_b = DUO_ROLES[roles]
            wr_a = member_wr(platform, role_a, a, weights)
            wr_b = member_wr(platform, role_b, b, weights)
            if wr_a is None or wr_b is None:
                continue  # baseline incalculable sur la fenêtre (rework)
            syn = scores.synergy(combo.wr, (wr_a.wr, wr_b.wr))
            ci_low, ci_high = scores.wilson_interval(combo.wr, combo.games_eff)
            # Le prior du trio utilise la synergie de duo RÉTRÉCIE vers 0 (prior
            # neutre) : un duo peu joué provient des mêmes matchs que le trio et
            # reproduirait son extrême — à volume réel le rétrécissement devient
            # négligeable. La table score_duo publie, elle, la synergie brute.
            duo_synergies[(platform, roles, a, b)] = scores.smooth(syn, combo.games_eff, 0.0, k)
            duo_rows.append(
                {
                    "window_label": window.label,
                    "platform": platform,
                    "roles": roles,
                    "champ_a": a,
                    "champ_b": b,
                    "games": combo.games,
                    "games_eff": combo.games_eff,
                    "wr": combo.wr,
                    "synergy": syn,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "tier": scores.reliability_tier(combo.games_eff, thresholds),
                }
            )

        trio_rows: list[dict] = []
        for (platform, jgl, mid, sup), per_patch in trios.items():
            weights = window.weights_for((jgl, mid, sup))
            combo = scores.weighted_wr(per_patch, weights)
            if combo is None:
                continue
            members = [
                member_wr(platform, "JUNGLE", jgl, weights),
                member_wr(platform, "MIDDLE", mid, weights),
                member_wr(platform, "UTILITY", sup, weights),
            ]
            if any(m is None for m in members):
                continue
            raw = scores.synergy(combo.wr, (m.wr for m in members))
            pred = scores.trio_prediction(
                duo_synergies[key]
                for key in (
                    (platform, "jgl_mid", jgl, mid),
                    (platform, "jgl_sup", jgl, sup),
                    (platform, "mid_sup", mid, sup),
                )
                if key in duo_synergies
            )
            smoothed = scores.smooth(raw, combo.games_eff, pred, k)
            ci_low, ci_high = scores.wilson_interval(combo.wr, combo.games_eff)
            trio_rows.append(
                {
                    "window_label": window.label,
                    "platform": platform,
                    "jgl_champion": jgl,
                    "mid_champion": mid,
                    "sup_champion": sup,
                    "games": combo.games,
                    "games_eff": combo.games_eff,
                    "wr": combo.wr,
                    "synergy_raw": raw,
                    "synergy_pred": pred,
                    "synergy": smoothed,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "tier": scores.reliability_tier(combo.games_eff, thresholds),
                }
            )

        with conn.transaction():
            conn.execute("DELETE FROM score_duo WHERE window_label = %s", (window.label,))
            conn.execute("DELETE FROM score_trio WHERE window_label = %s", (window.label,))
            with conn.cursor() as cur:
                if duo_rows:
                    cur.executemany(
                        """
                        INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b,
                                               games, games_eff, wr, synergy, ci_low, ci_high, tier)
                        VALUES (%(window_label)s, %(platform)s, %(roles)s, %(champ_a)s, %(champ_b)s,
                                %(games)s, %(games_eff)s, %(wr)s, %(synergy)s, %(ci_low)s,
                                %(ci_high)s, %(tier)s)
                        """,
                        duo_rows,
                    )
                if trio_rows:
                    cur.executemany(
                        """
                        INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,
                                                sup_champion, games, games_eff, wr, synergy_raw,
                                                synergy_pred, synergy, ci_low, ci_high, tier)
                        VALUES (%(window_label)s, %(platform)s, %(jgl_champion)s, %(mid_champion)s,
                                %(sup_champion)s, %(games)s, %(games_eff)s, %(wr)s, %(synergy_raw)s,
                                %(synergy_pred)s, %(synergy)s, %(ci_low)s, %(ci_high)s, %(tier)s)
                        """,
                        trio_rows,
                    )
    counts = {"score_duo": len(duo_rows), "score_trio": len(trio_rows)}
    logger.info("scores fenêtre %s rafraîchis : %s", window.label, counts)
    return counts
