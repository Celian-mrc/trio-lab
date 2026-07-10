"""Constructeurs de timelines synthétiques pour les tests d'extraction.

Complète le builder de match detail de `tests.collector._builders`. Le gold
par joueur est déterministe : `totalGold(minute, pid) = minute * (100 + pid)`,
ce qui rend les sommes de trio vérifiables à la main dans les assertions.
"""

from __future__ import annotations

from typing import Any


def gold_at(minute: int, pid: int) -> int:
    return minute * (100 + pid)


def build_timeline(
    match_id: str = "EUW1_1000000001",
    *,
    minutes: int = 20,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Timeline avec `minutes`+1 frames natives et des events bruts optionnels.

    Les events fournis (format brut Riot, champ `timestamp` requis) sont tous
    placés dans la dernière frame : l'extraction les re-trie par timestamp.
    """
    frames = []
    for minute in range(minutes + 1):
        frames.append(
            {
                "timestamp": minute * 60_000,
                "participantFrames": {
                    str(pid): {"totalGold": gold_at(minute, pid)} for pid in range(1, 11)
                },
                "events": [],
            }
        )
    frames[-1]["events"] = list(events or [])
    return {"metadata": {"matchId": match_id}, "info": {"frames": frames}}


def monster(monster_type: str, team: int, ts_s: int, subtype: str | None = None) -> dict[str, Any]:
    e = {
        "type": "ELITE_MONSTER_KILL",
        "monsterType": monster_type,
        "killerTeamId": team,
        "timestamp": ts_s * 1000,
        "position": {"x": 1, "y": 2},
    }
    if subtype:
        e["monsterSubType"] = subtype
    return e


def tower(owner_team: int, ts_s: int, tower_type: str = "OUTER_TURRET") -> dict[str, Any]:
    return {
        "type": "BUILDING_KILL",
        "buildingType": "TOWER_BUILDING",
        "towerType": tower_type,
        "teamId": owner_team,  # propriétaire qui PERD la tour
        "timestamp": ts_s * 1000,
        "position": {"x": 3, "y": 4},
    }


def kill(killer: int, victim: int, ts_s: int, assists: list[int] | None = None) -> dict[str, Any]:
    return {
        "type": "CHAMPION_KILL",
        "killerId": killer,
        "victimId": victim,
        "assistingParticipantIds": assists or [],
        "timestamp": ts_s * 1000,
        "position": {"x": 5, "y": 6},
    }


def first_blood(killer: int, ts_s: int) -> dict[str, Any]:
    return {
        "type": "CHAMPION_SPECIAL_KILL",
        "killType": "KILL_FIRST_BLOOD",
        "killerId": killer,
        "timestamp": ts_s * 1000,
        "position": {"x": 7, "y": 8},
    }
