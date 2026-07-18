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

Idempotent par fenêtre via UPSERT, pas DELETE+INSERT (mêmes raisons que
`synergy/compute.py` — l'ensemble des clés d'une fenêtre ne fait que
grossir d'un cycle à l'autre, cf. mémoire `supabase-disk-growth`).

Streaming (18/07/2026) : même correctif que `counters.py`, même cause
(pool de joueurs élargi 5→100 pages → `agg_trio_with_ally` trop gros pour
tenir en mémoire, faisait planter le collecteur à chaque cycle). Lecture par
curseur serveur trié par combo puis plateforme, écriture par lots committés
sur une connexion séparée.
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
    "ally_role",
    "ally_champion",
)
_UPDATE_COLUMNS = ("games", "games_eff", "wr", "uplift_raw", "uplift", "ci_low", "ci_high", "tier")
_UPDATE_SQL = ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPDATE_COLUMNS)
_INSERT_SQL = f"""
    INSERT INTO score_trio_with_ally (
        window_label, platform, jgl_champion, mid_champion, sup_champion,
        ally_role, ally_champion, games, games_eff, wr, uplift_raw, uplift,
        ci_low, ci_high, tier)
    VALUES (%(window_label)s, %(platform)s, %(jgl_champion)s,
            %(mid_champion)s, %(sup_champion)s, %(ally_role)s,
            %(ally_champion)s, %(games)s, %(games_eff)s, %(wr)s,
            %(uplift_raw)s, %(uplift)s, %(ci_low)s, %(ci_high)s, %(tier)s)
    ON CONFLICT ({", ".join(_PK)}) DO UPDATE SET {_UPDATE_SQL}
    WHERE score_trio_with_ally.games IS DISTINCT FROM EXCLUDED.games
"""
# Nombre de lignes accumulées avant un commit — borne la mémoire du lot
# d'écriture sans multiplier les aller-retours réseau.
BATCH_SIZE = 5000


def _load_trios(conn: psycopg.Connection, patches: list[str]) -> dict[tuple, _PerPatch]:
    """WR agrégés par trio (baseline) — bien plus petit que les alliés, gardé en mémoire."""
    trios: dict[tuple, _PerPatch] = {}
    for platform, jgl, mid, sup, patch, games, wins in conn.execute(
        "SELECT platform, jgl_champion, mid_champion, sup_champion, patch, games, wins"
        " FROM agg_trio WHERE patch = ANY(%s)",
        (patches,),
    ):
        trios.setdefault((platform, jgl, mid, sup), []).append((patch, games, wins))
    scores.add_combined_platform(trios)
    return trios


def _stream_ally_groups(
    conn: psycopg.Connection, patches: list[str]
) -> Iterator[tuple[tuple, dict[str, _PerPatch]]]:
    """Lignes `agg_trio_with_ally` groupées par combo, plateformes rassemblées.

    Trié par combo puis plateforme : toutes les plateformes d'un même combo
    arrivent à la suite, ce qui permet de calculer la vue « toutes régions »
    sans jamais charger tous les alliés en mémoire à la fois.
    """
    with conn.cursor(name="allies_groups") as cur:
        cur.itersize = BATCH_SIZE
        cur.execute(
            "SELECT jgl_champion, mid_champion, sup_champion, ally_role, ally_champion,"
            " platform, patch, games, wins"
            " FROM agg_trio_with_ally WHERE patch = ANY(%s)"
            " ORDER BY jgl_champion, mid_champion, sup_champion, ally_role, ally_champion,"
            " platform",
            (patches,),
        )
        current_combo: tuple | None = None
        per_platform: dict[str, _PerPatch] = {}
        for jgl, mid, sup, role, ally, platform, patch, games, wins in cur:
            combo = (jgl, mid, sup, role, ally)
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
    """Recalcule les meilleurs alliés d'une fenêtre. Retourne le nombre de lignes écrites."""
    patches = list(window.patches)
    resolved_dsn = db.require_dsn(dsn)

    def score_row(platform: str, jgl: int, mid: int, sup: int, role: str, ally: int, per_patch):
        weights = window.weights_for((jgl, mid, sup, ally))
        with_ally = scores.weighted_wr(per_patch, weights)
        if with_ally is None:
            return None
        trio_per_patch = trios.get((platform, jgl, mid, sup))
        baseline = scores.weighted_wr(trio_per_patch, weights) if trio_per_patch else None
        if baseline is None:
            return None
        uplift_raw = with_ally.wr - baseline.wr
        ci_low, ci_high = scores.wilson_interval(with_ally.wr, with_ally.games_eff)
        return {
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

    total = 0
    with (
        psycopg.connect(resolved_dsn) as read_conn,
        psycopg.connect(resolved_dsn) as write_conn,
    ):
        trios = _load_trios(read_conn, patches)

        batch: list[dict] = []
        for (jgl, mid, sup, role, ally), per_platform in _stream_ally_groups(read_conn, patches):
            for platform, per_patch in per_platform.items():
                row = score_row(platform, jgl, mid, sup, role, ally, per_patch)
                if row is not None:
                    batch.append(row)
            row = score_row(
                scores.ALL_PLATFORMS, jgl, mid, sup, role, ally, _combine_platforms(per_platform)
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

    counts = {"score_trio_with_ally": total}
    logger.info("alliés fenêtre %s rafraîchis : %s", window.label, counts)
    return counts
