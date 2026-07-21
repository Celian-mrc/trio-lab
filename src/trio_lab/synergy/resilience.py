"""Profil de résilience par champion : écart de winrate "en avance" vs "en
retard" sur 3 facteurs (Phase 8, retour utilisateur 2026-07-20).

Répond à une question que les modèles globaux (`win_factors`, `gold_factors`)
ne peuvent pas poser : un coefficient moyen ("×4 de chances de gagner en
avance au gold@15") mélange des champions qui ont besoin de cet avantage
pour gagner et des champions qui s'en passent très bien — Nasus jungle est
mené au gold@15 dans 60 % de ses games (WR 34 % dans cet état précis) mais
son WR global reste ~52 %, signe qu'il compense autrement. Il n'y a pas de
"combinaison parfaite universelle" de métriques : ce module matérialise,
PAR CHAMPION/RÔLE, l'écart de WR entre les 2 états pour identifier qui
tolère quoi.

3 facteurs retenus, choisis empiriquement en session (corrélations de
Pearson sur échantillon prod, ~50k lignes) pour leur signal réel ET leur
indépendance mutuelle :
- `team_gold_diff_15` (r=0,535 avec la victoire) — l'axe le plus fort.
- `jgl_cs_diff_15` (r=0,279 avec la victoire, r=0,456 avec le gold — recoupe
  partiellement mais reste informatif niveau équipe).
- `first_blood_team` (r=0,123 avec la victoire, largement indépendant des 2
  autres, r<0,25 avec chacun).
CC/min écarté : indépendant du gold (r=0,084) mais trop faiblement corrélé
à la victoire (r=0,092) — les écarts par champion seraient surtout du
bruit. Candidat d'extension si le volume grandit.

`team_gold_diff_15` a une zone neutre (±1000 gold, cf. `_NEUTRAL_ZONES`) :
un split brut à 0 comptait un écart de -50 gold comme "en retard" au même
titre que -3000, ce qui diluait le signal (retour utilisateur 2026-07-20).

Calcul PAR APPARITION (une ligne par match/équipe/rôle, comme
`gold_factors`), pas par match : chaque ligne porte les 3 facteurs
TEAM-LEVEL (déjà calculés une fois par équipe) + le champion de CE rôle —
un seul aller-retour SQL pour les 3 facteurs plutôt que 3 requêtes
séparées. Rafraîchi automatiquement à chaque cycle du service 24/24
(`collector/service.py`, depuis le 20/07/2026, retour utilisateur) : coût
mesuré négligeable (~13s pour ~2500 lignes) face à un cycle qui dure déjà
plusieurs minutes (rate limit Riot). `win_factors`/`gold_factors` restent
manuels, eux.
"""

from __future__ import annotations

import argparse
import logging

import psycopg

from trio_lab import config, db
from trio_lab.synergy.windows import PatchWindow, make_window

logger = logging.getLogger(__name__)

FACTORS = ("team_gold_diff_15", "jgl_cs_diff_15", "first_blood_team")
DEFAULT_MIN_ROWS = 200  # sous ce seuil, la fenêtre est ignorée (même esprit que win/gold_factors)

_FETCH_SQL = """
    WITH picks AS (
        SELECT mrs.match_id, mrs.team_id, mrs.role, mrs.gold_15,
               enemy.gold_15 AS enemy_gold_15
        FROM match_role_stats mrs
        JOIN matches m ON m.match_id = mrs.match_id
        JOIN match_role_stats enemy
            ON enemy.match_id = mrs.match_id AND enemy.role = mrs.role
           AND enemy.team_id <> mrs.team_id
        WHERE m.patch = ANY(%(patches)s)
          AND mrs.gold_15 IS NOT NULL AND enemy.gold_15 IS NOT NULL
    ),
    team_agg AS (
        SELECT match_id, team_id,
               sum(gold_15) - sum(enemy_gold_15) AS team_gold_diff_15,
               count(*) AS n_roles
        FROM picks
        GROUP BY match_id, team_id
    ),
    first_blood_agg AS (
        SELECT match_id, team_id, coalesce(bool_or(first_blood), false) AS first_blood_team
        FROM match_role_stats
        GROUP BY match_id, team_id
    )
    SELECT mrs.role, mrs.champion_id, mrs.win,
           ta.team_gold_diff_15, mt.jgl_cs_diff_15, fb.first_blood_team
    FROM match_role_stats mrs
    JOIN matches m ON m.match_id = mrs.match_id
    JOIN team_agg ta ON ta.match_id = mrs.match_id AND ta.team_id = mrs.team_id
    JOIN match_trio_stats mt ON mt.match_id = mrs.match_id AND mt.team_id = mrs.team_id
    JOIN first_blood_agg fb ON fb.match_id = mrs.match_id AND fb.team_id = mrs.team_id
    WHERE m.patch = ANY(%(patches)s) AND ta.n_roles = 5 AND mt.jgl_cs_diff_15 IS NOT NULL
"""


