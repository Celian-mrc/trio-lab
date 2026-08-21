"""Application FastAPI : pages Jinja2 (htmx en hx-boost) + API JSON de lecture.

Les routes sont des `def` synchrones (threadpool FastAPI) sur un pool psycopg
sync — pas d'event loop psycopg, donc pas de piège Windows. `create_app` prend
un DSN et un index champion injectables : les tests passent la base de test et
un index fixe (aucun appel Data Dragon).

    python -m trio_lab.web          # sert sur $PORT (défaut 8000)
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool

from trio_lab import config, db
from trio_lab.synergy import draft_suggestions
from trio_lab.synergy.compute import DUO_ROLES
from trio_lab.synergy.windows import make_window
from trio_lab.web import champions, queries, summary

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent

ROLE_LABELS = {"jgl": "Jungle", "mid": "Mid", "sup": "Support", "top": "Top", "bot": "ADC"}
# Affichage anglais des tiers de fiabilité — les valeurs internes
# (score_duo.tier/score_trio.tier, classes CSS .tier-*, query params
# min_tier) restent en français (faible/moyen/eleve) : pas de migration de
# données pour un changement purement cosmétique, retour utilisateur
# 2026-08-12 ("il faut juste que l'affichage soit en anglais").
TIER_LABELS = {"faible": "low", "moyen": "medium", "eleve": "high"}
# Libellés lisibles des axes d'archétype (synergy.draft_suggestions.
# ARCHETYPE_STAT_COLUMNS) — retour utilisateur 2026-07-26 : afficher le
# poids de chaque métrique sur les cartes de composition, pas seulement le
# nom de l'archétype.
DRAFT_ARCHETYPE_AXIS_LABELS = {
    "synergy": "Synergy",
    "scaling": "Scaling",
    "cc": "CC",
    "gold": "Gold@15",
    "drakes": "Drakes",
    "soul": "Soul",
    "range": "Range",
    "wr": "Winrate",
    "matchup": "Matchup",
}
# Ordre d'affichage des champs du formulaire "Personnalise tes poids"
# (retour utilisateur 2026-07-28) — mêmes clés que ARCHETYPE_STAT_COLUMNS.
CUSTOM_WEIGHT_AXES = ("synergy", "scaling", "cc", "gold", "drakes", "soul", "range")
# Volontairement limité au trio jgl/mid/sup : gate la route /champion/{role}
# (page individuelle par champion, jamais généralisée à top/bot — Phase 7 ne
# généralise que le duo, cf. docs/ROADMAP.md). Exposé en global Jinja pour que
# duo.html sache quels rôles ont une page champion à lier.
ROLE_TO_TEAM_POSITION = {"jgl": "JUNGLE", "mid": "MIDDLE", "sup": "UTILITY"}
# roles de score_duo/agg_duo (ex. 'top_jgl') → paire de rôles courts (Phase 7).
DUO_ROLE_KEYS: dict[str, tuple[str, str]] = {
    "jgl_mid": ("jgl", "mid"),
    "jgl_sup": ("jgl", "sup"),
    "mid_sup": ("mid", "sup"),
    "top_jgl": ("top", "jgl"),
    "top_mid": ("top", "mid"),
    "top_bot": ("top", "bot"),
    "top_sup": ("top", "sup"),
    "jgl_bot": ("jgl", "bot"),
    "mid_bot": ("mid", "bot"),
    "bot_sup": ("bot", "sup"),
}
# Simulateur de draft (Phase 8) : les 5 rôles courts + mapping vers les noms
# Riot (score_matchup/agg_champion) — volontairement séparé de
# ROLE_TO_TEAM_POSITION (qui gate la page champion, jgl/mid/sup seulement).
DRAFT_ROLES = ("top", "jgl", "mid", "bot", "sup")
DRAFT_ROLE_TO_TEAM_POSITION = {
    "top": "TOP",
    "jgl": "JUNGLE",
    "mid": "MIDDLE",
    "bot": "BOTTOM",
    "sup": "UTILITY",
}
# roles de score_duo (ex. 'top_jgl') retrouvée depuis une paire de rôles
# courts non ordonnée — inverse de DUO_ROLE_KEYS.
_DRAFT_ROLES_BY_PAIR = {frozenset(v): k for k, v in DUO_ROLE_KEYS.items()}
# Compositions suggérées + contres (Phase 9, retour utilisateur 2026-07-25) :
# le moteur de calcul (archétypes, algorithme glouton, contres 1v1) vit
# entièrement dans `synergy.draft_suggestions` — réutilisé tel quel ici en
# calcul à la demande, et par le service collector 24/24 pour la
# matérialisation (`platform="all"`, cf. `collector/service.py`).
# Profil de résilience par champion (Phase 8, /resilience, retour
# utilisateur 2026-07-20) : mêmes 3 facteurs que synergy.resilience.FACTORS,
# choisis pour leur signal réel et leur indépendance mutuelle (corrélations
# de Pearson vérifiées en session).
RESILIENCE_FACTOR_LABELS = {
    "team_gold_diff_15": "Team gold advantage at 15 min",
    "jgl_cs_diff_15": "Team jungle CS at 15 min",
    "first_blood_team": "Team first blood",
}
# Seuil de classement "en avance"/"en retard" par facteur — affiché sur la
# page (retour utilisateur 2026-07-20 : le seuil était invisible). Doit
# rester synchronisé à la main avec `synergy.resilience._NEUTRAL_ZONES`/
# `_is_ahead` (pas de dépendance croisée : ce module web n'importe pas le
# module de calcul batch, même séparation que les libellés au-dessus).
RESILIENCE_FACTOR_THRESHOLDS = {
    "team_gold_diff_15": "ahead: >1000 gold, behind: <-1000 gold",
    "jgl_cs_diff_15": "ahead: at least 1 jungle CS, behind: any deficit",
    "first_blood_team": "ahead: first blood, behind: enemy first blood",
}
# En dessous de ce nombre de games d'un côté (avance OU retard), l'écart de
# WR est trop bruité pour être lu comme un signal — exclu, pas juste grisé
# (retour utilisateur 2026-07-20 : une ligne illisible n'apporte rien, autant
# ne pas l'afficher plutôt que de la garder en gris).
RESILIENCE_MIN_GAMES_PER_SIDE = 30
# Détecteur de picks flex (Phase 8) : rôles Riot → libellé, pour l'affichage
# de /flex (contrairement à ROLE_LABELS, qui indexe sur les codes courts).
RIOT_ROLE_LABELS = {
    "TOP": "Top",
    "JUNGLE": "Jungle",
    "MIDDLE": "Mid",
    "BOTTOM": "ADC",
    "UTILITY": "Support",
}
# Seuils du détecteur : un rôle secondaire compte comme « réellement joué »
# (pas un troll pick isolé) s'il représente au moins FLEX_ROLE_SHARE_THRESHOLD
# des games du champion (historique complet, agg_champion) ET au moins
# FLEX_MIN_ROLE_GAMES games bruts. Le ratio ressources n'est calculé que s'il
# y a au moins FLEX_MIN_PROFILE_GAMES lignes `match_role_stats` pour ce rôle
# (table jeune, déployée le 19/07/2026 — le seuil est bas exprès).
# FLEX_MIN_DEVIATION : sous ce seuil le profil ressources est ~celui du rôle
# (constaté sur prod : la moitié des candidats bruts sont à <3 % d'écart,
# aucun signal réel — sans plancher la liste se noie dans du bruit proche de
# 0, retour utilisateur 2026-07-19). Pas de plafond arbitraire sur le nombre
# de lignes affichées : le plancher de déviation borne déjà la liste aux cas
# qui veulent dire quelque chose (~50 sur la fenêtre courante, pas 20 tronqués
# sur 157 candidats bruts sans que ce soit visible).
FLEX_ROLE_SHARE_THRESHOLD = 0.05
FLEX_MIN_ROLE_GAMES = 100
FLEX_MIN_PROFILE_GAMES = 30
FLEX_MIN_DEVIATION = 0.05
# `DUO_ROLES` (compute.py) donne les 2 rôles d'un duo en noms Riot (JUNGLE/
# MIDDLE/UTILITY) ; ce mapping retrouve la colonne CC par membre (migration
# 020) correspondante pour choisir laquelle des 3 valeurs trio concerne
# champ_a/champ_b (`_duo_detail`, summary.py calcule les 3 sans distinction).
TEAM_POSITION_TO_CC_FIELD = {
    "JUNGLE": "jgl_cc_time_s",
    "MIDDLE": "mid_cc_time_s",
    "UTILITY": "sup_cc_time_s",
}
# Même principe (migration 021) pour le dégâts/gold par membre.
TEAM_POSITION_TO_DMG_PER_GOLD_FIELD = {
    "JUNGLE": "jgl_dmg_per_gold",
    "MIDDLE": "mid_dmg_per_gold",
    "UTILITY": "sup_dmg_per_gold",
}
# Échelle fixe (pas relative au trio affiché) des barres d'avantage gold : un
# écart au-delà sature la barre à 100 %, mais le nombre affiché reste exact.
GOLD_DIFF_BAR_CAP = 2500
# En dessous de ce pourcentage de games atteignant un checkpoint gold, la
# carte est grisée (échantillon trop réduit pour être lu comme un signal).
GOLD_DIFF_LOW_SAMPLE_PCT = 10
DUO_BEST_TRIOS_SHOWN = 10  # meilleurs 3e membres affichés sur la page détail duo
CHAMPION_PARTNERS_SHOWN = 5  # meilleurs partenaires par rôle affichés sur la page champion
CHAMPION_TRIOS_SHOWN = 10  # meilleurs trios affichés sur la page champion

# Vérification du portail développeur Riot (candidature clé production, 15/07/2026).
RIOT_VERIFICATION_CODE = "6f6a29a2-2392-40c8-b1ef-81a20af4858e"

_admin_security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(_admin_security)) -> str:
    """Protège `/admin` (HTTP Basic Auth, identifiants dans ADMIN_USER/ADMIN_PASSWORD).

    Comparaison à temps constant (`secrets.compare_digest`) pour éviter une
    fuite d'information par timing. ADMIN_USER/ADMIN_PASSWORD absents (pas
    configurés) : accès refusé plutôt qu'ouvert par défaut."""
    expected_user = config.ADMIN_USER
    expected_password = config.ADMIN_PASSWORD
    valid = (
        expected_user is not None
        and expected_password is not None
        and secrets.compare_digest(credentials.username, expected_user)
        and secrets.compare_digest(credentials.password, expected_password)
    )
    if not valid:
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"}
        )
    return credentials.username


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{100 * value:.{digits}f} %"


