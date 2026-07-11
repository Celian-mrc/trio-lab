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

import json
import logging
import urllib.request
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

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


# --- Mode service (Phase 6) : patch courant auto, bornes de repli ---

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
_USER_AGENT = "trio-lab/0.1 (resolution du patch courant)"
# Repli quand le patch courant n'est pas dans PATCH_DATES : large fenêtre
# glissante (cadence patch = 2 semaines + marge). Le bornage n'est qu'un
# pré-filtre économique — `gameVersion` reste l'autorité (inclusion.py), on
# paie juste quelques appels detail de plus sur les matchs du patch précédent.
_FALLBACK_LOOKBACK = timedelta(days=16)
_FALLBACK_LOOKAHEAD = timedelta(days=2)


def from_version(version: str) -> str:
    """Version Data Dragon → patch API "major.minor" ("16.14.1" → "16.14")."""
    major, minor = version.split(".")[:2]
    return f"{major}.{minor}"


def _fetch_versions() -> list[str]:
    request = urllib.request.Request(VERSIONS_URL, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def current_patch() -> str:
    """Patch courant (valeur API "16.x") via Data Dragon — pas de clé requise.

    Data Dragon publie la nouvelle version quelques heures après la mise en
    production : au pire, le service collecte quelques heures de plus sur le
    patch sortant, ce qui est correct (les matchs restent tagués `patch`).
    """
    patch = from_version(_fetch_versions()[0])
    logger.info("patch courant Data Dragon : %s", patch)
    return patch


def service_bounds_for(patch: str) -> tuple[datetime, datetime]:
    """`bounds_for` avec repli pour le mode service 24/24.

    Un patch absent de PATCH_DATES ne doit pas tuer le service : on borne
    largement autour de maintenant et on signale qu'il manque les dates.
    """
    try:
        return bounds_for(patch)
    except ValueError:
        now = datetime.now(UTC)
        logger.warning(
            "patch %s absent de PATCH_DATES : bornes de repli (−%d j / +%d j) — "
            "ajouter les dates officielles dans patches.py à l'occasion",
            patch,
            _FALLBACK_LOOKBACK.days,
            _FALLBACK_LOOKAHEAD.days,
        )
        return now - _FALLBACK_LOOKBACK, now + _FALLBACK_LOOKAHEAD
