# Image unique pour les deux services Railway (collector et web) : seule la
# start command diffère (définie par service dans le dashboard). Un Dockerfile
# plutôt que le builder auto (Railpack) : ce dernier installe les dépendances
# AVANT de copier les sources, ce qui est incompatible avec `pip install .`
# d'un paquet local (README/src absents au moment de l'install).

FROM python:3.13-slim

WORKDIR /app

# Métadonnées + sources d'abord : l'install du paquet a besoin des deux.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Migrations embarquées pour pouvoir lancer `python -m trio_lab.db` en one-off.
COPY migrations ./migrations

# Défaut : l'interface. Le service collector surcharge la start command
# (`python -m trio_lab.collector --service`) dans Railway.
CMD ["python", "-m", "trio_lab.web"]
