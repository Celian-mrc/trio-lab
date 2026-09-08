"""Entraînement et comparaison de 3 modèles de winrate au draft (v1).

Manuel, hors service (comme `win_factors`/`gold_factors`) :
`python -m trio_lab.ml.train --patches 16.17,16.16,16.15`.

3 modèles comparés sur les mêmes features (`ml.features.FEATURE_NAMES`) :
- `logistic_regression` : baseline linéaire, coefficients interprétables
  (le pendant scikit-learn de `synergy.win_factors`, qui ajuste sa propre
  régression logistique à la main via `_linalg`).
- `random_forest` : ensemble d'arbres, robuste, peu de tuning — sert de
  garde-fou pour détecter un gradient boosting en surapprentissage.
- `gradient_boosting` (`HistGradientBoostingClassifier`) : capture les
  interactions entre features qu'un modèle linéaire ne voit pas. Choisi
  plutôt que LightGBM/XGBoost pour rester dans scikit-learn (déjà une
  dépendance du projet pour ce module) sans ajouter un 2e paquet ML lourd —
  XGBoost/LightGBM restent une amélioration facile si besoin plus tard.

Split train/test déterministe par hash du `match_id` (même mécanique que
`win_factors._is_test_match`) : les 2 lignes d'un même match (une par équipe)
tombent toujours du même côté, jamais mélangées entre train et test.

Chaque modèle = un run MLflow séparé (tracking local, dossier `mlruns/` —
`mlflow ui` pour comparer). Accuracy/AUC ET calibration (log loss, Brier
score) sont loggées : la sortie doit être un vrai pourcentage affichable, pas
juste une bonne classification binaire (cf. session, calibration d'un GBM
brut vs régression logistique).
"""

from __future__ import annotations

import argparse
import logging
import zlib

import mlflow
import mlflow.sklearn
import numpy as np
import psycopg
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from trio_lab import config, db
from trio_lab.ml import features

logger = logging.getLogger(__name__)

_TEST_SPLIT_MOD = 5  # même split que win_factors.py : 1 match sur 5 -> test
EXPERIMENT_NAME = "draft_win_probability"

MODEL_FACTORIES = {
    "logistic_regression": lambda: LogisticRegression(max_iter=1000),
    "random_forest": lambda: RandomForestClassifier(
        n_estimators=300, max_depth=8, min_samples_leaf=20, random_state=42, n_jobs=-1
    ),
    "gradient_boosting": lambda: HistGradientBoostingClassifier(
        max_depth=6, learning_rate=0.05, random_state=42
    ),
}


def _is_test_match(match_id: str) -> bool:
    """cf. `win_factors._is_test_match` — même hash, même seuil, dupliqué ici
    plutôt qu'importé : `ml/` reste découplé de `synergy/` (pas de dépendance
    croisée entre le code stats-maison et le code scikit-learn)."""
    return zlib.crc32(match_id.encode()) % _TEST_SPLIT_MOD == 0


def _train_test_split(
    X: np.ndarray, y: np.ndarray, match_ids: list[str]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    is_test = np.array([_is_test_match(m) for m in match_ids])
    return X[~is_test], X[is_test], y[~is_test], y[is_test]


def _evaluate(model, X_test: np.ndarray, y_test: np.ndarray) -> dict[str, float]:
    proba = model.predict_proba(X_test)[:, 1]
    return {
        "accuracy": accuracy_score(y_test, model.predict(X_test)),
        "auc": roc_auc_score(y_test, proba),
        "log_loss": log_loss(y_test, proba),
        "brier_score": brier_score_loss(y_test, proba),
    }


def run(patches: list[str], *, dsn: str | None = None) -> dict[str, dict[str, float]]:
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        X_raw, y_raw, match_ids = features.build_feature_table(conn, patches)

    X = np.array(X_raw, dtype=float)
    y = np.array(y_raw, dtype=int)
    X_train, X_test, y_train, y_test = _train_test_split(X, y, match_ids)

    # Standardisation utile à la régression logistique, sans effet (mais sans
    # nuire) sur les 2 modèles à base d'arbres — un seul pipeline de
    # prétraitement partagé plutôt que 3 chemins de code différents. Ajustée
    # sur le train uniquement, appliquée telle quelle au test (pas de fuite).
    scaler = StandardScaler().fit(X_train)
    X_train_scaled = scaler.transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    n_train, n_test = len(y_train), len(y_test)
    logger.info(
        "fenêtre %s : %d lignes train (%.1f %%), %d lignes test (%.1f %%)",
        "+".join(patches),
        n_train,
        100 * n_train / (n_train + n_test),
        n_test,
        100 * n_test / (n_train + n_test),
    )

    mlflow.set_experiment(EXPERIMENT_NAME)
    results: dict[str, dict[str, float]] = {}
    for name, make_model in MODEL_FACTORIES.items():
        with mlflow.start_run(run_name=name):
            model = make_model()
            model.fit(X_train_scaled, y_train)
            metrics = _evaluate(model, X_test_scaled, y_test)

            mlflow.log_param("window_patches", "+".join(patches))
            mlflow.log_param("n_train", n_train)
            mlflow.log_param("n_test", n_test)
            mlflow.log_param("n_features", X.shape[1])
            mlflow.log_params({f"model__{k}": v for k, v in model.get_params().items()})
            mlflow.log_metrics(metrics)
            mlflow.sklearn.log_model(model, name)

            results[name] = metrics
            logger.info("modèle %s : %s", name, metrics)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(prog="trio_lab.ml.train", description=__doc__)
    parser.add_argument(
        "--patches",
        required=True,
        help="fenêtre, du plus récent au plus ancien, ex. 16.17,16.16,16.15",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=config.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    patches = [p.strip() for p in args.patches.split(",") if p.strip()]
    results = run(patches)
    print(f"\n{'modèle':<20} {'accuracy':>10} {'AUC':>10} {'log loss':>10} {'brier':>10}")
    for name, metrics in results.items():
        print(
            f"{name:<20} {metrics['accuracy']:>10.4f} {metrics['auc']:>10.4f} "
            f"{metrics['log_loss']:>10.4f} {metrics['brier_score']:>10.4f}"
        )


if __name__ == "__main__":
    main()
