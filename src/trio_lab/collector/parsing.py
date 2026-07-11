"""Extraction des lignes Postgres depuis un détail de match (match-v5).

Pas de JSON brut en base (invariant du schéma 001) : on extrait à l'ingestion
les colonnes de `matches` et `match_participants`. Les stats trio issues de la
timeline arrivent en Phase 2 (`match_trio_stats`, `match_objective_events`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from trio_lab.collector import inclusion

ROLES: tuple[str, ...] = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
TEAMS: tuple[int, int] = (100, 200)


class ParseError(ValueError):
    """Détail de match inexploitable (équipes ou rôles incohérents).

    Traité par l'orchestrateur comme une exclusion définitive : les données
    dégradées (teamPosition vide sur AFK précoce, remake…) ne doivent ni entrer
    en base ni être retentées.
    """


def winning_team_of(detail: dict[str, Any]) -> int:
    """Équipe gagnante (100/200) d'un détail de match. Lève `ParseError` si indéterminée.

    Partagé avec l'extraction des stats trio (`trio_lab.stats.extract`).
    """
    winners = [t["teamId"] for t in detail["info"].get("teams", []) if t.get("win")]
    if len(winners) != 1 or winners[0] not in TEAMS:
        raise ParseError(f"équipe gagnante indéterminée : {winners!r}")
    return winners[0]


def match_row(detail: dict[str, Any], *, platform: str) -> dict[str, Any]:
    """Ligne `matches` d'un détail de match. Lève `ParseError` si incohérent."""
    info = detail["info"]
    return {
        "match_id": detail["metadata"]["matchId"],
        "platform": platform,
        "patch": inclusion.patch_of(info["gameVersion"]),
        "game_version": info["gameVersion"],
        "queue_id": info["queueId"],
        "game_creation": datetime.fromtimestamp(info["gameCreation"] / 1000, tz=UTC),
        "game_duration_s": inclusion.game_duration_ms(info) // 1000,
        "winning_team": winning_team_of(detail),
    }


def participant_rows(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Les 10 lignes `match_participants` d'un détail de match.

    Valide la structure attendue par le PK (match_id, team_id, role) : exactement
    5 rôles distincts par équipe. Lève `ParseError` sinon (teamPosition vide ou
    dupliquée sur données dégradées).
    """
    match_id = detail["metadata"]["matchId"]
    rows = [
        {
            "match_id": match_id,
            "team_id": p.get("teamId"),
            "role": p.get("teamPosition"),
            "champion_id": p.get("championId"),
            "win": p.get("win"),
            # CC empirique par champion (migration 005) : None toléré sur les
            # payloads dégradés, la colonne est nullable.
            "cc_time_s": p.get("timeCCingOthers"),
            "immobilizations": p.get("challenges", {}).get("enemyChampionImmobilizations"),
        }
        for p in detail["info"].get("participants", [])
    ]
    expected = {(team, role) for team in TEAMS for role in ROLES}
    got = {(r["team_id"], r["role"]) for r in rows}
    if got != expected or len(rows) != 10:
        raise ParseError(f"rôles/équipes incohérents : {sorted(got - expected)!r}")
    return rows