def _fmt_pct100(value: float | None, digits: int = 0) -> str:
    """Comme `pct`, mais pour une valeur déjà sur l'échelle 0-100 (pas 0-1)."""
    return "-" if value is None else f"{value:.{digits}f} %"


def _fmt_signed_pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{100 * value:+.{digits}f} %"


def _fmt_signed_int(value: float | None) -> str:
    return "-" if value is None else f"{value:+,.0f}".replace(",", " ")


def _fmt_num(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _fmt_duration(value: float | None) -> str:
    if value is None:
        return "-"
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes}:{seconds:02d}"


def _bar_pct(value: float | None, siblings: list[float | None]) -> float:
    """Largeur de barre (0-100) : `value` proportionnel au max des `siblings`
    (None ignorés). Utilisé pour comparer des membres entre eux (CC, dégâts/
    gold, wards) — l'échelle est relative au trio/duo affiché, pas absolue."""
    if value is None:
        return 0.0
    reference = max((s for s in siblings if s is not None), default=0.0)
    if reference <= 0:
        return 0.0
    return max(0.0, min(100.0, 100 * value / reference))


def _bar_pct_abs(value: float | None, cap: float) -> float:
    """Largeur de barre (0-100) sur une échelle absolue fixe : |value| ÷ `cap`,
    saturée à 100 au-delà (le nombre affiché à côté reste, lui, non plafonné)."""
    if value is None:
        return 0.0
    return max(0.0, min(100.0, 100 * abs(value) / cap))


def _pct_of(n: int, total: int) -> float:
    """Pourcentage `n` ÷ `total` (0-100), 0 si `total` nul. Communique la taille
    d'échantillon réelle derrière un checkpoint (ex. gold_diff_35 ne porte que
    sur les games qui ont duré ≥35 min — retour utilisateur 2026-07-19)."""
    return 100 * n / total if total else 0.0


def _fmt_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024 or unit == "Go":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} Go"


def _fmt_since(value: datetime | None) -> str:
    """Ancienneté relative d'un horodatage (« 4 min ago »).

    Pas d'apostrophe dans le texte : Jinja l'échapperait en `&#39;` dans le
    HTML rendu, ce qui casse toute comparaison de chaîne littérale côté
    tests — vécu.
    """
    if value is None:
        return "never"
    delta = datetime.now(UTC) - value
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "a few seconds ago"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    return f"{hours // 24} d ago"