def _fetch_rows(conn: psycopg.Connection, patches: list[str]) -> list[dict]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(_FETCH_SQL, {"patches": patches})
        return cur.fetchall()


# Zone neutre : sous ce seuil (valeur absolue), l'écart est trop faible pour
# compter comme "en avance" OU "en retard" — la game est ignorée pour ce
# facteur plutôt que rangée par le signe brut (retour utilisateur
# 2026-07-20 : un split à 0 comptait -50 gold comme "en retard" au même
# titre que -3000, ce qui diluait le signal). Vérifié empiriquement avant de
# choisir 1000 : écart médian en valeur absolue sur team_gold_diff_15 =
# 2597 gold (fenêtre 16.14+16.13, 287k lignes équipe), un écart franc est la
# norme — exclure ±1000 ne retire que ~21 % des lignes, les plus ambiguës.
# Pas de zone neutre pour jgl_cs_diff_15/first_blood_team : pas demandé, et
# first_blood_team est un booléen (pas de "quasi premier sang").
_NEUTRAL_ZONES: dict[str, float] = {"team_gold_diff_15": 1000.0}


def _is_ahead(value: object) -> bool:
    """État favorable : booléen vrai, ou diff continue ≥ 0."""
    return bool(value) if isinstance(value, bool) else value >= 0


def _aggregate(rows: list[dict]) -> list[dict]:
    """(role, champion_id, factor) → games/wins des 2 côtés (avance/retard)."""
    buckets: dict[tuple[str, int, str], dict[str, int]] = {}
    for r in rows:
        key_base = (r["role"], r["champion_id"])
        for factor in FACTORS:
            value = r[factor]
            zone = _NEUTRAL_ZONES.get(factor, 0.0)
            if zone and not isinstance(value, bool) and abs(value) < zone:
                continue  # écart trop faible pour compter comme avance ou retard
            key = (*key_base, factor)
            bucket = buckets.setdefault(
                key, {"games_ahead": 0, "wins_ahead": 0, "games_behind": 0, "wins_behind": 0}
            )
            side = "ahead" if _is_ahead(value) else "behind"
            bucket[f"games_{side}"] += 1
            if r["win"]:
                bucket[f"wins_{side}"] += 1
    return [
        {"role": role, "champion_id": champ, "factor": factor, **counts}
        for (role, champ, factor), counts in buckets.items()
    ]


_INSERT_SQL = """
    INSERT INTO score_champion_resilience
        (window_label, role, champion_id, factor,
         games_ahead, wins_ahead, games_behind, wins_behind)
    VALUES
        (%(window_label)s, %(role)s, %(champion_id)s, %(factor)s,
         %(games_ahead)s, %(wins_ahead)s, %(games_behind)s, %(wins_behind)s)
"""


def refresh(
    window: PatchWindow, *, dsn: str | None = None, min_rows: int = DEFAULT_MIN_ROWS
) -> int:
    """Matérialise les écarts avance/retard dans `score_champion_resilience`.
    Retourne le nombre de lignes écrites (0 si sous `min_rows`, pas d'erreur).

    DELETE + INSERT (pas UPSERT), même raisonnement que win_factors/gold_factors.
    """
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        # cf. win_factors.refresh / gold_factors.refresh : même piège de
        # planification Postgres (filtre sur n_roles = 5, colonne dérivée
        # d'un agrégat dans une CTE) et même timeout par défaut trop court.
        conn.execute("SET LOCAL statement_timeout = '5min'")
        conn.execute("SET LOCAL enable_nestloop = off")
        rows = _fetch_rows(conn, list(window.patches))
        if len(rows) < min_rows:
            logger.info(
                "resilience %s : %d lignes < seuil %d, ignoré", window.label, len(rows), min_rows
            )
            count = 0
            rows_to_write: list[dict] = []
        else:
            aggregated = _aggregate(rows)
            rows_to_write = [{"window_label": window.label, **a} for a in aggregated]
            count = len(rows_to_write)
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "DELETE FROM score_champion_resilience WHERE window_label = %s", (window.label,)
            )
            if rows_to_write:
                cur.executemany(_INSERT_SQL, rows_to_write)
    logger.info("resilience fenêtre %s rafraîchie : %d lignes", window.label, count)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.synergy.resilience", description=__doc__)
    parser.add_argument(
        "--patches", required=True, help="fenêtre, du plus récent au plus ancien, ex. 16.14,16.13"
    )
    parser.add_argument("--min-rows", type=int, default=DEFAULT_MIN_ROWS)
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    window = make_window([p.strip() for p in args.patches.split(",") if p.strip()])
    refresh(window, min_rows=args.min_rows)


if __name__ == "__main__":
    main()
