"""Le draft explique-t-il le gold@15 ÉQUIPE réel du match ? (régression)

Manuel, hors service : `python -m trio_lab.ml.train_gold15 --patches
16.17,16.16,16.15`. cf. docstring de `ml/gold15.py` pour le raisonnement
complet (distinguer si le draft n'explique pas le gold@15, ou s'il l'explique
mais que la game entière noie ensuite ce signal).

Mêmes 3 familles de modèles que `ml.train`, en version régression :
`LinearRegression`, `RandomForestRegressor`, `HistGradientBoostingRegressor`.
Métriques : R² (part de variance expliquée — la mesure la plus directe pour
répondre à la question posée), RMSE et MAE (en points de gold, lisibles
directement).
"""

from __future__ import annotations

import argparse
import logging
import zlib

import mlflow
import mlflow.sklearn
import numpy as np
import psycopg
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.preprocessing import StandardScaler

from trio_lab import config, db
from trio_lab.ml import gold15

logger = logging.getLogger(__name__)

_TEST_SPLIT_MOD = 5  # même split que ml.train/win_factors : 1 match sur 5 -> test
EXPERIMENT_NAME = "draft_gold15_regression"

MODEL_FACTORIES = {
    "linear_regression": lambda: LinearRegression(),
    # gradient_boosting AVANT random_forest : HistGradientBoostingRegressor
    # (binning par histogramme) est nettement moins gourmand en mémoire que
    # RandomForestRegressor sur ~2M lignes — utile d'avoir ce résultat même
    # si random_forest doit être retenté/ajusté.
    "gradient_boosting": lambda: HistGradientBoostingRegressor(
        max_depth=6, learning_rate=0.05, random_state=42
    ),
    # n_estimators/max_depth réduits et n_jobs plafonné (retour 2026-09-09) :
    # la config initiale (300 arbres, profondeur 8, n_jobs=-1) a fait
    # dériver le process vers un swap mémoire sur ~2M lignes × 39 features —
    # tué après ~35 min quasi à l'arrêt (progression CPU quasi nulle).
    "random_forest": lambda: RandomForestRegressor(
        n_estimators=100, max_depth=6, min_samples_leaf=50, random_state=42, n_jobs=4
    ),
}


def _is_test_match(match_id: str) -> bool:
    """cf. `ml.train._is_test_match` — même hash, même seuil, dupliqué pour
    la même raison (pas de dépendance croisée entre modules d'expérimentation)."""
    return zlib.crc32(match_id.encode()) % _TEST_SPLIT_MOD == 0


def _train_test_split(
    X: np.ndarray, y: np.ndarray, match_ids: list[str]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    is_test = np.array([_is_test_match(m) for m in match_ids])
    return X[~is_test], X[is_test], y[~is_test], y[is_test]


def _evaluate(model, X_test: np.ndarray, y_test: np.ndarray) -> dict[str, float]:
    pred = model.predict(X_test)
    return {
        "r2": r2_score(y_test, pred),
        "rmse": mean_squared_error(y_test, pred) ** 0.5,
        "mae": mean_absolute_error(y_test, pred),
    }


def run(patches: list[str], *, dsn: str | None = None) -> dict[str, dict[str, float]]:
    with psycopg.connect(db.require_dsn(dsn)) as conn:
        X_raw, y_raw, match_ids = gold15.build_gold15_table(conn, patches)

    X = np.array(X_raw, dtype=float)
    y = np.array(y_raw, dtype=float)
    X_train, X_test, y_train, y_test = _train_test_split(X, y, match_ids)

    scaler = StandardScaler().fit(X_train)
    X_train_scaled = scaler.transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    n_train, n_test = len(y_train), len(y_test)
    logger.info(
        "fenêtre %s : %d lignes train (%.1f %%), %d lignes test (%.1f %%), "
        "gold_diff_15 réel : moyenne %.0f, écart-type %.0f",
        "+".join(patches),
        n_train,
        100 * n_train / (n_train + n_test),
        n_test,
        100 * n_test / (n_train + n_test),
        float(np.mean(y)),
        float(np.std(y)),
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
    parser = argparse.ArgumentParser(prog="trio_lab.ml.train_gold15", description=__doc__)
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
    print(f"\n{'modèle':<20} {'R²':>10} {'RMSE':>10} {'MAE':>10}")
    for name, metrics in results.items():
        print(f"{name:<20} {metrics['r2']:>10.4f} {metrics['rmse']:>10.1f} {metrics['mae']:>10.1f}")


if __name__ == "__main__":
    main()