def create_app(*, dsn: str | None = None, champion_index=None) -> FastAPI:
    """Construit l'application. `champion_index` : injecté par les tests."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Autocommit (retour utilisateur 2026-08-11) : l'interface est
        # entièrement en lecture (aucune écriture dans ce module ni dans
        # `queries.py`, vérifié) — sans autocommit, une requête interrompue
        # côté client (onglet fermé, navigation abandonnée en plein
        # chargement) peut laisser une connexion du pool "idle in
        # transaction" indéfiniment (constaté en direct en prod : une
        # connexion bloquée 176s a ralenti TOUT le site, y compris les
        # autres requêtes). `psycopg_pool` réinitialise déjà les connexions
        # au retour dans le pool dans le cas normal ; l'autocommit supprime
        # la classe de bug entièrement plutôt que de compter sur ce filet.
        # max_size 4 → 12 (retour utilisateur 2026-08-12, avant partage
        # Discord) : sous charge concurrente, une fois /flex matérialisé
        # (fix principal), le pool à 4 restait le facteur limitant restant
        # (files d'attente mesurées en test de charge). Marge vérifiée sur
        # l'instance Supabase partagée avant d'augmenter : 90 connexions
        # max, 42 utilisées en tout (dont 17 supabase_admin + 5
        # authenticator, hors de notre contrôle), 14 pour trio_lab_app —
        # +8 dans le pire cas reste large sous la limite.
        app.state.pool = ConnectionPool(
            db.require_dsn(dsn), min_size=1, max_size=12, open=True, kwargs={"autocommit": True}
        )
        yield
        app.state.pool.close()

    app = FastAPI(title="Trio Lab", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")

    @app.middleware("http")
    async def _cache_champion_icons(request: Request, call_next):
        """`Cache-Control` long terme UNIQUEMENT sur les icônes de champion
        (retour utilisateur 2026-08-11 : navigation ressentie deux fois plus
        lente après leur passage en local — `StaticFiles` n'envoie aucun
        `Cache-Control` par défaut, chaque page revalide/retélécharge donc
        ~90 icônes au lieu de servir depuis le cache navigateur sans réseau).
        Jamais élargi à tout `/static/` : CSS/JS s'appuient sur le
        cache-busting par mtime (`static_version`) pour éviter un incident
        déjà vécu (CSS périmé après déploiement, retour utilisateur
        2026-07-13) — un `Cache-Control` agressif sur des URLs SANS
        versioning serait risqué là, contrairement aux icônes (retour
        utilisateur : "elles changent très rarement", nom de fichier stable
        tant que Data Dragon ne renomme pas l'image)."""
        response = await call_next(request)
        if request.url.path.startswith("/static/champions/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/riot.txt", response_class=PlainTextResponse)
    def riot_verification() -> str:
        """Vérification de l'URL produit pour la candidature clé de production
        (portail développeur Riot, 15/07/2026)."""
        return RIOT_VERIFICATION_CODE

    def static_version(filename: str) -> int:
        """Cache-busting (`?v=mtime`) pour les fichiers statiques : `StaticFiles`
        n'envoie pas de `Cache-Control`, donc le navigateur peut garder un CSS/JS
        périmé après un déploiement sans revalider (vécu — CSS servi correctement
        par le serveur mais mise en page cassée côté navigateur, retour
        utilisateur 2026-07-13)."""
        return int((_HERE / "static" / filename).stat().st_mtime)

    templates.env.globals["static_version"] = static_version
    templates.env.globals["gold_diff_bar_cap"] = GOLD_DIFF_BAR_CAP
    templates.env.globals["gold_diff_low_sample_pct"] = GOLD_DIFF_LOW_SAMPLE_PCT
    templates.env.globals["resilience_min_games_per_side"] = RESILIENCE_MIN_GAMES_PER_SIDE
    templates.env.filters.update(
        pct=_fmt_pct,
        pct100=_fmt_pct100,
        signed_pct=_fmt_signed_pct,
        signed_int=_fmt_signed_int,
        num=_fmt_num,
        duration=_fmt_duration,
        since=_fmt_since,
        bytes=_fmt_bytes,
        barpct=_bar_pct,
        barpct_abs=_bar_pct_abs,
        pctof=_pct_of,
    )
    state = {"champions": champion_index}
    _context_cache: dict[str, tuple[float, object]] = {}
    # TTL de `resolve_context` (retour utilisateur 2026-08-21, navigation
    # lente) : `available_windows`/`available_platforms`/`window_freshness`
    # tournaient sur CHAQUE page (appelées depuis `resolve_context`) — mesuré
    # en session : 724ms + 480ms + 594ms = ~1,8s, la majorité du temps de
    # chargement d'une page qui n'a par ailleurs presque rien à faire (ex.
    # /resilience, /flex). Aucune des 3 n'a besoin d'être exacte à la
    # seconde : le rollover de fenêtre prend des heures (le pipeline de
    # scores tourne au plus 1x/12h, cf. `SCORE_REFRESH_THROTTLE_S`), un
    # compteur de matchs vieux de quelques dizaines de secondes est
    # invisible pour un visiteur. 60s de cache mémoire par processus (même
    # esprit que `champ_index` ci-dessus) élimine ce coût pour l'écrasante
    # majorité des requêtes, sans jamais montrer une fenêtre/plateforme
    # perimée de plus d'1 minute.
    _CONTEXT_CACHE_TTL_S = 60

    def _cached(key: str, compute):
        now = time.monotonic()
        cached = _context_cache.get(key)
        if cached is not None and now - cached[0] < _CONTEXT_CACHE_TTL_S:
            return cached[1]
        value = compute()
        _context_cache[key] = (now, value)
        return value

    def champ_index() -> dict[int, champions.Champion]:
        # Fetch paresseux et mémorisé : l'app démarre même si Data Dragon est
        # injoignable (les ids restent affichables), et retentera au prochain hit.
        if state["champions"] is None:
            try:
                state["champions"] = champions.fetch_index()
            except OSError:
                logger.warning("Data Dragon injoignable, index champion vide pour cette requête")
                return {}
        return state["champions"]

    def champ(champ_id: int) -> champions.Champion:
        found = champ_index().get(champ_id)
        return found or champions.Champion(id=champ_id, name=f"#{champ_id}", icon_url="")

    templates.env.globals["champ"] = champ
    templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
    templates.env.globals["TIER_LABELS"] = TIER_LABELS
    templates.env.globals["ROLE_TO_TEAM_POSITION"] = ROLE_TO_TEAM_POSITION
    templates.env.globals["DUO_ROLE_KEYS"] = DUO_ROLE_KEYS
    templates.env.globals["RIOT_ROLE_LABELS"] = RIOT_ROLE_LABELS
    templates.env.globals["RESILIENCE_FACTOR_LABELS"] = RESILIENCE_FACTOR_LABELS
    templates.env.globals["RESILIENCE_FACTOR_THRESHOLDS"] = RESILIENCE_FACTOR_THRESHOLDS
    templates.env.globals["draft_counter_primary_picks"] = draft_suggestions.COUNTER_PRIMARY_PICKS
    templates.env.globals["draft_counter_secondary_roles"] = (
        draft_suggestions.COUNTER_SECONDARY_ROLES
    )

    def resolve_champion(name_or_id: str | None) -> int | None:
        """Filtre champion de la tier list : nom (recherche) ou id. None si vide."""
        if not name_or_id or not name_or_id.strip():
            return None
        text = name_or_id.strip()
        if text.isdigit():
            return int(text)
        found = champions.name_lookup(champ_index()).get(text.casefold())
        if found is None:
            raise HTTPException(404, f"unknown champion: {text}")
        return found

    _MAX_SORT_LEVELS = 4

    def parse_sort(
        sort_param: str, dir_param: str, valid: dict[str, str]
    ) -> tuple[list[str], list[str]]:
        """Tri façon tableur : `sort`/`dir` sont des listes séparées par des
        virgules (ex. `sort=cc,wr&dir=desc,desc`), appliquées dans l'ordre —
        clic simple sur une colonne (1 seul élément) ou Maj-clic pour ajouter
        un niveau (JS, cf. static/sort.js). Chaque élément est validé contre
        une whitelist avant d'atteindre le SQL (jamais interpolé brut)."""
        sorts = [s for s in sort_param.split(",") if s]
        dirs = [d for d in dir_param.split(",") if d]
        if not sorts or len(sorts) != len(dirs) or len(sorts) > _MAX_SORT_LEVELS:
            raise HTTPException(404, f"invalid sort: sort={sort_param!r} dir={dir_param!r}")
        for s in sorts:
            if s not in valid:
                raise HTTPException(404, f"unknown sort: {s!r}")
        for d in dirs:
            if d not in queries.SORT_DIRECTIONS:
                raise HTTPException(404, f"unknown sort direction: {d!r}")
        return sorts, dirs

    def _parse_optional_float(
        value: str | None, *, ge: float | None = None, le: float | None = None
    ) -> float | None:
        """Champ numérique optionnel de formulaire : `""` (input vidé) traité
        comme absent plutôt qu'une erreur 422 — même piège que `role` (cf. plus
        haut), mais `Query(..., ge=..., le=...)` ne peut pas l'absorber pour un
        type numérique (contrairement à `str`, qui accepte `""` nativement)."""
        if value is None or not value.strip():
            return None
        try:
            parsed = float(value)
        except ValueError:
            raise HTTPException(404, f"invalid numeric value: {value!r}") from None
        if ge is not None and parsed < ge:
            raise HTTPException(404, f"value too low: {value!r}")
        if le is not None and parsed > le:
            raise HTTPException(404, f"value too high: {value!r}")
        return parsed

    # Filtres par seuil "au moins X", une entrée par colonne triable sauf
    # `games` (son propre champ dédié `min_games` existait déjà) — whitelist
    # identique à TRIO_SORTS/DUO_SORTS (mêmes colonnes des deux côtés,
    # cf. queries.py). Certaines colonnes sont stockées en fraction 0-1 mais
    # affichées en % (is_percent) : saisies en % dans le formulaire, converties
    # ici comme `wr` déjà l'était avant que le filtre ne s'étende à toutes les
    # colonnes (retour utilisateur, 2026-07-13).
    _THRESHOLD_SPECS: dict[str, tuple[bool, float | None, float | None]] = {
        "wr": (True, 0, 100),
        "synergy": (True, -100, 100),
        "gold5": (False, None, None),
        "gold10": (False, None, None),
        "gold15": (False, None, None),
        "teamgold15": (False, None, None),
        "vision": (False, 0, None),
        "drakes": (False, 0, None),
        "soul": (True, 0, 100),
        "herald": (True, 0, 100),
        "tower1": (True, 0, 100),
        "cc": (False, 0, None),
        "scaling": (True, -100, 100),
        "range": (False, 0, 100),
    }

    def threshold_raw(request: Request, *, prefix: str) -> dict[str, str]:
        """Valeurs brutes des filtres de seuil pour `prefix` ("min" ou "max",
        retour utilisateur 2026-07-20 : filtrer par plage, pas juste un
        plancher) — chaînes telles que soumises, `""` si absent, repeuplent
        les champs du formulaire après filtrage."""
        return {key: request.query_params.get(f"{prefix}_{key}", "") for key in _THRESHOLD_SPECS}

    def parse_thresholds(raw: dict[str, str]) -> dict[str, float]:
        """Filtres « au moins X » / « au plus X » combinables, sur toutes les
        colonnes triables — trouver les combos bons sur plusieurs axes à la
        fois, ce que le tri seul ne permet pas quand la 1re colonne triée est
        presque toujours unique (retour utilisateur, 2026-07-13)."""
        values: dict[str, float] = {}
        for key, (is_percent, ge, le) in _THRESHOLD_SPECS.items():
            parsed = _parse_optional_float(raw.get(key), ge=ge, le=le)
            if parsed is not None:
                values[key] = parsed / 100.0 if is_percent else parsed
        return values

    def filters_qs(min_raw: dict[str, str], max_raw: dict[str, str], **base: object) -> str:
        """Querystring des filtres actifs (calculée côté Python, pas en Jinja
        `{% set %}` dans une boucle : la variable ne survivrait pas à la
        boucle) — réutilisée par la pagination et les liens de tri pour ne
        jamais perdre un filtre en changeant de page ou de tri."""
        params = dict(base)
        params.update({f"min_{k}": v for k, v in min_raw.items()})
        params.update({f"max_{k}": v for k, v in max_raw.items()})
        return urlencode(params)

    def resolve_context(conn, window: str | None, platform: str | None) -> tuple[str, str, dict]:
        """(fenêtre, plateforme) validées + le contexte commun des templates."""
        known = _cached("windows", lambda: queries.available_windows(conn))
        if not known:
            raise HTTPException(503, "no materialized scores (run python -m trio_lab.synergy)")
        if window is None:
            window = known[0]
        elif window not in known:
            raise HTTPException(404, f"window not materialized: {window}")
        platforms = _cached(
            f"platforms:{window}", lambda: queries.available_platforms(conn, window)
        )
        if platform is None:
            platform = platforms[0]
        elif platform not in platforms:
            raise HTTPException(404, f"platform not present in window: {platform}")
        freshness = _cached(f"freshness:{window}", lambda: queries.window_freshness(conn, window))
        context = {
            "window": window,
            "platform": platform,
            "windows": known,
            "platforms": platforms,
            "window_matches": freshness["matches"],
            "last_collected_at": freshness["last_collected_at"],
        }
        return window, platform, context

    # --- Pages HTML ---

    # Un <select> vide envoie `role=` : accepter la chaîne vide (sinon 422 que
    # hx-boost avale silencieusement — bouton « Filtrer » qui ne fait rien).
    # sort/dir : listes séparées par des virgules (tri multi-colonnes façon
    # tableur), validées à la main par `parse_sort` — pas de pattern Query
    # unique, la forme n'est plus une simple valeur whitelistée.
    # "all" (retour utilisateur 2026-07-20) : mélange les 10 paires, pour
    # filtrer par seuil sans devoir choisir un couple de rôles.
    _DUO_ROLES_PATTERN = f"^({'|'.join(queries.DUO_ROLES)}|all)$"

    @app.get("/", response_class=HTMLResponse)
    def tierlist_page(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        jgl: str | None = None,
        mid: str | None = None,
        sup: str | None = None,
        min_games: int = Query(0, ge=0),
        min_tier: str = Query("faible", pattern="^(faible|moyen|eleve)$"),
        sort: str = "synergy",
        direction: str = Query("desc", alias="dir"),
        page: int = Query(1, ge=1),
    ):
        sorts, dirs = parse_sort(sort, direction, queries.TRIO_SORTS)
        min_raw = threshold_raw(request, prefix="min")
        max_raw = threshold_raw(request, prefix="max")
        min_thresholds = parse_thresholds(min_raw)
        max_thresholds = parse_thresholds(max_raw)
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            result = queries.trio_tierlist(
                conn,
                window,
                platform,
                jgl_champion_id=resolve_champion(jgl),
                mid_champion_id=resolve_champion(mid),
                sup_champion_id=resolve_champion(sup),
                min_games=min_games,
                min_tier=min_tier,
                min_values=min_thresholds,
                max_values=max_thresholds,
                sort=sorts,
                direction=dirs,
                page=page,
            )
        return templates.TemplateResponse(
            request,
            "tierlist.html",
            {
                **context,
                **result,
                "jgl_search": jgl or "",
                "mid_search": mid or "",
                "sup_search": sup or "",
                "min_games": min_games,
                "min_tier": min_tier,
                "min_values": min_raw,
                "max_values": max_raw,
                "filters_qs": filters_qs(
                    min_raw,
                    max_raw,
                    window=window,
                    platform=platform,
                    jgl=jgl or "",
                    mid=mid or "",
                    sup=sup or "",
                    min_games=min_games,
                    min_tier=min_tier,
                ),
                "sort": sort,
                "direction": direction,
                "sorts": sorts,
                "directions": dirs,
                "champion_names": sorted(c.name for c in champ_index().values()),
            },
        )

    @app.get("/duos", response_class=HTMLResponse)
    def duos_page(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        roles: str = Query("jgl_mid", pattern=_DUO_ROLES_PATTERN),
        champ_a: str | None = None,
        champ_b: str | None = None,
        min_games: int = Query(0, ge=0),
        min_tier: str = Query("faible", pattern="^(faible|moyen|eleve)$"),
        sort: str = "synergy",
        direction: str = Query("desc", alias="dir"),
        page: int = Query(1, ge=1),
    ):
        sorts, dirs = parse_sort(sort, direction, queries.DUO_SORTS)
        min_raw = threshold_raw(request, prefix="min")
        max_raw = threshold_raw(request, prefix="max")
        min_thresholds = parse_thresholds(min_raw)
        max_thresholds = parse_thresholds(max_raw)
        all_roles = roles == "all"
        # champ_a/champ_b sont des recherches PAR RÔLE ("champion en jungle" vs
        # "champion en mid") : sans rôle fixé, ni l'un ni l'autre n'a de sens
        # (quel slot serait "champ_a" pour un mix top_bot/jgl_sup/...?) —
        # ignorés côté serveur si présents dans l'URL plutôt que de filtrer
        # sur une colonne qui ne veut plus rien dire.
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            result = queries.duo_tierlist(
                conn,
                window,
                platform,
                None if all_roles else roles,
                champ_a_id=None if all_roles else resolve_champion(champ_a),
                champ_b_id=None if all_roles else resolve_champion(champ_b),
                min_games=min_games,
                min_tier=min_tier,
                min_values=min_thresholds,
                max_values=max_thresholds,
                sort=sorts,
                direction=dirs,
                page=page,
            )
        return templates.TemplateResponse(
            request,
            "duos.html",
            {
                **context,
                **result,
                "roles": roles,
                "champ_a_search": "" if all_roles else champ_a or "",
                "champ_b_search": "" if all_roles else champ_b or "",
                "min_games": min_games,
                "min_tier": min_tier,
                "min_values": min_raw,
                "max_values": max_raw,
                "filters_qs": filters_qs(
                    min_raw,
                    max_raw,
                    window=window,
                    platform=platform,
                    roles=roles,
                    champ_a="" if all_roles else champ_a or "",
                    champ_b="" if all_roles else champ_b or "",
                    min_games=min_games,
                    min_tier=min_tier,
                ),
                "sort": sort,
                "direction": direction,
                "sorts": sorts,
                "directions": dirs,
                "champion_names": sorted(c.name for c in champ_index().values()),
            },
        )

    def _trio_detail(conn, window: str, platform: str, jgl: int, mid: int, sup: int) -> dict:
        score = queries.trio_score(conn, window, platform, jgl, mid, sup)
        if score is None:
            raise HTTPException(404, "trio not scored for this window/platform")
        patch_window = make_window(window.split("+"))
        weights = patch_window.weights_for((jgl, mid, sup))
        rows = queries.trio_match_rows(
            conn,
            list(patch_window.patches),
            None if platform == "all" else platform,  # 'all' = toutes régions
            jgl,
            mid,
            sup,
        )
        stats = summary.summarize(rows, weights)
        patches = list(patch_window.patches)
        member_wr = {
            "jgl": queries.member_wr(conn, patches, platform, "JUNGLE", jgl, weights),
            "mid": queries.member_wr(conn, patches, platform, "MIDDLE", mid, weights),
            "sup": queries.member_wr(conn, patches, platform, "UTILITY", sup, weights),
        }
        return {
            "score": score,
            "stats": stats,
            "member_wr": member_wr,
            "duos": queries.trio_duos(conn, window, platform, jgl, mid, sup),
        }

    @app.get("/trio/{jgl}/{mid}/{sup}", response_class=HTMLResponse)
    def trio_page(
        request: Request,
        jgl: int,
        mid: int,
        sup: int,
        window: str | None = None,
        platform: str | None = None,
    ):
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            detail = _trio_detail(conn, window, platform, jgl, mid, sup)
        return templates.TemplateResponse(request, "trio.html", {**context, **detail})

    def _duo_detail(
        conn, window: str, platform: str, roles: str, champ_a: int, champ_b: int
    ) -> dict:
        score = queries.duo_score(conn, window, platform, roles, champ_a, champ_b)
        if score is None:
            raise HTTPException(404, "duo not scored for this window/platform")
        patch_window = make_window(window.split("+"))
        weights = patch_window.weights_for((champ_a, champ_b))
        role_a, role_b = DUO_ROLES[roles]
        patches = list(patch_window.patches)
        plat = None if platform == "all" else platform  # 'all' = toutes régions
        if roles in queries.TRIO_DUO_ROLES:
            # 3 paires internes au trio jgl/mid/sup : match_trio_stats, notion
            # de « 3e membre libre » (best_trios).
            rows = queries.duo_match_rows(conn, patches, plat, roles, champ_a, champ_b)
            stats = summary.summarize(rows, weights)
            # Ventilation CC par membre (migration 020) : summary.summarize
            # calcule les 3 rôles trio sans distinction, on ne garde que les 2.
            stats["champ_a_cc_time_s"] = stats[TEAM_POSITION_TO_CC_FIELD[role_a]]
            stats["champ_b_cc_time_s"] = stats[TEAM_POSITION_TO_CC_FIELD[role_b]]
            # Idem dégâts/gold par membre (migration 021).
            stats["champ_a_dmg_per_gold"] = stats[TEAM_POSITION_TO_DMG_PER_GOLD_FIELD[role_a]]
            stats["champ_b_dmg_per_gold"] = stats[TEAM_POSITION_TO_DMG_PER_GOLD_FIELD[role_b]]
            best_trios = queries.duo_best_trios(
                conn, window, platform, roles, champ_a, champ_b, DUO_BEST_TRIOS_SHOWN
            )
        else:
            # Paire hors trio (Phase 7) : match_role_stats, déjà champ_a/b_*
            # génériques (cf. `duo_role_match_rows`) — pas de notion de « 3e
            # membre », le trio de ce projet reste uniquement jgl/mid/sup.
            rows = queries.duo_role_match_rows(
                conn, patches, plat, role_a, role_b, champ_a, champ_b
            )
            stats = summary.summarize(rows, weights)
            best_trios = []
        member_wr = {
            "a": queries.member_wr(conn, patches, platform, role_a, champ_a, weights),
            "b": queries.member_wr(conn, patches, platform, role_b, champ_b, weights),
        }
        return {
            "score": score,
            "stats": stats,
            "member_wr": member_wr,
            "best_trios": best_trios,
        }

    @app.get("/duo/{roles}/{champ_a}/{champ_b}", response_class=HTMLResponse)
    def duo_page(
        request: Request,
        roles: str,
        champ_a: int,
        champ_b: int,
        window: str | None = None,
        platform: str | None = None,
    ):
        if roles not in queries.DUO_ROLES:
            raise HTTPException(404, f"unknown roles: {roles!r}")
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            detail = _duo_detail(conn, window, platform, roles, champ_a, champ_b)
        return templates.TemplateResponse(
            request, "duo.html", {**context, "roles": roles, **detail}
        )

    def _champion_detail(conn, window: str, platform: str, role: str, champion_id: int) -> dict:
        patch_window = make_window(window.split("+"))
        weights = patch_window.weights_for((champion_id,))
        patches = list(patch_window.patches)
        baseline = queries.champion_baseline(
            conn, patches, platform, ROLE_TO_TEAM_POSITION[role], champion_id, weights
        )
        if baseline is None:
            raise HTTPException(404, "champion not scored in this role for this window")
        partners = {
            partner_role: queries.champion_best_partners(
                conn, window, platform, roles, role, champion_id, CHAMPION_PARTNERS_SHOWN
            )
            for roles, partner_role in queries.CHAMPION_PARTNER_GROUPS[role]
        }
        best_trios = queries.trio_tierlist(
            conn,
            window,
            platform,
            **{f"{role}_champion_id": champion_id},
            min_tier="moyen",  # écarte les trios à 1-2 games (retour utilisateur, 2026-07-12)
            sort=["synergy"],
            direction=["desc"],
        )["rows"][:CHAMPION_TRIOS_SHOWN]
        match_rows = queries.champion_match_rows(
            conn,
            patches,
            None if platform == "all" else platform,  # 'all' = toutes régions
            role,
            champion_id,
        )
        stats = summary.summarize(match_rows, weights)
        return {
            "role": role,
            "champion_id": champion_id,
            "baseline": baseline,
            "stats": stats,
            "partners": partners,
            "best_trios": best_trios,
        }

    @app.get("/champion/{role}/{champion_id}", response_class=HTMLResponse)
    def champion_page(
        request: Request,
        role: str,
        champion_id: int,
        window: str | None = None,
        platform: str | None = None,
    ):
        if role not in ROLE_TO_TEAM_POSITION:
            raise HTTPException(404, f"unknown role: {role!r}")
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            detail = _champion_detail(conn, window, platform, role, champion_id)
        return templates.TemplateResponse(request, "champion.html", {**context, **detail})

    # --- Compositions et contres (Phase 8, révisé Phase 9 : plus de
    # pick-par-pick, retour utilisateur 2026-07-25 — "il faut qu'on enlève la
    # partie draft avec la sélection des champions") ---
    #
    # État entièrement dans l'URL (query params), pas de session serveur —
    # même principe que window/platform ailleurs.

    def _resolve_seed_pairs(raw_pairs: list[dict]) -> list[dict]:
        """Résout les champion_id de `raw_pairs` (`synergy.draft_suggestions`,
        seed_pairs) en Champion/labels pour le template."""
        return [
            {
                "role_label_a": ROLE_LABELS[p["role_a"]],
                "role_label_b": ROLE_LABELS[p["role_b"]],
                "champion_a": champ(p["champ_a"]),
                "champion_b": champ(p["champ_b"]),
                "synergy": p["synergy"],
                "games": p["games"],
                "tier": p["tier"],
            }
            for p in raw_pairs
        ]

    def _resolve_counters(raw_counters: dict | None) -> dict | None:
        """Résout les champion_id de `raw_counters`
        (`synergy.draft_suggestions.draft_counters`, brut ou lu depuis
        `draft_suggestion_counter`) en Champion/labels pour le template."""
        if raw_counters is None:
            return None
        primary = raw_counters["primary"]
        return {
            "primary": {
                "role": primary["role"],
                "role_label": ROLE_LABELS[primary["role"]],
                "against": champ(primary["against_champion"]),
                "picks": [
                    {"champion": champ(p["champion_id"]), "delta": p["delta"]}
                    for p in primary["picks"]
                ],
            },
            "secondary": [
                {
                    "role": s["role"],
                    "role_label": ROLE_LABELS[s["role"]],
                    "against": champ(s["against_champion"]),
                    "pick": {"champion": champ(s["champion_id"]), "delta": s["delta"]},
                }
                for s in raw_counters["secondary"]
            ],
        }

    def _archetype_weights_display(weights: dict[str, float]) -> list[dict]:
        """Poids de chaque axe, prêts à afficher (retour utilisateur
        2026-07-26) — axes à poids nul omis (ex. scaling=0 pour "Avantage
        early / lane"). Prend directement le dict de poids (pas une clé
        d'archétype) : fonctionne aussi bien pour les archétypes fixes que
        pour des poids personnalisés (retour utilisateur 2026-07-28)."""
        return [
            {"label": DRAFT_ARCHETYPE_AXIS_LABELS[axis], "value": value}
            for axis, value in weights.items()
            if value
        ]

    def _build_draft_result(raw: dict, *, include_seed_pairs: bool = True) -> dict:
        """Adapte un résultat BRUT (`synergy.draft_suggestions.propose_drafts`,
        un draft "compose à la main" tout juste calculé, ou une ligne lue
        depuis `draft_suggestion(_counter)`) en structure prête pour le
        template : résout les champion_id en Champion (nom/icône), génère
        les phrases de conseil depuis les stats moyennes. Un seul chemin de
        rendu pour les 2 sources (précalculé ou en direct) et les 2 sections
        (compositions suggérées / composées à la main).

        `include_seed_pairs` (retour utilisateur 2026-07-27, "est-ce que
        cette donnée a un intérêt ?") : sur "Compositions suggérées", le duo
        de départ est un détail interne de l'algorithme (peut changer via
        `refine_draft`), sa synergie isolée à côté du total du draft
        n'apporte rien et prête à confusion — masqué. Sur "Compose à partir
        de tes champions", ce sont les champions CHOISIS par l'utilisateur :
        savoir s'ils synergisent déjà entre eux (et sur combien de games)
        répond à une vraie question — conservé."""
        members = [
            {"role": role, "role_label": ROLE_LABELS[role], "champion": champ(raw["members"][role])}
            for role in DRAFT_ROLES
        ]
        stats = raw["advice_stats"]
        advice = (
            draft_suggestions.draft_advice(
                stats["scaling"], stats["cc_time_s"], stats["gold_diff_15"]
            )
            if stats
            else []
        )
        # Winrate + IC : moyenne simple sur les 10 vraies paires (comme les
        # autres stats affichées ici), pas une combinaison statistique
        # rigoureuse des intervalles — retour utilisateur 2026-07-27.
        wr = (
            {"value": stats["wr"], "ci_low": stats["ci_low"], "ci_high": stats["ci_high"]}
            if stats
            else None
        )
        weights = raw.get("weights") or draft_suggestions.ARCHETYPES[raw["archetype"]]["weights"]
        return {
            "archetype": raw["archetype"],
            "suggestion_rank": raw["suggestion_rank"],
            "selection": raw["selection"],
            "label": raw["label"],
            "weights": _archetype_weights_display(weights),
            "members": members,
            "total_synergy": raw["total_synergy"],
            "wr": wr,
            "seed_pairs": _resolve_seed_pairs(raw["seed_pairs"]) if include_seed_pairs else [],
            "advice": advice,
            "counters": _resolve_counters(raw["counters"]),
            "strengths": _resolve_counters(raw["strengths"]),
        }

    def _group_draft_variants(rendered: list[dict]) -> list[dict]:
        """Regroupe une liste PLATE de variantes rendues (`_build_draft_result`,
        1 entrée par `(archetype, suggestion_rank)`) par archétype, dans
        l'ordre de première apparition — jusqu'à 3 boutons 1/2/3 par groupe
        sur "Compositions suggérées" (retour utilisateur 2026-07-27),
        `draft.html` n'affichant que le contenu de la variante active."""
        groups: dict[str, dict] = {}
        order: list[str] = []
        for r in rendered:
            key = r["archetype"]
            if key not in groups:
                groups[key] = {"label": r["label"], "variants": []}
                order.append(key)
            groups[key]["variants"].append(r)
        return [groups[k] for k in order]

    def _manual_propose(
        conn,
        window: str,
        platform: str,
        seed_picks: dict[str, int],
        archetype_key: str,
        label: str,
        weights: dict[str, float],
        zstats: dict[str, tuple[float, float]],
        min_games: int,
    ) -> dict | None:
        """Construit le résultat BRUT (même forme que `propose_drafts`) pour
        "Compose à partir de tes champions" — 1 seul résultat, pas de
        recherche parmi plusieurs seeds (l'utilisateur a déjà fixé son point
        de départ, `seed_picks`, 1 à 5 rôles). `None` si la complétion des
        rôles restants échoue (pas assez de données fiables). Prend `label`/
        `weights` directement (retour utilisateur 2026-07-28, poids
        personnalisés) plutôt que de chercher `archetype_key` dans
        `ARCHETYPES` — fonctionne aussi bien pour les archétypes fixes que
        pour l'option "Personnalisé" du formulaire. `min_games` (retour
        utilisateur 2026-07-28, "choisir le niveau de fiabilité... en
        choisissant un nombre de games") : seuil choisi par l'utilisateur via
        le champ `cw_min_games`, plus `MIN_GAMES_DEFAULT` fixe.

        Un passage de remplacement (`refine_draft`, retour utilisateur
        2026-07-27) s'applique ensuite — mais AVEC `seed_picks` verrouillés :
        l'utilisateur a choisi ces champions exprès, le raffinement ne peut
        toucher que les rôles que LE SYSTÈME a complétés lui-même."""
        placed, total, seed_pairs = draft_suggestions.seed_from_champions(
            conn, window, platform, seed_picks
        )
        completed = draft_suggestions.greedy_complete_draft(
            conn, window, platform, placed, total, min_games, weights, zstats
        )
        if completed is None:
            return None
        full_placed, full_total = completed
        full_placed, full_total = draft_suggestions.refine_draft(
            conn,
            window,
            platform,
            full_placed,
            full_total,
            min_games,
            weights,
            zstats,
            locked_roles=frozenset(seed_picks),
        )
        return {
            "archetype": archetype_key,
            "suggestion_rank": 0,
            "selection": "score",
            "weights": weights,
            "label": label,
            "members": full_placed,
            "total_synergy": full_total,
            "seed_pairs": seed_pairs,
            "advice_stats": draft_suggestions.full_draft_stat_averages(
                conn, window, platform, full_placed, draft_suggestions.DISPLAY_STAT_COLUMNS
            ),
            "counters": draft_suggestions.draft_counters(conn, window, platform, full_placed),
            "strengths": draft_suggestions.draft_strengths(conn, window, platform, full_placed),
        }

    def _parse_custom_weights(
        raw_weights: dict[str, str | None],
    ) -> tuple[dict[str, float] | None, str | None]:
        """Lit les champs `w_<axe>` (un par `CUSTOM_WEIGHT_AXES`) du formulaire
        "Personnalise tes poids" (retour utilisateur 2026-07-28 : "que
        l'utilisateur décide lui-même des poids") — `(None, None)` si le
        formulaire n'a jamais été soumis
        (tous les champs vides, cas de la page fraîche), `(None, message)`
        si soumis mais invalide (jamais une correction silencieuse — poids
        négatif ou somme ≠ 100, tolérance ±0.5 pour l'arrondi d'un champ
        décimal), `(weights, None)` sinon — fractions de 1 (mêmes clés que
        `ARCHETYPE_STAT_COLUMNS`), prêtes pour `propose_for_weights`."""
        if not any(v for v in raw_weights.values()):
            return None, None
        try:
            values = {
                axis: float(raw_weights[axis]) if raw_weights[axis] else 0.0
                for axis in CUSTOM_WEIGHT_AXES
            }
        except ValueError:
            return None, "Invalid weights: use numbers."
        if any(v < 0 for v in values.values()):
            return None, "Weights cannot be negative."
        total = sum(values.values())
        if abs(total - 100.0) > 0.5:
            return None, f"Weights must add up to 100% (currently {total:.0f}%)."
        return {axis: v / 100.0 for axis, v in values.items()}, None

    @app.get("/draft", response_class=HTMLResponse)
    def draft_page(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        suggest: bool = False,
        seed_top: str | None = None,
        seed_jgl: str | None = None,
        seed_mid: str | None = None,
        seed_bot: str | None = None,
        seed_sup: str | None = None,
        archetype: str | None = None,
        w_synergy: str | None = None,
        w_scaling: str | None = None,
        w_cc: str | None = None,
        w_gold: str | None = None,
        w_drakes: str | None = None,
        w_soul: str | None = None,
        w_range: str | None = None,
        cw_synergy: str | None = None,
        cw_scaling: str | None = None,
        cw_cc: str | None = None,
        cw_gold: str | None = None,
        cw_drakes: str | None = None,
        cw_soul: str | None = None,
        cw_range: str | None = None,
        w_min_games: int = Query(draft_suggestions.MIN_GAMES_DEFAULT, ge=0),
        cw_min_games: int = Query(draft_suggestions.MIN_GAMES_DEFAULT, ge=0),
        enemy_top: str | None = None,
        enemy_jgl: str | None = None,
        enemy_mid: str | None = None,
        enemy_bot: str | None = None,
        enemy_sup: str | None = None,
        enemy_min_games: int = Query(draft_suggestions.MIN_GAMES_DEFAULT, ge=0),
    ):
        raw_seeds = {
            "top": seed_top,
            "jgl": seed_jgl,
            "mid": seed_mid,
            "bot": seed_bot,
            "sup": seed_sup,
        }
        # État courant du formulaire "Compose à partir de tes champions",
        # pour le reconstruire tel quel après un changement de fenêtre/région
        # (même principe que filters_qs ailleurs sur le site).
        current_seed_params = {f"seed_{role}": v or "" for role, v in raw_seeds.items()}

        # "Contre cette équipe" (retour utilisateur 2026-08-11) : picks
        # adverses par rôle, scouting partiel autorisé (0 à 5) — formulaire
        # séparé de "Compose à partir de tes champions" (rôles ADVERSES, pas
        # les tiens), même principe d'état courant que les autres.
        raw_enemy = {
            "top": enemy_top,
            "jgl": enemy_jgl,
            "mid": enemy_mid,
            "bot": enemy_bot,
            "sup": enemy_sup,
        }
        current_enemy_params = {f"enemy_{role}": v or "" for role, v in raw_enemy.items()}
        current_enemy_params["enemy_min_games"] = str(enemy_min_games)

        # "Personnalise tes poids" (retour utilisateur 2026-07-28) : 5e
        # archétype dans "Compositions suggérées", TOUJOURS en direct (même
        # sur la région par défaut où les 4 autres sont précalculées — un
        # poids personnalisé ne peut jamais être matérialisé à l'avance par
        # le collector, qui ne connaît pas les poids d'un futur visiteur).
        raw_weights = {
            "synergy": w_synergy,
            "scaling": w_scaling,
            "cc": w_cc,
            "gold": w_gold,
            "drakes": w_drakes,
            "soul": w_soul,
            "range": w_range,
        }
        current_weight_params = {f"w_{axis}": v or "" for axis, v in raw_weights.items()}
        current_weight_params["w_min_games"] = str(w_min_games)
        custom_weights, custom_error = _parse_custom_weights(raw_weights)

        # Poids personnalisés pour "Compose à partir de tes champions" —
        # champs `cw_<axe>` SÉPARÉS de `w_<axe>` (retour utilisateur
        # 2026-07-28 : "pourquoi il devrait partager les mêmes poids
        # personnalisés ?") : rien ne justifie que le 5e archétype
        # auto-suggéré et une composition bâtie à partir de TES champions
        # utilisent forcément le même réglage — 2 formulaires, 2 états
        # indépendants.
        raw_manual_weights = {
            "synergy": cw_synergy,
            "scaling": cw_scaling,
            "cc": cw_cc,
            "gold": cw_gold,
            "drakes": cw_drakes,
            "soul": cw_soul,
            "range": cw_range,
        }
        current_manual_weight_params = {
            f"cw_{axis}": v or "" for axis, v in raw_manual_weights.items()
        }
        current_manual_weight_params["cw_min_games"] = str(cw_min_games)
        manual_custom_weights, manual_custom_error = _parse_custom_weights(raw_manual_weights)

        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            seed_picks = {role: resolve_champion(v) for role, v in raw_seeds.items() if v}
            enemy_picks = {role: resolve_champion(v) for role, v in raw_enemy.items() if v}

            # `pool`/`zstats` (le tri par archétype des duos fiables) coûte
            # une requête large (~10 000 lignes) : calculé au plus une fois
            # PAR SEUIL `min_games` utilisé dans la requête (retour
            # utilisateur 2026-07-28, "Compose à partir de tes champions" et
            # "Personnalise tes poids" ont chacun leur propre seuil) —
            # partagé entre "Compositions suggérées" (calcul en direct, pas
            # la région par défaut, `MIN_GAMES_DEFAULT`) et les 2 autres
            # formulaires SI ILS UTILISENT LE MÊME SEUIL (cas courant, aucun
            # n'y a touché).
            _pool_zstats_cache: dict[int, tuple[list[dict], dict]] = {}

            def _get_pool_zstats(
                min_games: int = draft_suggestions.MIN_GAMES_DEFAULT,
            ) -> tuple[list[dict], dict]:
                if min_games not in _pool_zstats_cache:
                    _pool_zstats_cache[min_games] = draft_suggestions.pool_and_zstats(
                        conn, window, platform, min_games
                    )
                return _pool_zstats_cache[min_games]

            # Compositions suggérées : précalculées pour la région par défaut
            # (`platform == "all"`, matérialisées par le service collector,
            # cf. synergy/draft_suggestions.py) — affichées SANS clic (retour
            # utilisateur 2026-07-25 : "je voulais garder les drafts
            # proposées sans avoir à cliquer"). Pour toute autre région (ou
            # si rien n'est encore matérialisé, ex. juste après un déploiement),
            # calcul à la demande derrière le bouton, comme avant. Jusqu'à 3
            # variantes par archétype (retour utilisateur 2026-07-27, boutons
            # 1/2/3) : `propose_drafts`/`queries.draft_suggestions` renvoient
            # une liste PLATE, regroupée ici par archétype pour le template.
            suggested_drafts = None
            if platform == "all":
                precomputed = queries.draft_suggestions(conn, window, platform)
                if precomputed:
                    suggested_drafts = _group_draft_variants(
                        [_build_draft_result(raw, include_seed_pairs=False) for raw in precomputed]
                    )
            if suggested_drafts is None and suggest:
                pool, zstats = _get_pool_zstats()
                raws = draft_suggestions.propose_drafts(conn, window, platform, pool, zstats)
                suggested_drafts = _group_draft_variants(
                    [_build_draft_result(raw, include_seed_pairs=False) for raw in raws]
                )

            # Archétype "Personnalisé" de la section "Personnalise tes
            # poids" : sa PROPRE place, à part des archétypes fixes (retour
            # utilisateur 2026-07-28 — éviter de forcer une carte de plus
            # dans la grille des archétypes fixes).
            custom_draft = None
            if custom_weights is not None:
                pool, zstats = _get_pool_zstats(w_min_games)
                custom_raws = draft_suggestions.propose_for_weights(
                    conn,
                    window,
                    platform,
                    pool,
                    zstats,
                    "custom",
                    "Custom",
                    custom_weights,
                    min_games=w_min_games,
                )
                if custom_raws:
                    custom_draft = _group_draft_variants(
                        [_build_draft_result(raw, include_seed_pairs=False) for raw in custom_raws]
                    )[0]
                else:
                    custom_error = (
                        "Not enough reliable data for this weight split. Try other values."
                    )

            # "Contre cette équipe" (retour utilisateur 2026-08-11) : calcul
            # à la demande, comme "Compose à partir de tes champions" —
            # `enemy_picks` est une saisie utilisateur, jamais matérialisable
            # à l'avance par le collector. `DEFAULT_COUNTER_WEIGHTS` fixe
            # (pas de formulaire de poids personnalisés pour ce mode, pour
            # l'instant) — jusqu'à 3 variantes comme les autres archétypes.
            counter_drafts = None
            counter_error = None
            if enemy_picks:
                counter_raws = draft_suggestions.propose_counter_draft(
                    conn, window, platform, enemy_picks, min_games=enemy_min_games
                )
                if counter_raws:
                    counter_drafts = _group_draft_variants(
                        [_build_draft_result(raw, include_seed_pairs=False) for raw in counter_raws]
                    )[0]
                else:
                    counter_error = (
                        "Not enough reliable data against these picks."
                        " Try other champions or lower the reliability threshold."
                    )

            # Formulaire soumis dès que 1 champion ou un archétype est fourni
            # — une page fraîche n'a ni l'un ni l'autre dans l'URL. Archétype
            # non précisé (retour utilisateur 2026-07-26) : une proposition
            # par archétype (comme "Compositions suggérées"), pas obligé de
            # choisir à l'avance — chaque archétype réussi ou non
            # indépendamment, comme partout ailleurs sur cette page.
            manual_results: list[dict] = []
            manual_error = None
            if seed_picks or archetype:
                if (
                    archetype
                    and archetype != "custom"
                    and archetype not in draft_suggestions.ARCHETYPES
                ):
                    raise HTTPException(404, f"unknown archetype: {archetype!r}")
                if not seed_picks:
                    manual_error = "Pick at least 1 champion before completing the draft."
                elif archetype == "custom" and manual_custom_weights is None:
                    # "Personnalisé" choisi mais poids absents/invalides —
                    # jamais un plantage silencieux (retour utilisateur
                    # 2026-07-28).
                    manual_error = manual_custom_error or (
                        "Enter weights that add up to 100% above."
                    )
                else:
                    _, zstats = _get_pool_zstats(cw_min_games)
                    keys = [archetype] if archetype else list(draft_suggestions.ARCHETYPES)
                    for key in keys:
                        if key == "custom":
                            label, weights = "Custom", manual_custom_weights
                        else:
                            label = draft_suggestions.ARCHETYPES[key]["label"]
                            weights = draft_suggestions.ARCHETYPES[key]["weights"]
                        raw = _manual_propose(
                            conn,
                            window,
                            platform,
                            seed_picks,
                            key,
                            label,
                            weights,
                            zstats,
                            cw_min_games,
                        )
                        if raw is not None:
                            manual_results.append(_build_draft_result(raw))
                    if not manual_results:
                        manual_error = (
                            "Not enough reliable data to complete this composition."
                            " Try other champions or a different archetype."
                        )

        def _draft_url(**overrides: str) -> str:
            params = {
                **current_seed_params,
                **current_weight_params,
                **current_manual_weight_params,
                **current_enemy_params,
                "archetype": archetype or "",
                **overrides,
                "window": window or "",
                "platform": platform or "",
            }
            return "/draft?" + urlencode({k: v for k, v in params.items() if v})

        return templates.TemplateResponse(
            request,
            "draft.html",
            {
                **context,
                "draft_roles": DRAFT_ROLES,
                "champion_names": sorted(c.name for c in champ_index().values()),
                "suggested_drafts": suggested_drafts,
                "suggest_url": _draft_url(suggest="1"),
                "seed_names": {
                    role: (champ(seed_picks[role]).name if seed_picks.get(role) else "")
                    for role in DRAFT_ROLES
                },
                "enemy_names": {
                    role: (champ(enemy_picks[role]).name if enemy_picks.get(role) else "")
                    for role in DRAFT_ROLES
                },
                "counter_drafts": counter_drafts,
                "counter_error": counter_error,
                "enemy_min_games": enemy_min_games,
                "archetypes": draft_suggestions.ARCHETYPES,
                "selected_archetype": archetype or "",
                "manual_results": manual_results,
                "manual_error": manual_error,
                "custom_draft": custom_draft,
                "custom_error": custom_error,
                "custom_weight_axes": CUSTOM_WEIGHT_AXES,
                "custom_weight_axis_labels": DRAFT_ARCHETYPE_AXIS_LABELS,
                "custom_weight_values": {axis: v or "" for axis, v in raw_weights.items()},
                "manual_custom_weight_values": {
                    axis: v or "" for axis, v in raw_manual_weights.items()
                },
                "w_min_games": w_min_games,
                "cw_min_games": cw_min_games,
            },
        )

    # --- Profil de résilience par champion (Phase 8, retour utilisateur) ---
    #
    # Pas de "combinaison parfaite universelle" de métriques : un coefficient
    # global moyenne des chemins vers la victoire très différents selon le
    # champion (Nasus jungle mené au gold@15 dans 60 % de ses games, WR 34 %
    # dans cet état, mais ~52 % au global). Cette page montre, PAR CHAMPION,
    # l'écart de WR entre "en avance" et "en retard" sur chaque facteur —
    # `synergy.resilience`.

    _RESILIENCE_SORT_KEYS: dict[str, object] = {
        "gap": lambda r: r["gap"],
        "wr_behind": lambda r: r["wr_behind"],
        "wr_ahead": lambda r: r["wr_ahead"],
        "games": lambda r: r["games_ahead"] + r["games_behind"],
    }
    # 1er clic sur une colonne : écart croissant (le plus résilient en tête,
    # cohérent avec le titre de la page) ; WR/games décroissants (le plus
    # haut d'abord, plus naturel pour lire "qui performe le mieux").
    _RESILIENCE_DEFAULT_DIR = {
        "gap": "asc",
        "wr_behind": "desc",
        "wr_ahead": "desc",
        "games": "desc",
    }

    def _resilience_rows(
        conn,
        window: str,
        factor: str,
        *,
        role: str | None,
        min_games: int,
        min_gap: float | None,
        max_gap: float | None,
        min_wr_ahead: float | None,
        max_wr_ahead: float | None,
        min_wr_behind: float | None,
        max_wr_behind: float | None,
    ) -> tuple[list[dict], int]:
        """Retourne (lignes filtrées, nombre de lignes fiables AVANT les filtres
        par seuil) — le 2e sert à distinguer « rien de matérialisé pour cette
        fenêtre » de « des filtres trop stricts pour les valeurs réellement
        observées » (retour utilisateur 2026-07-20 : la page pointait vers la
        commande de matérialisation même quand la donnée existait déjà, ex.
        aucun champion ne dépasse 46 % de WR en retard sur la fenêtre actuelle
        — un filtre "WR en retard min. 50" est un choix naturel mais ne peut
        matcher aucune ligne, ce n'est pas un problème de matérialisation)."""
        rows = queries.champion_resilience(conn, window, factor, role=role)
        reliable_count = 0
        result = []
        for r in rows:
            games_ahead, wins_ahead = r["games_ahead"], r["wins_ahead"]
            games_behind, wins_behind = r["games_behind"], r["wins_behind"]
            if games_ahead == 0 or games_behind == 0:
                continue  # pas de comparaison possible sans les 2 côtés
            if (
                games_ahead < RESILIENCE_MIN_GAMES_PER_SIDE
                or games_behind < RESILIENCE_MIN_GAMES_PER_SIDE
            ):
                continue  # écart trop bruité pour être un signal (retour utilisateur 2026-07-20)
            reliable_count += 1
            if games_ahead + games_behind < min_games:
                continue
            wr_ahead = wins_ahead / games_ahead
            wr_behind = wins_behind / games_behind
            gap = wr_ahead - wr_behind
            if min_gap is not None and gap < min_gap:
                continue
            if max_gap is not None and gap > max_gap:
                continue
            if min_wr_ahead is not None and wr_ahead < min_wr_ahead:
                continue
            if max_wr_ahead is not None and wr_ahead > max_wr_ahead:
                continue
            if min_wr_behind is not None and wr_behind < min_wr_behind:
                continue
            if max_wr_behind is not None and wr_behind > max_wr_behind:
                continue
            c = champ(r["champion_id"])
            result.append(
                {
                    "champion_id": r["champion_id"],
                    "name": c.name,
                    "role": r["role"],
                    "role_label": RIOT_ROLE_LABELS[r["role"]],
                    "wr_ahead": wr_ahead,
                    "wr_behind": wr_behind,
                    "gap": gap,
                    "games_ahead": games_ahead,
                    "games_behind": games_behind,
                }
            )
        return result, reliable_count

    @app.get("/resilience", response_class=HTMLResponse)
    def resilience_page(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        factor: str = "team_gold_diff_15",
        role: str | None = None,
        sort: str = "gap",
        dir: str = "asc",
        min_games: int = Query(0, ge=0),
        min_gap: str | None = None,
        max_gap: str | None = None,
        min_wr_ahead: str | None = None,
        max_wr_ahead: str | None = None,
        min_wr_behind: str | None = None,
        max_wr_behind: str | None = None,
    ):
        if factor not in RESILIENCE_FACTOR_LABELS:
            raise HTTPException(404, f"unknown factor: {factor!r}")
        if role and role not in RIOT_ROLE_LABELS:
            raise HTTPException(404, f"unknown role: {role!r}")
        # Un <select> vide envoie `role=` (chaîne vide), pas une clé absente —
        # `champion_resilience` teste `role is not None` : sans cette
        # normalisation, choisir "tous" (valeur "") ajoutait silencieusement
        # `AND role = ''` en SQL, qui ne matche jamais rien (retour
        # utilisateur 2026-07-20 : "0 champions" dès le premier clic sur
        # Filtrer, avant même de toucher un filtre — le <select> Rôle envoie
        # toujours role= au submit).
        role = role or None
        sort = sort if sort in _RESILIENCE_SORT_KEYS else "gap"
        gap_bounds = (
            _parse_optional_float(min_gap, ge=-100, le=100),
            _parse_optional_float(max_gap, ge=-100, le=100),
        )
        wr_ahead_bounds = (
            _parse_optional_float(min_wr_ahead, ge=0, le=100),
            _parse_optional_float(max_wr_ahead, ge=0, le=100),
        )
        wr_behind_bounds = (
            _parse_optional_float(min_wr_behind, ge=0, le=100),
            _parse_optional_float(max_wr_behind, ge=0, le=100),
        )
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            rows, reliable_count = _resilience_rows(
                conn,
                window,
                factor,
                role=role,
                min_games=min_games,
                min_gap=None if gap_bounds[0] is None else gap_bounds[0] / 100.0,
                max_gap=None if gap_bounds[1] is None else gap_bounds[1] / 100.0,
                min_wr_ahead=None if wr_ahead_bounds[0] is None else wr_ahead_bounds[0] / 100.0,
                max_wr_ahead=None if wr_ahead_bounds[1] is None else wr_ahead_bounds[1] / 100.0,
                min_wr_behind=None if wr_behind_bounds[0] is None else wr_behind_bounds[0] / 100.0,
                max_wr_behind=None if wr_behind_bounds[1] is None else wr_behind_bounds[1] / 100.0,
            )
        rows.sort(key=_RESILIENCE_SORT_KEYS[sort], reverse=(dir == "desc"))

        filter_params = {
            "min_games": min_games or "",
            "min_gap": min_gap or "",
            "max_gap": max_gap or "",
            "min_wr_ahead": min_wr_ahead or "",
            "max_wr_ahead": max_wr_ahead or "",
            "min_wr_behind": min_wr_behind or "",
            "max_wr_behind": max_wr_behind or "",
        }

        def _sort_url(key: str) -> str:
            next_dir = (
                ("desc" if dir == "asc" else "asc") if sort == key else _RESILIENCE_DEFAULT_DIR[key]
            )
            params = {
                "window": window,
                "platform": platform,
                "factor": factor,
                "role": role or "",
                "sort": key,
                "dir": next_dir,
                **filter_params,
            }
            return "/resilience?" + urlencode({k: v for k, v in params.items() if v})

        sort_urls = {key: _sort_url(key) for key in _RESILIENCE_SORT_KEYS}
        return templates.TemplateResponse(
            request,
            "resilience.html",
            {
                **context,
                "rows": rows,
                "reliable_count": reliable_count,
                "factor": factor,
                "role": role or "",
                "min_games": min_games,
                "min_gap": min_gap or "",
                "max_gap": max_gap or "",
                "min_wr_ahead": min_wr_ahead or "",
                "max_wr_ahead": max_wr_ahead or "",
                "min_wr_behind": min_wr_behind or "",
                "max_wr_behind": max_wr_behind or "",
                "sort": sort,
                "dir": dir,
                "sort_urls": sort_urls,
            },
        )

    # --- Détecteur de picks flex/hybrides (Phase 8) ---
    #
    # Automatise la vérification manuelle faite en session sur Camille/Elise/
    # Twitch : un champion joué dans un rôle secondaire non-anecdotique
    # (agg_champion, historique complet) dont le profil de gold à 15 min
    # (match_role_stats, jeune) dévie de la moyenne du rôle — signal de méta
    # hybride (ex. bruiser/skirmisher en support), pas forcément un artefact.

    # Colonnes triables (retour utilisateur 2026-07-20 : pas de tri du tout
    # avant) — un seul critère actif à la fois, pas le tri multi-colonnes
    # façon tableur de `/`/`/duos` (pas nécessaire, le volume de picks est
    # petit et déjà filtré par les seuils de fiabilité). Tri par VALEUR
    # SIGNÉE brute, pas par magnitude (abs) : un premier essai triait
    # `deviation`/`dmg_deviation` par écart le plus marquant peu importe le
    # signe, mais ça mélange + et - sans que croissant/décroissant ne
    # redonne un ordre cohérent — contraire à la convention déjà en place
    # sur `/`/`/duos` (tri par valeur brute), source de confusion (retour
    # utilisateur 2026-07-20). `None` (pas de donnée dégâts/gold) toujours
    # en dernier quel que soit le sens, cf. `_sort_flex_picks` ci-dessous.
    _FLEX_SORT_KEYS: dict[str, object] = {
        "deviation": lambda p: p["gold_deviation"],
        "dmg_deviation": lambda p: p["dmg_deviation"],
        "wr_secondary": lambda p: p["wr_secondary"],
        "share": lambda p: p["share"],
        "games": lambda p: p["games_role"],
    }

    def _sort_flex_picks(picks: list[dict], key_fn, *, reverse: bool) -> list[dict]:
        """Trie par `key_fn`, `None` toujours en dernier quel que soit `reverse`
        (même convention que `NULLS LAST` en SQL, cf. `_order_by_clause`) —
        un simple `reverse=True` sur une liste contenant des `None` lèverait
        de toute façon un `TypeError` en les comparant à des floats."""
        with_value = [p for p in picks if key_fn(p) is not None]
        without_value = [p for p in picks if key_fn(p) is None]
        with_value.sort(key=key_fn, reverse=reverse)
        return with_value + without_value

    _FLEX_DEFAULT_DIR = {
        "deviation": "desc",
        "dmg_deviation": "desc",
        "wr_secondary": "desc",
        "share": "desc",
        "games": "desc",
    }

    def _flex_picks(conn, window: str, platform: str, *, role: str | None) -> list[dict]:
        distribution = queries.champion_role_distribution(conn, window, platform)
        totals: dict[int, int] = {}
        primary_role: dict[int, str] = {}
        primary_games: dict[int, int] = {}
        # (champion, role) -> (games, wins) — sert à comparer le WR du pick flex à
        # SON PROPRE WR au rôle principal, pas à la moyenne du rôle (retour
        # utilisateur 2026-08-14 : la moyenne d'un rôle sur tous les champions vaut
        # TOUJOURS exactement 50%, jeu à somme nulle — un gagnant/un perdant par
        # rôle et par game — donc "vs role" ne comparait en réalité qu'à 50%,
        # aucun signal réel malgré le libellé).
        champion_role_wr: dict[tuple[int, str], tuple[int, int]] = {}
        for row in distribution:
            cid, games, wins = row["champion_id"], row["games"], row["wins"]
            totals[cid] = totals.get(cid, 0) + games
            if games > primary_games.get(cid, -1):
                primary_games[cid] = games
                primary_role[cid] = row["role"]
            champion_role_wr[(cid, row["role"])] = (games, wins)

        # Matérialisé pour platform="all" (cas par défaut, retour
        # utilisateur 2026-08-12 : le calcul à la demande scanne
        # intégralement match_role_stats, ~17,7s par appel, sature l'I/O
        # sous quelques requêtes concurrentes) — les autres régions restent
        # en calcul à la demande, cas rare.
        if platform == "all":
            profiles = {
                (r["champion_id"], r["role"]): r
                for r in queries.role_resource_profile_materialized(
                    conn, window, min_games=FLEX_MIN_PROFILE_GAMES
                )
            }
            baseline = queries.role_resource_baseline_materialized(conn, window)
        else:
            profiles = {
                (r["champion_id"], r["role"]): r
                for r in queries.role_resource_profile(
                    conn, window, platform, min_games=FLEX_MIN_PROFILE_GAMES
                )
            }
            baseline = queries.role_resource_baseline(conn, window, platform)

        picks: list[dict] = []
        for row in distribution:
            cid, row_role, games, wins = (
                row["champion_id"],
                row["role"],
                row["games"],
                row["wins"],
            )
            if role and row_role != role:
                continue
            if row_role == primary_role[cid] or games < FLEX_MIN_ROLE_GAMES:
                continue
            share = games / totals[cid]
            if share < FLEX_ROLE_SHARE_THRESHOLD:
                continue
            profile = profiles.get((cid, row_role))
            base = baseline.get(row_role)
            if profile is None or base is None or not base["avg_gold_15"]:
                continue  # pas (encore) assez de match_role_stats pour ce rôle
            gold_ratio = profile["avg_gold_15"] / base["avg_gold_15"]
            gold_deviation = gold_ratio - 1  # signé : + au-dessus, - en dessous de la moyenne
            if abs(gold_deviation) < FLEX_MIN_DEVIATION:
                continue  # profil ~= la moyenne du rôle : pas un vrai signal hybride
            name = champ(cid).name
            direction = "above" if gold_deviation > 0 else "below"
            base_dmg_per_gold = base["avg_dmg_per_gold"]
            dmg_deviation = (
                profile["avg_dmg_per_gold"] / base_dmg_per_gold - 1
                if base_dmg_per_gold and profile["avg_dmg_per_gold"] is not None
                else None
            )
            wr_secondary = wins / games
            primary_games_n, primary_wins_n = champion_role_wr[(cid, primary_role[cid])]
            wr_primary = primary_wins_n / primary_games_n if primary_games_n else None
            wr_deviation = wr_secondary - wr_primary if wr_primary is not None else None
            picks.append(
                {
                    "champion_id": cid,
                    "name": name,
                    "role": row_role,
                    "role_label": RIOT_ROLE_LABELS[row_role],
                    "primary_role": primary_role[cid],
                    "primary_role_label": RIOT_ROLE_LABELS[primary_role[cid]],
                    "share": share,
                    "games_role": games,
                    "games_total": totals[cid],
                    "profile_n": profile["n"],
                    "gold_deviation": gold_deviation,
                    "direction": direction,
                    "dmg_deviation": dmg_deviation,
                    "wr_secondary": wr_secondary,
                    "wr_primary": wr_primary,
                    "wr_deviation": wr_deviation,
                    "sentence": (
                        f"{name} plays {RIOT_ROLE_LABELS[row_role]} in {100 * share:.0f}%"
                        f" of their games ({games}/{totals[cid]}). Gold profile"
                        f" {100 * abs(gold_deviation):.0f}% {direction} the role average."
                    ),
                }
            )
        return picks

    @app.get("/flex", response_class=HTMLResponse)
    def flex_page(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        role: str | None = None,
        sort: str = "deviation",
        dir: str = "desc",
    ):
        if role and role not in RIOT_ROLE_LABELS:
            raise HTTPException(404, f"unknown role: {role!r}")
        sort = sort if sort in _FLEX_SORT_KEYS else "deviation"
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            picks = _flex_picks(conn, window, platform, role=role)
        picks = _sort_flex_picks(picks, _FLEX_SORT_KEYS[sort], reverse=(dir == "desc"))

        def _sort_url(key: str) -> str:
            next_dir = (
                ("desc" if dir == "asc" else "asc") if sort == key else _FLEX_DEFAULT_DIR[key]
            )
            params = {
                "window": window,
                "platform": platform,
                "role": role or "",
                "sort": key,
                "dir": next_dir,
            }
            return "/flex?" + urlencode({k: v for k, v in params.items() if v})

        sort_urls = {key: _sort_url(key) for key in _FLEX_SORT_KEYS}
        return templates.TemplateResponse(
            request,
            "flex.html",
            {
                **context,
                "picks": picks,
                "role": role or "",
                "sort": sort,
                "dir": dir,
                "sort_urls": sort_urls,
            },
        )

    # --- API JSON ---

    def _named(row: dict) -> dict:
        """Ajoute les noms de champions aux ids d'une ligne de score."""
        out = dict(row)
        for key in (
            "jgl_champion",
            "mid_champion",
            "sup_champion",
            "champ_a",
            "champ_b",
        ):
            if key in out:
                out[key + "_name"] = champ(out[key]).name
        return out

    @app.get("/api/status")
    def api_status(request: Request):
        with request.app.state.pool.connection() as conn:
            return queries.collection_status(conn)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, _admin: str = Depends(require_admin)):
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, None, None)
            status = queries.collection_status(conn)
            gaps = queries.collector_gaps(conn)
            sizes = queries.table_sizes(conn)

        by_day: dict[str, dict[str, int]] = {}
        platforms_seen: list[str] = []
        for row in status["matches_per_day"]:
            day, platform_row, matches = row["day"], row["platform"], row["matches"]
            by_day.setdefault(day, {})[platform_row] = matches
            if platform_row not in platforms_seen:
                platforms_seen.append(platform_row)
        days_sorted = sorted(by_day)
        platforms_seen.sort()
        per_day_chart = {
            "days": days_sorted,
            "platforms": platforms_seen,
            "series": {p: [by_day[d].get(p, 0) for d in days_sorted] for p in platforms_seen},
        }
        sizes_chart = {
            "labels": [row["table_name"] for row in sizes],
            "bytes": [row["bytes"] for row in sizes],
        }

        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                **context,
                "total_matches": status["total_matches"],
                "last_collected_at": status["last_collected_at"],
                "journal": status["journal"],
                "gaps": gaps,
                "table_sizes": sizes,
                "per_day_chart_json": json.dumps(per_day_chart),
                "sizes_chart_json": json.dumps(sizes_chart),
            },
        )

    @app.get("/api/windows")
    def api_windows(request: Request):
        with request.app.state.pool.connection() as conn:
            known = queries.available_windows(conn)
            return {
                "windows": [
                    {"label": label, "platforms": queries.available_platforms(conn, label)}
                    for label in known
                ]
            }

    @app.get("/api/champions")
    def api_champions():
        return {
            "champions": [vars(c) for c in sorted(champ_index().values(), key=lambda c: c.name)]
        }

    @app.get("/api/trios")
    def api_trios(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        jgl: str | None = None,
        mid: str | None = None,
        sup: str | None = None,
        min_games: int = Query(0, ge=0),
        min_tier: str = Query("faible", pattern="^(faible|moyen|eleve)$"),
        sort: str = "synergy",
        direction: str = Query("desc", alias="dir"),
        page: int = Query(1, ge=1),
    ):
        sorts, dirs = parse_sort(sort, direction, queries.TRIO_SORTS)
        with request.app.state.pool.connection() as conn:
            window, platform, _ = resolve_context(conn, window, platform)
            result = queries.trio_tierlist(
                conn,
                window,
                platform,
                jgl_champion_id=resolve_champion(jgl),
                mid_champion_id=resolve_champion(mid),
                sup_champion_id=resolve_champion(sup),
                min_games=min_games,
                min_tier=min_tier,
                min_values=parse_thresholds(threshold_raw(request, prefix="min")),
                max_values=parse_thresholds(threshold_raw(request, prefix="max")),
                sort=sorts,
                direction=dirs,
                page=page,
            )
        result["rows"] = [_named(r) for r in result["rows"]]
        return {"window": window, "platform": platform, **result}

    @app.get("/api/trios/{jgl}/{mid}/{sup}")
    def api_trio_detail(
        request: Request,
        jgl: int,
        mid: int,
        sup: int,
        window: str | None = None,
        platform: str | None = None,
    ):
        with request.app.state.pool.connection() as conn:
            window, platform, _ = resolve_context(conn, window, platform)
            detail = _trio_detail(conn, window, platform, jgl, mid, sup)
        detail["score"] = _named(detail["score"])
        detail["duos"] = [_named(r) for r in detail["duos"]]
        return {"window": window, "platform": platform, **detail}

    @app.get("/api/duos")
    def api_duos(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        roles: str = Query("jgl_mid", pattern=_DUO_ROLES_PATTERN),
        champ_a: str | None = None,
        champ_b: str | None = None,
        min_games: int = Query(0, ge=0),
        min_tier: str = Query("faible", pattern="^(faible|moyen|eleve)$"),
        sort: str = "synergy",
        direction: str = Query("desc", alias="dir"),
        page: int = Query(1, ge=1),
    ):
        sorts, dirs = parse_sort(sort, direction, queries.DUO_SORTS)
        all_roles = roles == "all"
        with request.app.state.pool.connection() as conn:
            window, platform, _ = resolve_context(conn, window, platform)
            result = queries.duo_tierlist(
                conn,
                window,
                platform,
                None if all_roles else roles,
                champ_a_id=None if all_roles else resolve_champion(champ_a),
                champ_b_id=None if all_roles else resolve_champion(champ_b),
                min_games=min_games,
                min_tier=min_tier,
                min_values=parse_thresholds(threshold_raw(request, prefix="min")),
                max_values=parse_thresholds(threshold_raw(request, prefix="max")),
                sort=sorts,
                direction=dirs,
                page=page,
            )
        result["rows"] = [_named(r) for r in result["rows"]]
        return {"window": window, "platform": platform, "roles": roles, **result}

    @app.get("/api/duos/{roles}/{champ_a}/{champ_b}")
    def api_duo_detail(
        request: Request,
        roles: str,
        champ_a: int,
        champ_b: int,
        window: str | None = None,
        platform: str | None = None,
    ):
        if roles not in queries.DUO_ROLES:
            raise HTTPException(404, f"unknown roles: {roles!r}")
        with request.app.state.pool.connection() as conn:
            window, platform, _ = resolve_context(conn, window, platform)
            detail = _duo_detail(conn, window, platform, roles, champ_a, champ_b)
        detail["score"] = _named(detail["score"])
        detail["best_trios"] = [_named(r) for r in detail["best_trios"]]
        return {"window": window, "platform": platform, "roles": roles, **detail}

    return app
