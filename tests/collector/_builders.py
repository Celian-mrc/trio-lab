"""Constructeurs de payloads match-v5 synthétiques mais structurellement réalistes.

Partagés entre les tests de parsing et d'orchestration. Les champion_ids sont
arbitraires ; seule la structure (10 participants, 5 rôles × 2 équipes, champs
d'info) compte pour la Phase 1.
"""

from __future__ import annotations

from typing import Any

from trio_lab.collector.parsing import ROLES, TEAMS


def build_detail(
    match_id: str = "EUW1_1000000001",
    *,
    patch: str = "16.13",
    queue_id: int = 420,
    duration_s: int = 1800,
    winning_team: int = 100,
    game_creation_ms: int = 1_780_000_000_000,
    early_surrender: bool = False,
) -> dict[str, Any]:
    """Détail de match valide (10 participants, rôles complets, un gagnant)."""
    participants = []
    for team_index, team in enumerate(TEAMS):
        for role_index, role in enumerate(ROLES):
            pid = 5 * team_index + role_index + 1
            participants.append(
                {
                    "participantId": pid,
                    "teamId": team,
                    "teamPosition": role,
                    "championId": 10 * team_index + role_index + 1,
                    "win": team == winning_team,
                    # Stats de fin de match déterministes (tests d'extraction Phase 2).
                    "visionScore": pid,
                    "timeCCingOthers": pid * 2,
                    "totalDamageDealtToChampions": pid * 1000,
                    "firstBloodKill": False,
                    "firstBloodAssist": False,
                    "challenges": {"turretPlatesTaken": 1, "enemyChampionImmobilizations": pid},
                }
            )
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "queueId": queue_id,
            "gameVersion": f"{patch}.673.9817",
            "gameCreation": game_creation_ms,
            "gameDuration": duration_s,  # secondes car gameEndTimestamp présent
            "gameEndTimestamp": game_creation_ms + duration_s * 1000,
            "gameEndedInEarlySurrender": early_surrender,
            "teams": [
                {"teamId": 100, "win": winning_team == 100},
                {"teamId": 200, "win": winning_team == 200},
            ],
            "participants": participants,
        },
    }
