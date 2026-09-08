"""Expérimentation ML (scikit-learn/MLflow) — isolée du reste du code.

Contrairement à `synergy/` (stats à la main, `_linalg.py`, volontairement
sans numpy/scipy pour rester léger), ce sous-package a de vraies dépendances
ML (`scikit-learn`, `mlflow`) — installées via l'extra optionnel `ml`
(`pip install -e ".[ml]"`), jamais dans l'image Docker de prod : c'est un
outil d'entraînement/comparaison manuel, pas un service servi en continu.
"""
