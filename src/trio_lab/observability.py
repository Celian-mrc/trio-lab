"""Observabilité optionnelle : Sentry (erreurs) + logs vers Grafana Cloud
(Loki) — retour utilisateur 2026-08-02, incident OOM diagnostiqué à la main
via les logs Railway.

No-op si les variables d'environnement correspondantes sont absentes (dev
local, tests, CI) — jamais requis pour faire tourner le projet.
"""

from __future__ import annotations

import logging
import queue

from trio_lab import config

logger = logging.getLogger(__name__)


def init_sentry() -> None:
    """Initialise Sentry si `SENTRY_DSN` est renseignée, no-op sinon.

    Pas de tracing de performance (`traces_sample_rate=0`) : seule la
    capture d'erreurs nous intéresse ici, pas de coût/quota supplémentaire
    pour rien. `sentry_sdk` capture automatiquement les exceptions non
    gérées et les `logger.exception(...)` (déjà en place partout, ex.
    `collector/service.py`) — aucun changement de code métier nécessaire.
    """
    if not config.SENTRY_DSN:
        return
    import sentry_sdk

    sentry_sdk.init(dsn=config.SENTRY_DSN, traces_sample_rate=0.0)
    logger.info("Sentry initialisé")


def init_loki_logging(service: str) -> None:
    """Envoie les logs (`logging` standard, déjà en place partout) vers
    Grafana Cloud (Loki), no-op si `LOKI_URL`/`LOKI_USER`/`LOKI_TOKEN`
    absents. `service` (`"collector"`/`"web"`) : tag Loki pour filtrer par
    service dans Grafana.

    `LokiQueueHandler` : l'envoi HTTP tourne dans un thread à part (file +
    `QueueListener`), jamais sur le thread appelant `logger.info(...)` — un
    Loki indisponible ne doit jamais ralentir ni faire planter le collector.
    """
    if not (config.LOKI_URL and config.LOKI_USER and config.LOKI_TOKEN):
        return
    import logging_loki

    handler = logging_loki.LokiQueueHandler(
        queue.Queue(-1),
        url=config.LOKI_URL,
        tags={"service": service},
        auth=(config.LOKI_USER, config.LOKI_TOKEN),
        version="1",
    )
    logging.getLogger().addHandler(handler)
    logger.info("Logs Grafana Cloud (Loki) initialisés (service=%s)", service)
