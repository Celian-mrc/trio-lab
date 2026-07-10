"""Mapping centralisé patch → fenêtre temporelle (repris de macro-lab).

Le bornage temporel des requêtes `match-v5/ids` réduit drastiquement le nombre
d'appels `detail` inutiles (matchs hors-patch). Le filtre `gameVersion` (cf.
`inclusion.py`) reste l'autorité finale sur l'appartenance au patch — ce bornage
n'est qu'un pré-filtre économique approximatif.

Convention de remplissage :
- `start_time` = mise en production du patch sur la plus précoce des régions
  visées (KR patche avant EUW/NA) ;
- `end_time`   = mise en production du patch suivant (provisoire tant que le
  suivant n'est pas daté officiellement).
- Bornes stockées en **UTC tz-aware** ; `gameVersion` tranche les quelques
  heures de chevauchement, un léger excès est sans danger.

⚠️ Double nommage : la saison 2026 est brandée "26.x" dans les patch notes,
mais l'API renvoie "16.x" dans `gameVersion`. On clé sur la valeur API (16.x).
"""

from __future__ import annotations

from datetime import UTC, datetime

# patch "major.minor" (valeur API `gameVersion`) → (start, end) UTC.
# Sources : patch notes officielles + LoL Wiki (Patch/2026_Annual_Cycle).
PATCH_DATES: dict[str, tuple[datetime, datetime]] = {
    "16.12": (  # branding 26.12
        datetime(2026, 6, 10, 0, tzinfo=UTC),
        datetime(2026, 6, 24, 0, tzinfo=UTC),
    ),
    "16.13": (  # branding 26.13 — patch courant au 10/07/2026
        datetime(2026, 6, 24, 0, tzinfo=UTC),
        # 26.14 annoncé pour le mercredi 15/07/2026.
        datetime(2026, 7, 15, 0, tzinfo=UTC),
    ),
    "16.14": (  # branding 26.14 — live le 15/07/2026
        datetime(2026, 7, 15, 0, tzinfo=UTC),
        # end provisoire : 26.15 attendu ~29/07 (cadence 2 semaines), à confirmer.
        datetime(2026, 7, 29, 0, tzinfo=UTC),
    ),
}


def to_epoch_seconds(dt: datetime) -> int:
    """Convertit un datetime tz-aware en epoch **secondes** (unité de `match-v5/ids`)."""
    return int(dt.timestamp())


def bounds_for(patch: str) -> tuple[datetime, datetime]:
    """Retourne `(start_time, end_time)` UTC pour un patch.

    Lève `ValueError` explicite si le patch n'est pas dans `PATCH_DATES` — il faut
    alors l'ajouter manuellement (dates depuis les patch notes officielles).
    """
    try:
        return PATCH_DATES[patch]
    except KeyError:
        raise ValueError(
            f"Patch {patch!r} absent de PATCH_DATES. "
            f"Ajoute-le à src/trio_lab/collector/patches.py "
            f"(dates issues des patch notes officielles, en UTC). "
            f"Patches connus : {sorted(PATCH_DATES)}"
        ) from None


def epoch_bounds_for(patch: str) -> tuple[int, int]:
    """Bornes du patch directement en epoch secondes, prêtes pour `match-v5/ids`."""
    start, end = bounds_for(patch)
    return to_epoch_seconds(start), to_epoch_seconds(end)
