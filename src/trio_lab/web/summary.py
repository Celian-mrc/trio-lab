"""Résumé statistique détaillé d'un trio sur une fenêtre — module pur, zéro I/O.

Agrège en Python les lignes `match_trio_stats` (+ durée du match) d'un trio :
moyennes et taux pondérés par les poids de patch de la fenêtre (les MÊMES que
le score de synergie, coupure de rework incluse). Les NULL sont ignorés
colonne par colonne — une partie finie avant 20 min ne compte ni au numérateur
ni au dénominateur de `gold_diff_20`.

Le volume par trio reste modeste (quelques milliers de games au pire pour un
trio populaire à ~1M matchs/patch) : l'agrégation à la volée évite une table
matérialisée de plus tant que la lecture reste instantanée.
"""

from __future__ import annotations

from collections.abc import Iterable

GOLD_MINUTES = (5, 10, 15, 20, 25, 30, 35)

# Colonnes moyennées telles quelles (les bools deviennent des taux 0-1).
# vision_score, drakes_taken, cc_time_s sont traités à part (par minute, cf.
# `_per_minute`) : le cumulé brut est mécaniquement gonflé par la durée de la
# partie (retour utilisateur, 2026-07-13).
_MEAN_KEYS = (
    "grubs_taken",
    "herald_taken",
    "soul_taken",
    "nashor_first",
    "nashor_first_s",
    "first_tower",
    "towers_destroyed",
    "plates_taken",
    "first_blood_trio",
    "kill_participation_pre15",
    "damage_share",
)
# jgl/mid/sup_cc_time_s (migration 020) : ventilation par membre du CC déjà
# sommé dans cc_time_s — calculée pour trio ET duo (le duo n'a besoin que de
# 2 des 3, choisis par `app._duo_detail` selon `roles`).
_PER_MINUTE_KEYS = (
    "vision_score",
    "drakes_taken",
    "cc_time_s",
    "jgl_cc_time_s",
    "mid_cc_time_s",
    "sup_cc_time_s",
)


def _weighted_mean(rows: Iterable[dict], weights: dict[str, float], key: str) -> float | None:
    """Moyenne pondérée de `key`, NULLs et patchs hors fenêtre ignorés."""
    return _weighted_mean_of(rows, weights, lambda row: row.get(key))


def _weighted_mean_of(rows: Iterable[dict], weights: dict[str, float], value_of) -> float | None:
    """Comme `_weighted_mean`, mais la valeur est calculée par `value_of(row)`
    plutôt que lue directement à une clé (ex. un ratio dérivé de 2 colonnes)."""
    num = 0.0
    den = 0.0
    for row in rows:
        weight = weights.get(row["patch"], 0.0)
        value = value_of(row)
        if weight <= 0.0 or value is None:
            continue
        num += weight * float(value)
        den += weight
    return num / den if den > 0.0 else None


def _per_minute(key: str):
    def value_of(row: dict) -> float | None:
        value = row.get(key)
        duration = row.get("game_duration_s")
        if value is None or not duration:
            return None
        return value / (duration / 60.0)

    return value_of


def summarize(rows: list[dict], weights: dict[str, float]) -> dict:
    """Toutes les stats détaillées d'un trio (PROJECT.md « Statistiques par trio »).

    `rows` : lignes match_trio_stats du trio, enrichies de `patch` et
    `game_duration_s`. Retourne un dict prêt pour le template / l'API JSON.
    """
    in_window = [r for r in rows if weights.get(r["patch"], 0.0) > 0.0]
    wins = [r for r in in_window if r["win"]]
    losses = [r for r in in_window if not r["win"]]
    return {
        "games": len(in_window),
        "wr": _weighted_mean(in_window, weights, "win"),
        "gold_diff": {
            m: _weighted_mean(in_window, weights, f"gold_diff_{m}") for m in GOLD_MINUTES
        },
        **{key: _weighted_mean(in_window, weights, key) for key in _MEAN_KEYS},
        **{
            key: _weighted_mean_of(in_window, weights, _per_minute(key)) for key in _PER_MINUTE_KEYS
        },
        # WR selon l'obtention de l'âme (proxy « âme perdue » côté without :
        # on ne stocke pas si l'adversaire l'a prise).
        "wr_with_soul": _weighted_mean(
            [r for r in in_window if r.get("soul_taken") is True], weights, "win"
        ),
        "wr_without_soul": _weighted_mean(
            [r for r in in_window if r.get("soul_taken") is False], weights, "win"
        ),
        # Profil de tempo : un trio early-game gagne court et perd long.
        "avg_duration_win_s": _weighted_mean(wins, weights, "game_duration_s"),
        "avg_duration_loss_s": _weighted_mean(losses, weights, "game_duration_s"),
    }
