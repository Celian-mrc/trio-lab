"""Prédiction d'une draft DONNÉE (10 champions + rôles), pas depuis
l'historique — utile pour tester une draft hypothétique (ex. une game pro)
plutôt qu'évaluer le modèle sur des matchs déjà joués.

ATTENTION domaine (à lire avant d'interpréter un résultat) : le modèle est
entraîné exclusivement sur des games SOLO QUEUE (joueurs Emerald+, cf.
`collector/`). L'appliquer à une draft PRO est un changement de contexte —
comms vocales, jungle scripté, contre-draft délibéré, préparation en scrim :
des dynamiques que le solo queue non coordonné ne capture pas forcément.
Le résultat se lit comme « ce que prédirait le solo queue sur cette
combinaison de champions », pas comme un signal d'analyse pro à proprement
parler.

Utilise le feature set 5 rôles (meilleur résultat de la série, AUC
0,553-0,555) et la politique `> 0` (pas de seuil `MIN_GAMES`, abandonné
après mesure — cf. `ml/features.py`). Modèle entraîné sur 100 % des données
disponibles (pas de split train/test ici : on ne cherche plus à évaluer,
juste à prédire au mieux) et sauvegardé localement (`joblib`, jamais
committé — mêmes conventions que `mlruns/`, cf. `.gitignore`).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import joblib
import numpy as np
import psycopg
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler

from trio_lab import config, db
from trio_lab.ml import features
from trio_lab.web import champions

logger = logging.getLogger(__name__)

_ROLE_ORDER = ("jgl", "mid", "sup", "top", "bot")

DEFAULT_MODEL_PATH = Path("mlruns") / "draft_predict_model.joblib"

# Patch qui ne correspond à AUCUN patch réel de la fenêtre : `_sum_excluding`
# ne exclut rien, donc les baselines utilisent 100 % de la fenêtre — correct
# pour une draft qui n'appartient à aucun match déjà en base (contrairement
# au leave-one-patch-out utilisé à l'entraînement, nécessaire seulement
# parce que CES matchs-là étaient dans la fenêtre elle-même).
_NO_PATCH = "__prediction__"


def train_and_save(
    patches: list[str], *, dsn: str | None = None, out_path: Path = DEFAULT_MODEL_PATH
) -> None:
    """Entraîne sur 100 % des données de `patches` (feature set 5 rôles) et
    sauvegarde modèle + scaler + fenêtre utilisée pour `predict`."""
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        X_raw, y_raw, _ = features.build_five_role_feature_table(conn, patches)
    X = np.array(X_raw, dtype=float)
    y = np.array(y_raw, dtype=int)
    scaler = StandardScaler().fit(X)
    model = HistGradientBoostingClassifier(max_depth=6, learning_rate=0.05, random_state=42)
    model.fit(scaler.transform(X), y)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "scaler": scaler, "patches": patches}, out_path)
    logger.info("modèle de prédiction entraîné sur %d lignes, sauvegardé dans %s", len(X), out_path)


def resolve_champion(name: str, index: dict[int, champions.Champion]) -> int:
    """Nom (insensible à la casse) -> champion_id. Lève `ValueError` si inconnu."""
    lookup = champions.name_lookup(index)
    champ_id = lookup.get(name.casefold())
    if champ_id is None:
        raise ValueError(f"champion inconnu : {name!r}")
    return champ_id


def predict(
    us: dict[str, int],
    them: dict[str, int],
    *,
    dsn: str | None = None,
    model_path: Path = DEFAULT_MODEL_PATH,
) -> float | None:
    """`us`/`them` : `{"jgl": champion_id, "mid": ..., "sup": ..., "top": ...,
    "bot": ...}`. Retourne la probabilité de victoire de `us`, ou `None` si
    une baseline manque (champion/matchup jamais vu dans la fenêtre
    d'entraînement — trop récent, trop rare)."""
    bundle = joblib.load(model_path)
    patches = bundle["patches"]
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        agg_champion = features._fetch_agg_champion(conn, patches)
        agg_duo = features._fetch_agg_duo(conn, patches)
        agg_trio = features._fetch_agg_trio(conn, patches)
        agg_matchup = features._fetch_agg_matchup(conn, patches)

    row = {
        "patch": _NO_PATCH,
        "us_jgl": us["jgl"],
        "us_mid": us["mid"],
        "us_sup": us["sup"],
        "them_jgl": them["jgl"],
        "them_mid": them["mid"],
        "them_sup": them["sup"],
        "us_top": us["top"],
        "us_bot": us["bot"],
        "them_top": them["top"],
        "them_bot": them["bot"],
    }
    feat = features._row_features_5roles(row, agg_champion, agg_duo, agg_trio, agg_matchup, patches)
    if feat is None:
        return None
    X = bundle["scaler"].transform([feat])
    return float(bundle["model"].predict_proba(X)[0, 1])


def _parse_team(raw: str, index: dict[int, champions.Champion]) -> dict[str, int]:
    names = [n.strip() for n in raw.split(",")]
    if len(names) != len(_ROLE_ORDER):
        raise ValueError(
            f"attendu 5 champions séparés par des virgules dans l'ordre "
            f"{','.join(_ROLE_ORDER)} (jgl,mid,sup,top,bot) — reçu {len(names)}"
        )
    return {
        role: resolve_champion(name, index) for role, name in zip(_ROLE_ORDER, names, strict=True)
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="trio_lab.ml.predict",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--patches", help="fenêtre pour --train, ex. 16.17,16.16,16.15 (obligatoire avec --train)"
    )
    parser.add_argument("--train", action="store_true", help="(ré)entraîne et sauvegarde le modèle")
    parser.add_argument(
        "--us", help="5 champions jgl,mid,sup,top,bot séparés par des virgules (ex. --predict)"
    )
    parser.add_argument("--them", help="même format que --us, équipe adverse")
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if args.train:
        if not args.patches:
            parser.error("--train nécessite --patches")
        patches = [p.strip() for p in args.patches.split(",") if p.strip()]
        train_and_save(patches)

    if args.us or args.them:
        if not (args.us and args.them):
            parser.error("--us et --them doivent être fournis ensemble")
        index = champions.fetch_index()
        us = _parse_team(args.us, index)
        them = _parse_team(args.them, index)
        proba = predict(us, them)
        if proba is None:
            print(
                "Baseline manquante pour au moins un champion/matchup (trop récent ou trop rare)."
            )
        else:
            print(f"\nProbabilité de victoire de l'équipe --us : {proba:.1%}")
            print(f"Probabilité de victoire de l'équipe --them : {1 - proba:.1%}")


if __name__ == "__main__":
    main()
