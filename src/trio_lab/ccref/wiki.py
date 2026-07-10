"""Client minimal de l'API MediaWiki du wiki LoL (import one-shot, Phase 2b).

Exception scopée de CLAUDE.md règle 4 : les *propriétés intrinsèques du jeu*
(types de CC des kits) peuvent être importées via l'API MediaWiki du wiki LoL
(contenu CC BY-SA, attribution requise), par script one-shot avec relecture
humaine avant gel. Jamais de scraping HTML, jamais de données de match ici.

stdlib uniquement (urllib) : pas de dépendance pour un script one-shot.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

API_URL = "https://wiki.leagueoflegends.com/en-us/api.php"
# Ancien wiki (Fandom) : même moteur MediaWiki, mêmes templates de données,
# prose parfois plus détaillée sur les durées — utilisé en 2e passe pour les
# durées manquantes. Contenu CC BY-SA également, mais potentiellement daté
# (le wiki a migré vers wiki.leagueoflegends.com en 2024) → toujours annoté.
FANDOM_API_URL = "https://leagueoflegends.fandom.com/api.php"
USER_AGENT = "trio-lab/0.1 (projet perso; import one-shot cc_reference; script non recurrent)"
# Limite MediaWiki : 50 titres par requête `action=query` pour un utilisateur anonyme.
BATCH_SIZE = 50
# Politesse entre deux requêtes batch (script one-shot, aucune urgence).
BATCH_PAUSE_S = 1.0


def _get(params: dict[str, str], *, api_url: str = API_URL) -> dict:
    query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    request = urllib.request.Request(f"{api_url}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_wikitext(page: str, *, api_url: str = API_URL) -> str:
    """Wikitext d'une page (suit les redirections)."""
    data = _get(
        {"action": "parse", "page": page, "prop": "wikitext", "redirects": "1"},
        api_url=api_url,
    )
    return data["parse"]["wikitext"]


def fetch_many(titles: list[str], *, api_url: str = API_URL) -> dict[str, str | None]:
    """Wikitext de plusieurs pages, par lots de 50, redirections suivies.

    Retourne `{titre demandé: wikitext | None si page absente}`.
    """
    results: dict[str, str | None] = {}
    for i in range(0, len(titles), BATCH_SIZE):
        batch = titles[i : i + BATCH_SIZE]
        data = _get(
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "redirects": "1",
                "titles": "|".join(batch),
            },
            api_url=api_url,
        )
        query = data["query"]
        # titre demandé → titre canonique (normalisation puis redirection).
        resolved = {t: t for t in batch}
        for step in ("normalized", "redirects"):
            for entry in query.get(step, []):
                for requested, target in list(resolved.items()):
                    if target == entry["from"]:
                        resolved[requested] = entry["to"]
        by_title: dict[str, str | None] = {}
        for page in query.get("pages", []):
            if page.get("missing"):
                by_title[page["title"]] = None
            else:
                by_title[page["title"]] = page["revisions"][0]["slots"]["main"]["content"]
        for requested, target in resolved.items():
            results[requested] = by_title.get(target)
        logger.info("batch wiki %d-%d / %d", i + 1, i + len(batch), len(titles))
        if i + BATCH_SIZE < len(titles):
            time.sleep(BATCH_PAUSE_S)
    return results
