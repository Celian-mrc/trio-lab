"""Critères d'inclusion d'un match Riot (repris du collector de macro-lab).

Règle canonique :
- **Exclus** si `gameDuration < 300_000 ms` **OU** `gameEndedInEarlySurrender = true`.
- **Inclus** : tout le reste (FF standards compris).

Ajouts propres à la collecte :
- on ne garde que la **SoloQ** (`queueId == 420`) ;
- on vérifie l'appartenance au patch via `gameVersion` (autorité finale — le
  bornage temporel de `patches.py` n'est qu'un pré-filtre économique).

⚠️ Piège d'unité `gameDuration` (match-v5) : depuis un changement Riot,
`gameDuration` est en **secondes** lorsque `gameEndTimestamp` est présent, en
**millisecondes** sinon. `game_duration_ms` normalise ça.
"""

from __future__ import annotations

from typing import Any

MIN_DURATION_MS = 300_000  # 5 minutes
SOLOQ_QUEUE = 420


def patch_of(game_version: str) -> str:
    """Extrait le patch "major.minor" d'un `gameVersion` (ex. "16.13.673.9817" → "16.13")."""
    parts = game_version.split(".")
    return ".".join(parts[:2])


def game_duration_ms(info: dict[str, Any]) -> int:
    """Durée du match en millisecondes, en gérant le piège secondes/ms de match-v5."""
    duration = int(info["gameDuration"])
    # Si gameEndTimestamp est présent, gameDuration est en secondes.
    if "gameEndTimestamp" in info:
        return duration * 1000
    return duration


def is_included(match_detail: dict[str, Any], patch: str) -> tuple[bool, str | None]:
    """Indique si un match doit être collecté pour `patch`.

    Retourne `(True, None)` si inclus, sinon `(False, reason)` avec
    `reason ∈ {"queue", "duration", "early_surrender", "wrong_patch"}`.
    """
    info = match_detail["info"]

    if info.get("queueId") != SOLOQ_QUEUE:
        return False, "queue"
    if info.get("gameEndedInEarlySurrender", False):
        return False, "early_surrender"
    if game_duration_ms(info) < MIN_DURATION_MS:
        return False, "duration"
    if patch_of(info["gameVersion"]) != patch:
        return False, "wrong_patch"
    return True, None
