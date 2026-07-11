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

# Stat de score → paire (somme, dénominateur) d'agg_trio/agg_duo. Chaque stat
# a son propre n : gold_diff_10 est NULL si la partie finit avant 10 min.
STAT_PAIRS: dict[str, tuple[str, str]] = {
    "gold_diff_5": ("gold5_sum", "gold5_n"),
    "gold_diff_10": ("gold10_sum", "gold10_n"),
    "gold_diff_15": ("gold15_sum", "gold15_n"),
    "vision_score": ("vision_sum", "vision_n"),
    "drakes": ("drakes_sum", "drakes_n"),
    "soul_rate": ("soul_sum", "soul_n"),
    "herald_rate": ("herald_sum", "herald_n"),
    "first_tower_rate": ("tower1_sum", "tower1_n"),
    "cc_time_s": ("cc_sum", "cc_n"),
}
_AGG_STAT_COLUMNS = ("games", "wins", *(col for pair in STAT_PAIRS.values() for col in pair))
_SCORE_STAT_SQL = ", ".join(STAT_PAIRS)
_SCORE_STAT_PLACEHOLDERS = ", ".join(f"%({name})s" for name in STAT_PAIRS)


def _weighted_stats(rows: list[dict], weights: dict[str, float]) -> dict[str, float | None]:
    """Moyennes de stats pondérées fenêtre : Σw·somme / Σw·n, None sans donnée."""
    out: dict[str, float | None] = {}
    for name, (sum_key, n_key) in STAT_PAIRS.items():
        num = 0.0
        den = 0.0
        for row in rows:
            weight = weights.get(row["patch"], 0.0)
            if weight <= 0.0 or row.get(sum_key) is None or not row.get(n_key):
                continue
            num += weight * row[sum_key]
            den += weight * row[n_key]
        out[name] = num / den if den > 0.0 else None
    return out


def _per_patch(agg_rows: list[dict]) -> _PerPatch:
    """Projette des lignes dict d'agrégat vers les tuples de `weighted_wr`."""
    return [(r["patch"], r["games"], r["wins"]) for r in agg_rows]


def _add_combined_stat_rows(mapping: dict[tuple, list[dict]]) -> None:
    """Équivalent de `scores.add_combined_platform` pour les lignes dict d'agg_*."""
    combined: dict[tuple, dict[str, dict]] = {}
    for (platform, *rest), rows in mapping.items():
        if platform == scores.ALL_PLATFORMS:
            continue
        acc = combined.setdefault((scores.ALL_PLATFORMS, *rest), {})
        for row in rows:
            cell = acc.setdefault(row["patch"], {"patch": row["patch"]})
            for column in _AGG_STAT_COLUMNS:
                value = row.get(column)
                if value is not None:
                    cell[column] = cell.get(column, 0) + value
    for key, per_patch in combined.items():
        mapping[key] = list(per_patch.values())


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

    stat_columns = ", ".join(_AGG_STAT_COLUMNS)
    duos: dict[tuple, list[dict]] = defaultdict(list)
    trios: dict[tuple, list[dict]] = defaultdict(list)
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        for row in cur.execute(
            f"SELECT platform, roles, champ_a, champ_b, patch, {stat_columns}"
            " FROM agg_duo WHERE patch = ANY(%s)",
            (patches,),
        ):
            duos[(row["platform"], row["roles"], row["champ_a"], row["champ_b"])].append(row)
        for row in cur.execute(
            f"SELECT platform, jgl_champion, mid_champion, sup_champion, patch, {stat_columns}"
            " FROM agg_trio WHERE patch = ANY(%s)",
            (patches,),
        ):
            trios[
                (row["platform"], row["jgl_champion"], row["mid_champion"], row["sup_champion"])
            ].append(row)

    # Vue « toutes régions » : sommes par patch entre plateformes, matérialisée
    # sous platform='all' comme n'importe quelle autre valeur de colonne.
    scores.add_combined_platform(indiv)
    _add_combined_stat_rows(duos)
    _add_combined_stat_rows(trios)
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
        for (platform, roles, a, b), agg_rows in duos.items():
            weights = window.weights_for((a, b))
            combo = scores.weighted_wr(_per_patch(agg_rows), weights)
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
                    **_weighted_stats(agg_rows, weights),
                }
            )

        trio_rows: list[dict] = []
        for (platform, jgl, mid, sup), agg_rows in trios.items():
            weights = window.weights_for((jgl, mid, sup))
            combo = scores.weighted_wr(_per_patch(agg_rows), weights)
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
                    **_weighted_stats(agg_rows, weights),
                }
            )

        with conn.transaction():
            conn.execute("DELETE FROM score_duo WHERE window_label = %s", (window.label,))
            conn.execute("DELETE FROM score_trio WHERE window_label = %s", (window.label,))
            with conn.cursor() as cur:
                if duo_rows:
                    cur.executemany(
                        f"""
                        INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b,
                                               games, games_eff, wr, synergy, ci_low, ci_high, tier,
                                               {_SCORE_STAT_SQL})
                        VALUES (%(window_label)s, %(platform)s, %(roles)s, %(champ_a)s, %(champ_b)s,
                                %(games)s, %(games_eff)s, %(wr)s, %(synergy)s, %(ci_low)s,
                                %(ci_high)s, %(tier)s, {_SCORE_STAT_PLACEHOLDERS})
                        """,
                        duo_rows,
                    )
                if trio_rows:
                    cur.executemany(
                        f"""
                        INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,
                                                sup_champion, games, games_eff, wr, synergy_raw,
                                                synergy_pred, synergy, ci_low, ci_high, tier,
                                                {_SCORE_STAT_SQL})
                        VALUES (%(window_label)s, %(platform)s, %(jgl_champion)s, %(mid_champion)s,
                                %(sup_champion)s, %(games)s, %(games_eff)s, %(wr)s, %(synergy_raw)s,
                                %(synergy_pred)s, %(synergy)s, %(ci_low)s, %(ci_high)s, %(tier)s,
                                {_SCORE_STAT_PLACEHOLDERS})
                        """,
                        trio_rows,
                    )
    counts = {"score_duo": len(duo_rows), "score_trio": len(trio_rows)}
    logger.info("scores fenêtre %s rafraîchis : %s", window.label, counts)
    return counts
