"""Matérialisation des scores de synergie : agg_* → score_duo / score_trio.

Idempotent par fenêtre via UPSERT (`INSERT ... ON CONFLICT DO UPDATE`), pas
DELETE+INSERT : à `window_label` fixe, `games_eff` d'une combinaison ne peut
que croître d'un cycle à l'autre (jamais de matchs retirés de la fenêtre
avant son rollover, purgé à part par `maintenance.purge_stale_scores`) donc
l'ensemble des clés ne fait que grossir — aucune ligne existante n'a besoin
d'être supprimée en cours de fenêtre. Un DELETE+INSERT complet à chaque
cycle générait des tuples morts sur la totalité de la fenêtre (~500k lignes
pour score_trio) même quand la ligne était inchangée ; l'UPSERT, guardé par
`games IS DISTINCT FROM EXCLUDED.games`, ne touche que les combinaisons
réellement mises à jour par les nouveaux matchs du cycle (cf. mémoire
`supabase-disk-growth`, bloat constaté le 14/07/2026). Tout tient en
mémoire : les agrégats sont déjà compactés par (patch, platform,
combinaison) — à re-profiler quand le volume de trios distincts explosera
(des millions de lignes possibles à ~1M matchs/patch).

La baseline individuelle d'un combo est pondérée avec les MÊMES poids de
fenêtre que le combo (coupure de rework incluse) : la synergie est une
différence, ses deux termes doivent couvrir la même fenêtre (PROJECT.md).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Iterable

import psycopg

from trio_lab import db
from trio_lab.ccref import score as ccref_score
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

# Score de scaling (WR ~ tranche de durée, cf. migration 015) : uniquement
# empirique, pas de lissage vers un prior (mélange avec la trajectoire de
# gold testé et écarté — corrélation quasi nulle, cf. commentaire migration).
# En dessous de ces seuils, `scaling` reste NULL plutôt que de publier une
# pente calculée sur un bruit de 1-2 games.
SCALING_MIN_BUCKET_GAMES = 3
SCALING_MIN_BUCKETS = 3

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

# CC normalisé 0-100 (théorique/empirique/mélangé) : pas une simple moyenne
# pondérée d'agrégats comme STAT_PAIRS, calculé à part par `_cc_pct_fields`.
_CC_PCT_COLUMNS = ("cc_theoretical_pct", "cc_empirical_pct", "cc_blended_pct")

_SCORE_COLUMNS = (*STAT_PAIRS, *_CC_PCT_COLUMNS)
_SCORE_STAT_SQL = ", ".join(_SCORE_COLUMNS)
_SCORE_STAT_PLACEHOLDERS = ", ".join(f"%({name})s" for name in _SCORE_COLUMNS)

_DUO_PK = ("window_label", "platform", "roles", "champ_a", "champ_b")
_DUO_UPDATE_COLUMNS = (
    "games",
    "games_eff",
    "wr",
    "synergy",
    "synergy_ci_low",
    "synergy_ci_high",
    "ci_low",
    "ci_high",
    "tier",
    "scaling",
    "scaling_ci_low",
    "scaling_ci_high",
    *_SCORE_COLUMNS,
)
_DUO_UPDATE_SQL = ", ".join(f"{c} = EXCLUDED.{c}" for c in _DUO_UPDATE_COLUMNS)

_TRIO_PK = ("window_label", "platform", "jgl_champion", "mid_champion", "sup_champion")
_TRIO_UPDATE_COLUMNS = (
    "games",
    "games_eff",
    "wr",
    "synergy_raw",
    "synergy_pred",
    "synergy",
    "synergy_ci_low",
    "synergy_ci_high",
    "ci_low",
    "ci_high",
    "tier",
    "scaling",
    "scaling_ci_low",
    "scaling_ci_high",
    *_SCORE_COLUMNS,
)
_TRIO_UPDATE_SQL = ", ".join(f"{c} = EXCLUDED.{c}" for c in _TRIO_UPDATE_COLUMNS)


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


def _cc_pct_fields(
    member_champions: tuple[int, ...],
    cc_theo_scores: dict[int, float],
    empirical_cc_time_s: float | None,
    games_eff: float,
    k: float,
) -> dict[str, float | None]:
    """Scores CC normalisés 0-100 (théorique/empirique/mélangé) d'une combinaison.

    `cc_theo_scores` vide (table `champion_cc_theoretical` pas encore
    synchronisée, `python -m trio_lab.ccref.sync_theoretical`) : les 3 champs
    restent `None` plutôt que de faire échouer tout le refresh.
    """
    if not cc_theo_scores:
        return {"cc_theoretical_pct": None, "cc_empirical_pct": None, "cc_blended_pct": None}
    raw_theo = sum(cc_theo_scores.get(c, 0.0) for c in member_champions)
    theo_pct = ccref_score.theoretical_pct(
        raw_theo, member_count=len(member_champions), scores=cc_theo_scores
    )
    emp_pct = ccref_score.empirical_pct(empirical_cc_time_s)
    return {
        "cc_theoretical_pct": theo_pct,
        "cc_empirical_pct": emp_pct,
        "cc_blended_pct": ccref_score.blended_pct(emp_pct, theo_pct, games_eff, k),
    }


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

    cc_theo_scores = dict(
        conn.execute("SELECT champion_id, score FROM champion_cc_theoretical").fetchall()
    )
    return indiv, duos, trios, cc_theo_scores


def _load_duration_buckets(
    conn: psycopg.Connection, patches: list[str], *, table: str, key_columns: tuple[str, ...]
) -> dict[tuple, dict[int, _PerPatch]]:
    """Charge `agg_trio_duration`/`agg_duo_duration`, groupé par combo puis par tranche.

    Réutilise `scores.add_combined_platform` (clé `(platform, *rest)`) en
    incluant la tranche dans `*rest` : la vue 'all' est donc déjà sommée par
    tranche, pas seulement par combo.
    """
    columns = ", ".join(key_columns)
    flat: dict[tuple, _PerPatch] = defaultdict(list)
    with conn.cursor() as cur:
        for row in cur.execute(
            f"SELECT platform, {columns}, duration_bucket, patch, games, wins"  # noqa: S608
            f" FROM {table} WHERE patch = ANY(%s)",
            (patches,),
        ):
            platform, *key, bucket, patch, games, wins = row
            flat[(platform, *key, bucket)].append((patch, games, wins))
    scores.add_combined_platform(flat)
    grouped: dict[tuple, dict[int, _PerPatch]] = defaultdict(dict)
    for (platform, *key, bucket), rows in flat.items():
        grouped[(platform, *key)][bucket] = rows
    return grouped


def _scaling_slope(
    by_bucket: dict[int, _PerPatch] | None, weights: dict[str, float]
) -> scores.WeightedSlope | None:
    """Pente WR ~ tranche de durée (points de WR par tranche de 5 min) + IC 95 %.

    `None` tant que le volume ne permet pas au moins `SCALING_MIN_BUCKETS`
    tranches avec chacune au moins `SCALING_MIN_BUCKET_GAMES` games bruts —
    en dessous, un point de la régression serait du bruit pur.
    """
    if not by_bucket:
        return None
    points: list[tuple[float, float, float]] = []
    for bucket, rows in by_bucket.items():
        if sum(games for _, games, _ in rows) < SCALING_MIN_BUCKET_GAMES:
            continue
        wr = scores.weighted_wr(rows, weights)
        if wr is None:
            continue
        points.append((bucket / 5.0, wr.wr, wr.games_eff))
    if len(points) < SCALING_MIN_BUCKETS:
        return None
    return scores.weighted_slope_ci(points)


def _scaling_fields(slope: scores.WeightedSlope | None) -> dict[str, float | None]:
    if slope is None:
        return {"scaling": None, "scaling_ci_low": None, "scaling_ci_high": None}
    return {
        "scaling": slope.slope,
        "scaling_ci_low": slope.ci_low,
        "scaling_ci_high": slope.ci_high,
    }


def _synergy_ci(
    combo: scores.WeightedWR,
    combo_ci: tuple[float, float],
    members: Iterable[scores.WeightedWR],
) -> tuple[float, float]:
    """IC de Newcombe (1998) pour la synergie BRUTE (combo.wr − moyenne des WR
    membres) : combine l'IC de Wilson du combo avec un IC normal sur la
    baseline (moyenne de 2 ou 3 WR de champion — le volume individuel de
    chacun est presque toujours bien plus grand que celui du combo)."""
    member_list = list(members)
    n = len(member_list)
    baseline = sum(m.wr for m in member_list) / n
    var_baseline = sum(m.wr * (1.0 - m.wr) / m.games_eff for m in member_list) / (n * n)
    l2, u2 = scores.normal_interval(baseline, math.sqrt(var_baseline))
    l1, u1 = combo_ci
    return scores.newcombe_interval(combo.wr, l1, u1, baseline, l2, u2)


def refresh(
    window: PatchWindow,
    *,
    dsn: str | None = None,
    k: float = scores.DEFAULT_PRIOR_K,
    thresholds: tuple[float, float] = scores.DEFAULT_TIER_THRESHOLDS,
) -> dict[str, int]:
    """Recalcule les scores d'une fenêtre. Retourne le nombre de lignes par table."""
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        indiv, duos, trios, cc_theo_scores = _load(conn, window)
        patches = list(window.patches)
        duo_durations = _load_duration_buckets(
            conn, patches, table="agg_duo_duration", key_columns=("roles", "champ_a", "champ_b")
        )
        trio_durations = _load_duration_buckets(
            conn,
            patches,
            table="agg_trio_duration",
            key_columns=("jgl_champion", "mid_champion", "sup_champion"),
        )

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
            syn_ci_low, syn_ci_high = _synergy_ci(combo, (ci_low, ci_high), (wr_a, wr_b))
            # Le prior du trio utilise la synergie de duo RÉTRÉCIE vers 0 (prior
            # neutre) : un duo peu joué provient des mêmes matchs que le trio et
            # reproduirait son extrême — à volume réel le rétrécissement devient
            # négligeable. La table score_duo publie, elle, la synergie brute.
            duo_synergies[(platform, roles, a, b)] = scores.smooth(syn, combo.games_eff, 0.0, k)
            stats = _weighted_stats(agg_rows, weights)
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
                    "synergy_ci_low": syn_ci_low,
                    "synergy_ci_high": syn_ci_high,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "tier": scores.reliability_tier(combo.games_eff, thresholds),
                    **_scaling_fields(
                        _scaling_slope(duo_durations.get((platform, roles, a, b)), weights)
                    ),
                    **stats,
                    **_cc_pct_fields(
                        (a, b), cc_theo_scores, stats["cc_time_s"], combo.games_eff, k
                    ),
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
            syn_ci_low, syn_ci_high = _synergy_ci(combo, (ci_low, ci_high), members)
            stats = _weighted_stats(agg_rows, weights)
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
                    "synergy_ci_low": syn_ci_low,
                    "synergy_ci_high": syn_ci_high,
                    "ci_low": ci_low,
                    "ci_high": ci_high,
                    "tier": scores.reliability_tier(combo.games_eff, thresholds),
                    **_scaling_fields(
                        _scaling_slope(trio_durations.get((platform, jgl, mid, sup)), weights)
                    ),
                    **stats,
                    **_cc_pct_fields(
                        (jgl, mid, sup), cc_theo_scores, stats["cc_time_s"], combo.games_eff, k
                    ),
                }
            )

        with conn.transaction(), conn.cursor() as cur:
            if duo_rows:
                cur.executemany(
                    f"""
                    INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b,
                                           games, games_eff, wr, synergy, synergy_ci_low,
                                           synergy_ci_high, ci_low, ci_high, tier, scaling,
                                           scaling_ci_low, scaling_ci_high, {_SCORE_STAT_SQL})
                    VALUES (%(window_label)s, %(platform)s, %(roles)s, %(champ_a)s, %(champ_b)s,
                            %(games)s, %(games_eff)s, %(wr)s, %(synergy)s, %(synergy_ci_low)s,
                            %(synergy_ci_high)s, %(ci_low)s, %(ci_high)s, %(tier)s, %(scaling)s,
                            %(scaling_ci_low)s, %(scaling_ci_high)s, {_SCORE_STAT_PLACEHOLDERS})
                    ON CONFLICT ({", ".join(_DUO_PK)}) DO UPDATE SET {_DUO_UPDATE_SQL}
                    WHERE score_duo.games IS DISTINCT FROM EXCLUDED.games
                    """,
                    duo_rows,
                )
            if trio_rows:
                cur.executemany(
                    f"""
                    INSERT INTO score_trio (window_label, platform, jgl_champion, mid_champion,
                                            sup_champion, games, games_eff, wr, synergy_raw,
                                            synergy_pred, synergy, synergy_ci_low,
                                            synergy_ci_high, ci_low, ci_high, tier, scaling,
                                            scaling_ci_low, scaling_ci_high, {_SCORE_STAT_SQL})
                    VALUES (%(window_label)s, %(platform)s, %(jgl_champion)s, %(mid_champion)s,
                            %(sup_champion)s, %(games)s, %(games_eff)s, %(wr)s, %(synergy_raw)s,
                            %(synergy_pred)s, %(synergy)s, %(synergy_ci_low)s,
                            %(synergy_ci_high)s, %(ci_low)s, %(ci_high)s, %(tier)s, %(scaling)s,
                            %(scaling_ci_low)s, %(scaling_ci_high)s, {_SCORE_STAT_PLACEHOLDERS})
                    ON CONFLICT ({", ".join(_TRIO_PK)}) DO UPDATE SET {_TRIO_UPDATE_SQL}
                    WHERE score_trio.games IS DISTINCT FROM EXCLUDED.games
                    """,
                    trio_rows,
                )
    counts = {"score_duo": len(duo_rows), "score_trio": len(trio_rows)}
    logger.info("scores fenêtre %s rafraîchis : %s", window.label, counts)
    return counts
