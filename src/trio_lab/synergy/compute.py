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
`supabase-disk-growth`, bloat constaté le 14/07/2026).

`agg_duo`/`agg_trio` sont lus en FLUX par pages (`_iter_agg_groups`), pas
chargés intégralement en dicts Python : à ~220k lignes `agg_duo` / 110k
`agg_trio` sur la fenêtre, matérialiser tout (doublé par la vue "toutes
régions") faisait grimper le service collector à 6-7 Go de RAM par cycle —
OOM en prod le 2026-08-01 une fois `refresh_scores` déclenché bien plus
souvent (`DEFAULT_BATCH_TARGET` abaissé). Pagination par CLÉ (pas un curseur
serveur nommé : testé en prod le 2026-08-01, incompatible avec le pooler
Supabase/Supavisor en mode transaction — `server closed the connection
unexpectedly`, chaque FETCH suivant peut atterrir sur une session Postgres
différente) — chaque page est une requête complète et sans état, et chaque
itération ne garde en mémoire que la page courante + les lignes d'UN SEUL
combo en attente, jamais le volume total de la table. `indiv` (agg_champion)
et `duo_synergies` restent des dicts complets en mémoire : bornés par
plateformes × rôles × champions (quelques dizaines de milliers d'entrées au
pire), sans commune mesure avec `agg_duo`/`agg_trio`. `agg_duo_duration`/
`agg_trio_duration` restent chargés à l'ancienne pour l'instant (lignes plus
légères — 4 colonnes contre ~14-20 — mais à re-profiler si les pics
persistent).

La baseline individuelle d'un combo est pondérée avec les MÊMES poids de
fenêtre que le combo (coupure de rework incluse) : la synergie est une
différence, ses deux termes doivent couvrir la même fenêtre (PROJECT.md).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Iterable, Iterator

import psycopg
from psycopg.rows import dict_row

from trio_lab import db
from trio_lab.ccref import score as ccref_score
from trio_lab.synergy import scores
from trio_lab.synergy.windows import PatchWindow

logger = logging.getLogger(__name__)

# `roles` de agg_duo/score_duo → rôles agg_champion de (champ_a, champ_b).
# Les 3 premières paires (internes au trio jgl/mid/sup) viennent de
# match_trio_stats ; les 7 suivantes (Phase 7, duo généralisé) de
# match_role_stats — mêmes tables agg_duo/score_duo en aval, ce dict est
# le seul endroit qui doit connaître les 10 combinaisons.
DUO_ROLES: dict[str, tuple[str, str]] = {
    "jgl_mid": ("JUNGLE", "MIDDLE"),
    "jgl_sup": ("JUNGLE", "UTILITY"),
    "mid_sup": ("MIDDLE", "UTILITY"),
    "top_jgl": ("TOP", "JUNGLE"),
    "top_mid": ("TOP", "MIDDLE"),
    "top_bot": ("TOP", "BOTTOM"),
    "top_sup": ("TOP", "UTILITY"),
    "jgl_bot": ("JUNGLE", "BOTTOM"),
    "mid_bot": ("MIDDLE", "BOTTOM"),
    "bot_sup": ("BOTTOM", "UTILITY"),
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
# team_gold_diff_15 : diff de gold@15 de l'ÉQUIPE ENTIÈRE (5 joueurs, retour
# utilisateur 2026-07-20), pas seulement le trio/duo (gold_diff_15) — sourcée
# sur match_role_stats (migration 032), sans historique profond (patch 16.14+
# seulement) : n reste souvent à 0 sur les patchs plus anciens de la fenêtre,
# `_weighted_stats` retombe alors sur None (jamais d'erreur).
STAT_PAIRS: dict[str, tuple[str, str]] = {
    "gold_diff_5": ("gold5_sum", "gold5_n"),
    "gold_diff_10": ("gold10_sum", "gold10_n"),
    "gold_diff_15": ("gold15_sum", "gold15_n"),
    "team_gold_diff_15": ("team_gold15_sum", "team_gold15_n"),
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
# Portée théorique normalisée 0-100 (retour utilisateur 2026-07-28, compos
# poke) — calculée à part par `_range_pct_fields`, comme le CC, mais UNE
# seule colonne : jamais de mélange empirique (aucune stat Riot ne mesure
# la distance de poke réelle), toujours 100% théorique.
_RANGE_PCT_COLUMNS = ("range_theoretical_pct",)

# CC empirique par membre (migration 020), en plus du total (`cc_time_s` dans
# STAT_PAIRS) : colonnes différentes trio (rôles fixes) vs duo (champ_a/b
# génériques, cf. `stats/aggregate.py`) — deux mappings séparés plutôt qu'un
# STAT_PAIRS partagé, qui suppose le même schéma agg_trio/agg_duo.
_TRIO_POSITION_CC_PAIRS: dict[str, tuple[str, str]] = {
    "jgl_cc_time_s": ("jgl_cc_sum", "jgl_cc_n"),
    "mid_cc_time_s": ("mid_cc_sum", "mid_cc_n"),
    "sup_cc_time_s": ("sup_cc_sum", "sup_cc_n"),
}
_DUO_POSITION_CC_PAIRS: dict[str, tuple[str, str]] = {
    "champ_a_cc_time_s": ("champ_a_cc_sum", "champ_a_cc_n"),
    "champ_b_cc_time_s": ("champ_b_cc_sum", "champ_b_cc_n"),
}

_SCORE_COLUMNS = (*STAT_PAIRS, *_CC_PCT_COLUMNS, *_RANGE_PCT_COLUMNS)
_SCORE_STAT_SQL = ", ".join(_SCORE_COLUMNS)
_SCORE_STAT_PLACEHOLDERS = ", ".join(f"%({name})s" for name in _SCORE_COLUMNS)

# Colonnes agg_trio/agg_duo à charger dans `_load` : le socle partagé + la
# ventilation CC propre à chaque table (schémas différents, cf. plus haut).
_TRIO_STAT_COLUMNS = (
    *_AGG_STAT_COLUMNS,
    *(c for pair in _TRIO_POSITION_CC_PAIRS.values() for c in pair),
)
_DUO_STAT_COLUMNS = (
    *_AGG_STAT_COLUMNS,
    *(c for pair in _DUO_POSITION_CC_PAIRS.values() for c in pair),
)

# Colonnes score_trio/score_duo à écrire : socle partagé + ventilation CC.
_TRIO_SCORE_COLUMNS = (*_SCORE_COLUMNS, *_TRIO_POSITION_CC_PAIRS)
_DUO_SCORE_COLUMNS = (*_SCORE_COLUMNS, *_DUO_POSITION_CC_PAIRS)
_TRIO_SCORE_SQL = ", ".join(_TRIO_SCORE_COLUMNS)
_TRIO_SCORE_PLACEHOLDERS = ", ".join(f"%({name})s" for name in _TRIO_SCORE_COLUMNS)
_DUO_SCORE_SQL = ", ".join(_DUO_SCORE_COLUMNS)
_DUO_SCORE_PLACEHOLDERS = ", ".join(f"%({name})s" for name in _DUO_SCORE_COLUMNS)

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
    *_DUO_SCORE_COLUMNS,
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
    *_TRIO_SCORE_COLUMNS,
)
_TRIO_UPDATE_SQL = ", ".join(f"{c} = EXCLUDED.{c}" for c in _TRIO_UPDATE_COLUMNS)


def _weighted_stats(
    rows: list[dict], weights: dict[str, float], pairs: dict[str, tuple[str, str]] = STAT_PAIRS
) -> dict[str, float | None]:
    """Moyennes de stats pondérées fenêtre : Σw·somme / Σw·n, None sans donnée."""
    out: dict[str, float | None] = {}
    for name, (sum_key, n_key) in pairs.items():
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


def _range_pct_fields(
    member_champions: tuple[int, ...], range_theo_scores: dict[int, float]
) -> dict[str, float | None]:
    """Score de portée théorique normalisé 0-100 (retour utilisateur
    2026-07-28, "compos poke avec de la range") d'une combinaison — réutilise
    `ccref.score.theoretical_pct` (formule générique de normalisation par
    plafond, pas spécifique au CC malgré son nom de module). JAMAIS de
    mélange empirique (contrairement au CC) : aucune stat Riot ne mesure la
    distance de poke réelle en jeu — voir `rangeref/score.py`.

    `range_theo_scores` vide (table `champion_range_theoretical` pas encore
    synchronisée, `python -m trio_lab.rangeref.sync`) : le champ reste
    `None` plutôt que de faire échouer tout le refresh."""
    if not range_theo_scores:
        return {"range_theoretical_pct": None}
    raw = sum(range_theo_scores.get(c, 0.0) for c in member_champions)
    pct = ccref_score.theoretical_pct(
        raw, member_count=len(member_champions), scores=range_theo_scores
    )
    return {"range_theoretical_pct": pct}


# Lignes par page (cf. `_iter_agg_groups`) : chaque page est une requête SQL
# complète et sans état (pas un curseur serveur nommé — testé en prod le
# 2026-08-01 contre le pooler Supabase/Supavisor en mode transaction,
# `server closed the connection unexpectedly` : un curseur nommé suppose la
# MÊME session Postgres entre deux FETCH, que le pooler ne garantit pas). La
# pagination par clé (keyset, ci-dessous) reste bornée en mémoire tout en
# étant compatible avec n'importe quel mode de pooling.
_STREAM_PAGE_SIZE = 5000


def _finalize_combo_group(rows: list[dict], stat_columns: tuple[str, ...]) -> dict[str, list[dict]]:
    """Regroupe les lignes d'UN combo par plateforme + reconstruit la vue
    « toutes régions » (équivalent, sur un seul combo, de
    `scores.add_combined_platform`)."""
    by_platform: dict[str, list[dict]] = defaultdict(list)
    combined_by_patch: dict[str, dict] = {}
    for row in rows:
        by_platform[row["platform"]].append(row)
        cell = combined_by_patch.setdefault(row["patch"], {"patch": row["patch"]})
        for column in stat_columns:
            value = row.get(column)
            if value is not None:
                cell[column] = cell.get(column, 0) + value
    by_platform[scores.ALL_PLATFORMS] = list(combined_by_patch.values())
    return by_platform


def _iter_agg_groups(
    conn: psycopg.Connection,
    table: str,
    patches: list[str],
    key_columns: tuple[str, ...],
    stat_columns: tuple[str, ...],
) -> Iterator[tuple[tuple, dict[str, list[dict]]]]:
    """Flux de `table` (agg_duo/agg_trio) sur `patches`, groupé par combo.

    Pagination par clé (retour utilisateur 2026-08-01, OOM en prod) : à la
    place d'un dict complet indexé par combinaison (jusqu'à ~220k lignes
    `agg_duo` / 110k `agg_trio` sur la fenêtre, doublé par la vue combinée),
    chaque page (`_STREAM_PAGE_SIZE` lignes) est lue par une requête complète
    ordonnée par `(key_columns, platform, patch)` — clé primaire de
    `agg_duo`/`agg_trio` (migration 003), donc un ordre total sans ex-æquo,
    ce qui permet de reprendre exactement là où la page précédente s'est
    arrêtée (`WHERE (...) > dernière ligne vue`) sans jamais sauter ni
    dupliquer de ligne, même si un combo est à cheval sur deux pages. Chaque
    itération ne garde en mémoire que la page courante + les lignes d'UN SEUL
    combo en attente — jamais le volume total de la table.

    Reconstruit aussi la vue « toutes régions » (équivalent streaming de
    `scores.add_combined_platform`) : chaque combo est retourné sous la forme
    `{platform: [lignes dict, ...], ..., "all": [lignes sommées par patch]}`.
    """
    key_sql = ", ".join(key_columns)
    stat_sql = ", ".join(stat_columns)
    order_columns = (*key_columns, "platform", "patch")
    order_sql = ", ".join(order_columns)

    pending_key: tuple | None = None
    pending_rows: list[dict] = []
    after: tuple | None = None
    while True:
        with conn.cursor(row_factory=dict_row) as cur:
            if after is None:
                cur.execute(
                    f"SELECT platform, {key_sql}, patch, {stat_sql}"  # noqa: S608
                    f" FROM {table} WHERE patch = ANY(%s)"
                    f" ORDER BY {order_sql} LIMIT %s",
                    (patches, _STREAM_PAGE_SIZE),
                )
            else:
                placeholders = ", ".join(["%s"] * len(order_columns))
                cur.execute(
                    f"SELECT platform, {key_sql}, patch, {stat_sql}"  # noqa: S608
                    f" FROM {table} WHERE patch = ANY(%s)"
                    f" AND ({order_sql}) > ({placeholders})"
                    f" ORDER BY {order_sql} LIMIT %s",
                    (patches, *after, _STREAM_PAGE_SIZE),
                )
            page = cur.fetchall()
        if not page:
            break
        for row in page:
            key_values = tuple(row[c] for c in key_columns)
            if pending_key is not None and key_values != pending_key:
                yield pending_key, _finalize_combo_group(pending_rows, stat_columns)
                pending_rows = []
            pending_key = key_values
            pending_rows.append(row)
        after = tuple(page[-1][c] for c in order_columns)
        if len(page) < _STREAM_PAGE_SIZE:
            break
    if pending_rows:
        yield pending_key, _finalize_combo_group(pending_rows, stat_columns)


def _load(conn: psycopg.Connection, window: PatchWindow):
    """Charge les petits référentiels de la fenêtre (agg_champion + tables
    théoriques CC/portée) — `agg_duo`/`agg_trio` sont lus en flux à part
    (`_iter_agg_groups`), pas ici."""
    patches = list(window.patches)
    indiv: dict[tuple, _PerPatch] = defaultdict(list)
    for platform, role, champ, patch, games, wins in conn.execute(
        "SELECT platform, role, champion_id, patch, games, wins"
        " FROM agg_champion WHERE patch = ANY(%s)",
        (patches,),
    ):
        indiv[(platform, role, champ)].append((patch, games, wins))

    # Vue « toutes régions » : sommes par patch entre plateformes, matérialisée
    # sous platform='all' comme n'importe quelle autre valeur de colonne.
    scores.add_combined_platform(indiv)

    cc_theo_scores = dict(
        conn.execute("SELECT champion_id, score FROM champion_cc_theoretical").fetchall()
    )
    range_theo_scores = dict(
        conn.execute("SELECT champion_id, score FROM champion_range_theoretical").fetchall()
    )
    return indiv, cc_theo_scores, range_theo_scores


def _load_duration_buckets(
    conn: psycopg.Connection, patches: list[str], *, table: str, key_columns: tuple[str, ...]
) -> dict[tuple, dict[int, _PerPatch]]:
    """Charge `agg_trio_duration`/`agg_duo_duration`, groupé par combo puis par tranche.

    Lu par pages (retour utilisateur 2026-08-01) : jusqu'à ~9 tranches de
    durée par combo, cette table est PLUS grosse que `agg_duo`/`agg_trio` —
    une lecture en un seul bloc dépassait `statement_timeout` en prod, une
    fois `agg_duo`/`agg_trio` eux-mêmes passés en lecture paginée
    (`_iter_agg_groups`). Même principe de pagination par clé (ordre total
    sur `platform, *key_columns, duration_bucket, patch` — sous-ensemble de
    la clé primaire, migration 015), mais remplit ici le même dict complet
    qu'avant : ces lignes restent bien plus légères (4 colonnes) que celles
    d'`agg_duo`/`agg_trio`, pas besoin d'un traitement combo par combo.

    Réutilise `scores.add_combined_platform` (clé `(platform, *rest)`) en
    incluant la tranche dans `*rest` : la vue 'all' est donc déjà sommée par
    tranche, pas seulement par combo.
    """
    columns = ", ".join(key_columns)
    order_sql = f"platform, {columns}, duration_bucket, patch"
    flat: dict[tuple, _PerPatch] = defaultdict(list)
    after: tuple | None = None
    while True:
        with conn.cursor() as cur:
            if after is None:
                cur.execute(
                    f"SELECT platform, {columns}, duration_bucket, patch, games, wins"  # noqa: S608
                    f" FROM {table} WHERE patch = ANY(%s)"
                    f" ORDER BY {order_sql} LIMIT %s",
                    (patches, _STREAM_PAGE_SIZE),
                )
            else:
                placeholders = ", ".join(["%s"] * (3 + len(key_columns)))
                cur.execute(
                    f"SELECT platform, {columns}, duration_bucket, patch, games, wins"  # noqa: S608
                    f" FROM {table} WHERE patch = ANY(%s)"
                    f" AND ({order_sql}) > ({placeholders})"
                    f" ORDER BY {order_sql} LIMIT %s",
                    (patches, *after, _STREAM_PAGE_SIZE),
                )
            page = cur.fetchall()
        for row in page:
            platform, *key, bucket, patch, games, wins = row
            flat[(platform, *key, bucket)].append((patch, games, wins))
        if len(page) < _STREAM_PAGE_SIZE:
            break
        platform, *key, bucket, patch, _games, _wins = page[-1]
        after = (platform, *key, bucket, patch)
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
    # Autocommit (convention `db.py` : chaque instruction est atomique, les
    # écritures multi-lignes ouvrent leur propre `conn.transaction()`) —
    # indispensable depuis le passage en lecture paginée (`_iter_agg_groups`) :
    # une connexion SANS autocommit garde une seule transaction ouverte sur
    # toute la durée de `refresh` (dizaines de pages + calcul Python entre
    # chacune, largement plus long qu'un chargement en un bloc), et Supabase
    # coupe la connexion en cours de route (`server closed the connection
    # unexpectedly`, reproduit en prod le 2026-08-01 à deux reprises).
    with psycopg.connect(db.require_dsn(dsn), autocommit=True) as conn:
        indiv, cc_theo_scores, range_theo_scores = _load(conn, window)
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
        duo_groups = _iter_agg_groups(
            conn, "agg_duo", patches, ("roles", "champ_a", "champ_b"), _DUO_STAT_COLUMNS
        )
        for (roles, a, b), by_platform in duo_groups:
            for platform, agg_rows in by_platform.items():
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
                cc_by_member = _weighted_stats(agg_rows, weights, pairs=_DUO_POSITION_CC_PAIRS)
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
                        **cc_by_member,
                        **_cc_pct_fields(
                            (a, b), cc_theo_scores, stats["cc_time_s"], combo.games_eff, k
                        ),
                        **_range_pct_fields((a, b), range_theo_scores),
                    }
                )

        trio_rows: list[dict] = []
        trio_groups = _iter_agg_groups(
            conn,
            "agg_trio",
            patches,
            ("jgl_champion", "mid_champion", "sup_champion"),
            _TRIO_STAT_COLUMNS,
        )
        for (jgl, mid, sup), by_platform in trio_groups:
            for platform, agg_rows in by_platform.items():
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
                cc_by_member = _weighted_stats(agg_rows, weights, pairs=_TRIO_POSITION_CC_PAIRS)
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
                        **cc_by_member,
                        **_cc_pct_fields(
                            (jgl, mid, sup),
                            cc_theo_scores,
                            stats["cc_time_s"],
                            combo.games_eff,
                            k,
                        ),
                        **_range_pct_fields((jgl, mid, sup), range_theo_scores),
                    }
                )

        with conn.transaction(), conn.cursor() as cur:
            if duo_rows:
                cur.executemany(
                    f"""
                    INSERT INTO score_duo (window_label, platform, roles, champ_a, champ_b,
                                           games, games_eff, wr, synergy, synergy_ci_low,
                                           synergy_ci_high, ci_low, ci_high, tier, scaling,
                                           scaling_ci_low, scaling_ci_high, {_DUO_SCORE_SQL})
                    VALUES (%(window_label)s, %(platform)s, %(roles)s, %(champ_a)s, %(champ_b)s,
                            %(games)s, %(games_eff)s, %(wr)s, %(synergy)s, %(synergy_ci_low)s,
                            %(synergy_ci_high)s, %(ci_low)s, %(ci_high)s, %(tier)s, %(scaling)s,
                            %(scaling_ci_low)s, %(scaling_ci_high)s, {_DUO_SCORE_PLACEHOLDERS})
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
                                            scaling_ci_low, scaling_ci_high, {_TRIO_SCORE_SQL})
                    VALUES (%(window_label)s, %(platform)s, %(jgl_champion)s, %(mid_champion)s,
                            %(sup_champion)s, %(games)s, %(games_eff)s, %(wr)s, %(synergy_raw)s,
                            %(synergy_pred)s, %(synergy)s, %(synergy_ci_low)s,
                            %(synergy_ci_high)s, %(ci_low)s, %(ci_high)s, %(tier)s, %(scaling)s,
                            %(scaling_ci_low)s, %(scaling_ci_high)s, {_TRIO_SCORE_PLACEHOLDERS})
                    ON CONFLICT ({", ".join(_TRIO_PK)}) DO UPDATE SET {_TRIO_UPDATE_SQL}
                    WHERE score_trio.games IS DISTINCT FROM EXCLUDED.games
                    """,
                    trio_rows,
                )
    counts = {"score_duo": len(duo_rows), "score_trio": len(trio_rows)}
    logger.info("scores fenêtre %s rafraîchis : %s", window.label, counts)
    return counts
