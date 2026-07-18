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

Streaming (18/07/2026) : `agg_trio_vs_champion` a explosé avec
l'élargissement du pool de joueurs (5→100 pages de découverte) — tout
charger en mémoire (l'ancienne version) faisait planter le collecteur à
chaque cycle (constaté : `score_trio_vs_champion` figé 4 jours d'affilée
alors que `score_trio`, plus léger, continuait de se rafraîchir). La lecture
passe par un curseur serveur trié par combo (jgl/mid/sup/enemy_role/
enemy_champion puis plateforme, pour regrouper les plateformes d'un même
combo consécutivement et calculer la vue « toutes régions » à la volée sans
tout charger) ; l'écriture se fait par lots committés indépendamment, sur
une connexion séparée pour ne pas invalider le curseur de lecture.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

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
_INSERT_SQL = f"""
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
"""
# Nombre de lignes accumulées avant un commit — borne la mémoire du lot
# d'écriture sans multiplier les aller-retours réseau.
BATCH_SIZE = 5000


def _load_trios(conn: psycopg.Connection, patches: list[str]) -> dict[tuple, _PerPatch]:
    """WR agrégés par trio (baseline) — bien plus petit que les matchups, gardé en mémoire."""
    trios: dict[tuple, _PerPatch] = {}
    for platform, jgl, mid, sup, patch, games, wins in conn.execute(
        "SELECT platform, jgl_champion, mid_champion, sup_champion, patch, games, wins"
        " FROM agg_trio WHERE patch = ANY(%s)",
        (patches,),
    ):
        trios.setdefault((platform, jgl, mid, sup), []).append((patch, games, wins))
    scores.add_combined_platform(trios)
    return trios


def _stream_matchup_groups(
    conn: psycopg.Connection, patches: list[str]
) -> Iterator[tuple[tuple, dict[str, _PerPatch]]]:
    """Lignes `agg_trio_vs_champion` groupées par combo, plateformes rassemblées.

    Trié par combo puis plateforme (pas par patch, qui n'apparaît nulle part
    dans l'ordre) : toutes les plateformes d'un même combo arrivent à la
    suite, ce qui permet de calculer la vue « toutes régions » sans jamais
    charger tous les matchups en mémoire à la fois.
    """
    with conn.cursor(name="counters_matchups") as cur:
        cur.itersize = BATCH_SIZE
        cur.execute(
            "SELECT jgl_champion, mid_champion, sup_champion, enemy_role, enemy_champion,"
            " platform, patch, games, wins"
            " FROM agg_trio_vs_champion WHERE patch = ANY(%s)"
            " ORDER BY jgl_champion, mid_champion, sup_champion, enemy_role, enemy_champion,"
            " platform",
            (patches,),
        )
        current_combo: tuple | None = None
        per_platform: dict[str, _PerPatch] = {}
        for jgl, mid, sup, role, enemy, platform, patch, games, wins in cur:
            combo = (jgl, mid, sup, role, enemy)
            if combo != current_combo:
                if current_combo is not None:
                    yield current_combo, per_platform
                current_combo = combo
                per_platform = {}
            per_platform.setdefault(platform, []).append((patch, games, wins))
        if current_combo is not None:
            yield current_combo, per_platform


def _combine_platforms(per_platform: dict[str, _PerPatch]) -> _PerPatch:
    """Somme par patch toutes plateformes confondues (équivalent `add_combined_platform`)."""
    combined: dict[str, list[int]] = {}
    for per_patch in per_platform.values():
        for patch, games, wins in per_patch:
            acc = combined.setdefault(patch, [0, 0])
            acc[0] += games
            acc[1] += wins
    return [(patch, g, w) for patch, (g, w) in combined.items()]


def refresh(
    window: PatchWindow,
    *,
    dsn: str | None = None,
    k: float = scores.DEFAULT_PRIOR_K,
    thresholds: tuple[float, float] = scores.DEFAULT_TIER_THRESHOLDS,
) -> dict[str, int]:
    """Recalcule les counters d'une fenêtre. Retourne le nombre de lignes écrites."""
    patches = list(window.patches)
    resolved_dsn = db.require_dsn(dsn)

    def score_row(platform: str, jgl: int, mid: int, sup: int, role: str, enemy: int, per_patch):
        weights = window.weights_for((jgl, mid, sup, enemy))
        vs = scores.weighted_wr(per_patch, weights)
        if vs is None:
            return None
        trio_per_patch = trios.get((platform, jgl, mid, sup))
        baseline = scores.weighted_wr(trio_per_patch, weights) if trio_per_patch else None
        if baseline is None:
            return None
        delta_raw = vs.wr - baseline.wr
        ci_low, ci_high = scores.wilson_interval(vs.wr, vs.games_eff)
        return {
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

    total = 0
    with (
        psycopg.connect(resolved_dsn) as read_conn,
        psycopg.connect(resolved_dsn) as write_conn,
    ):
        trios = _load_trios(read_conn, patches)

        batch: list[dict] = []
        for (jgl, mid, sup, role, enemy), per_platform in _stream_matchup_groups(
            read_conn, patches
        ):
            for platform, per_patch in per_platform.items():
                row = score_row(platform, jgl, mid, sup, role, enemy, per_patch)
                if row is not None:
                    batch.append(row)
            row = score_row(
                scores.ALL_PLATFORMS, jgl, mid, sup, role, enemy, _combine_platforms(per_platform)
            )
            if row is not None:
                batch.append(row)

            if len(batch) >= BATCH_SIZE:
                with write_conn.cursor() as cur:
                    cur.executemany(_INSERT_SQL, batch)
                write_conn.commit()
                total += len(batch)
                batch = []

        if batch:
            with write_conn.cursor() as cur:
                cur.executemany(_INSERT_SQL, batch)
            write_conn.commit()
            total += len(batch)

    counts = {"score_trio_vs_champion": total}
    logger.info("counters fenêtre %s rafraîchis : %s", window.label, counts)
    return counts
