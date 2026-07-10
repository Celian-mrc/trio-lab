"""Fenêtre multi-patchs glissante (Phase 3).

Les patchs ne sont jamais fusionnés au stockage (CLAUDE.md règle 5) : la
fenêtre est une pondération de lecture — patchs récents pondérés plus fort
(1.0 / 0.6 / 0.35 par défaut, cf. PROJECT.md), et poids **zéro** pour les
patchs antérieurs au rework d'un des champions considérés (un Skarner
pré-rework n'est pas le même champion).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Pondération décroissante des patchs, du plus récent au plus ancien.
DEFAULT_WEIGHTS: tuple[float, ...] = (1.0, 0.6, 0.35)

# champion_id → premier patch (format API "16.x") de sa version retravaillée.
# À compléter à chaque rework majeur (VGU / changement de kit) ; les patchs
# antérieurs sont exclus de toute fenêtre impliquant ce champion.
# Vide au 2026-07-10 : aucun rework depuis le début de la collecte (16.13).
REWORKS: dict[int, str] = {}


def patch_key(patch: str) -> tuple[int, int]:
    """Clé de tri chronologique d'un patch "major.minor" ("16.13" → (16, 13))."""
    major, minor = patch.split(".")[:2]
    return int(major), int(minor)


@dataclass(frozen=True)
class PatchWindow:
    """Fenêtre ordonnée du patch le plus récent au plus ancien, avec ses poids."""

    patches: tuple[str, ...]
    weights: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.patches:
            raise ValueError("fenêtre vide")
        if len(self.weights) < len(self.patches):
            raise ValueError(
                f"{len(self.patches)} patchs mais {len(self.weights)} poids ({self.weights!r})"
            )
        ordered = sorted(self.patches, key=patch_key, reverse=True)
        if list(self.patches) != ordered:
            raise ValueError(f"patchs non ordonnés du plus récent au plus ancien : {self.patches}")

    @property
    def label(self) -> str:
        """Étiquette de matérialisation, ex. '16.13+16.12'."""
        return "+".join(self.patches)

    def weights_for(self, champion_ids: Iterable[int]) -> dict[str, float]:
        """Poids par patch pour une combinaison de champions donnée.

        Un patch antérieur au rework d'un des champions reçoit un poids nul
        (coupure de fenêtre) ; il reste dans le dict pour que l'appelant sache
        qu'il a été considéré puis écarté.
        """
        cutoff: tuple[int, int] | None = None
        for champion_id in champion_ids:
            rework_patch = REWORKS.get(champion_id)
            if rework_patch is not None:
                key = patch_key(rework_patch)
                if cutoff is None or key > cutoff:
                    cutoff = key
        weights = {}
        for patch, weight in zip(self.patches, self.weights, strict=False):
            excluded = cutoff is not None and patch_key(patch) < cutoff
            weights[patch] = 0.0 if excluded else weight
        return weights


def make_window(patches: list[str], weights: tuple[float, ...] = DEFAULT_WEIGHTS) -> PatchWindow:
    """Fenêtre depuis une liste de patchs (1 à 3, du plus récent au plus ancien)."""
    if not 1 <= len(patches) <= 3:
        raise ValueError(f"fenêtre de 1 à 3 patchs attendue, reçu {patches!r}")
    return PatchWindow(tuple(patches), tuple(weights[: len(patches)]))
