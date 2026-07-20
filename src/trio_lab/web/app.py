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
import math
import secrets
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
from trio_lab.synergy.compute import DUO_ROLES
from trio_lab.synergy.gold_factors import BLOCK_OF as GOLD_FACTOR_BLOCK_OF
from trio_lab.synergy.gold_factors import CONTINUOUS as GOLD_FACTOR_CONTINUOUS
from trio_lab.synergy.gold_factors import FEATURES as GOLD_FACTOR_FEATURES
from trio_lab.synergy.win_factors import FEATURES as WIN_FACTOR_FEATURES
from trio_lab.synergy.windows import make_window
from trio_lab.web import champions, queries, summary

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent

ROLE_LABELS = {"jgl": "Jungle", "mid": "Mid", "sup": "Support", "top": "Top", "bot": "ADC"}
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
# Ordre fixe de parcours des 10 slots (avance auto après un pick, choix du
# 1er slot vide par défaut) — retour utilisateur 2026-07-19 : interface façon
# champ select, un seul rôle "actif" à la fois plutôt qu'une liste par slot.
DRAFT_SLOT_ORDER = tuple(f"{side}_{role}" for side in ("blue", "red") for role in DRAFT_ROLES)
# Seuil de grisage (pas de filtre, retour utilisateur 2026-07-19) : sous ce
# games_eff la suggestion reste affichée mais visuellement atténuée — même
# esprit que GOLD_DIFF_LOW_SAMPLE_PCT, cohérent avec le tier 'moyen' (≥ 50).
DRAFT_MIN_GAMES_EFF = 50.0
# Nombre de candidats mis en avant ("Recommandé") en tête de la grille
# complète — le reste du roster reste visible/pickable en dessous, comme en
# champ select LoL (retour utilisateur 2026-07-19 : jamais de liste tronquée
# qui masque des champions).
DRAFT_RECOMMENDED_COUNT = 12
# Plancher de fiabilité pour le signal "sécurité blind pick" (pire matchup
# connu) — mêmes games_eff que le tier 'moyen' des matchups (score_matchup),
# affiché uniquement quand aucun ennemi même rôle n'est verrouillé.
DRAFT_SAFETY_MIN_GAMES_EFF = 50.0
# Delta à partir duquel un matchup compte comme un "contre notable" pour le
# signal de sécurité blind pick (retour utilisateur 2026-07-19 : compter les
# contres plutôt que ne montrer que le pire — un champion avec dix contres
# à -3 % est un risque différent d'un champion avec un seul à -15 %, ce que
# le pire cas seul ne distingue pas). -3 pts de WR : repère arbitraire mais
# cohérent avec l'amplitude typique des deltas de score_matchup.
DRAFT_NOTABLE_COUNTER_DELTA = -0.03
# Libellés lisibles pour le dashboard /insights (synergy.win_factors.FEATURES).
WIN_FACTOR_LABELS = {
    "team_gold_diff_15": "Avantage gold d'ÉQUIPE à 15 min",
    "team_cc_per_min": "CC d'équipe / min",
    "team_vision_per_min": "Vision d'équipe / min",
    "jgl_cs_diff_15": "CS jungle vs adverse à 15 min",
    "herald_taken": "Héraut pris",
    "soul_taken": "Âme de dragon",
    "first_tower": "Première tour",
}
# Libellés lisibles pour la section "qu'est-ce qui construit l'avantage au
# gold" de /insights (synergy.gold_factors.FEATURES).
GOLD_FACTOR_LABELS = {
    "team_baseline_wr": "Force brute des picks (WR baseline)",
    "team_matchup_delta": "Avantage de matchup (vs même rôle adverse)",
    "team_trio_synergy": "Synergie du trio jungle/mid/support",
    "jgl_cs_diff_15": "CS jungle vs adverse à 15 min",
    "first_blood_team": "Premier sang",
    "herald_taken_pre15": "Héraut pris avant 15 min",
    "dragons_taken_pre15": "Dragons pris avant 15 min",
    "wards_pre15": "Wards posées/détruites avant 15 min",
}
# Profil de résilience par champion (Phase 8, /resilience, retour
# utilisateur 2026-07-20) : mêmes 3 facteurs que synergy.resilience.FACTORS,
# choisis pour leur signal réel et leur indépendance mutuelle (corrélations
# de Pearson vérifiées en session).
RESILIENCE_FACTOR_LABELS = {
    "team_gold_diff_15": "Avantage gold d'équipe à 15 min",
    "jgl_cs_diff_15": "CS jungle d'équipe à 15 min",
    "first_blood_team": "Premier sang d'équipe",
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
            status_code=401, detail="Non autorisé", headers={"WWW-Authenticate": "Basic"}
        )
    return credentials.username


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{100 * value:.{digits}f} %"


def _fmt_pct100(value: float | None, digits: int = 0) -> str:
    """Comme `pct`, mais pour une valeur déjà sur l'échelle 0-100 (pas 0-1)."""
    return "—" if value is None else f"{value:.{digits}f} %"


def _fmt_signed_pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{100 * value:+.{digits}f} %"


def _fmt_signed_int(value: float | None) -> str:
    return "—" if value is None else f"{value:+,.0f}".replace(",", " ")


def _fmt_num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _fmt_duration(value: float | None) -> str:
    if value is None:
        return "—"
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
        return "—"
    size = float(value)
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024 or unit == "Go":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} Go"


def _fmt_since(value: datetime | None) -> str:
    """Ancienneté relative d'un horodatage (« il y a 4 min »).

    Pas d'apostrophe dans le texte (ex. « à l'instant ») : Jinja l'échapperait
    en `&#39;` dans le HTML rendu, ce qui casse toute comparaison de chaîne
    littérale côté tests — vécu.
    """
    if value is None:
        return "jamais"
    delta = datetime.now(UTC) - value
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "il y a quelques secondes"
    if minutes < 60:
        return f"il y a {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"il y a {hours} h"
    return f"il y a {hours // 24} j"


def create_app(*, dsn: str | None = None, champion_index=None) -> FastAPI:
    """Construit l'application. `champion_index` : injecté par les tests."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = ConnectionPool(db.require_dsn(dsn), min_size=1, max_size=4, open=True)
        yield
        app.state.pool.close()

    app = FastAPI(title="Trio Lab", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    templates = Jinja2Templates(directory=_HERE / "templates")

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
    templates.env.globals["draft_recommended_count"] = DRAFT_RECOMMENDED_COUNT
    templates.env.globals["gold_factor_continuous"] = GOLD_FACTOR_CONTINUOUS
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
    templates.env.globals["ROLE_TO_TEAM_POSITION"] = ROLE_TO_TEAM_POSITION
    templates.env.globals["DUO_ROLE_KEYS"] = DUO_ROLE_KEYS
    templates.env.globals["RIOT_ROLE_LABELS"] = RIOT_ROLE_LABELS
    templates.env.globals["RESILIENCE_FACTOR_LABELS"] = RESILIENCE_FACTOR_LABELS

    def resolve_champion(name_or_id: str | None) -> int | None:
        """Filtre champion de la tier list : nom (recherche) ou id. None si vide."""
        if not name_or_id or not name_or_id.strip():
            return None
        text = name_or_id.strip()
        if text.isdigit():
            return int(text)
        found = champions.name_lookup(champ_index()).get(text.casefold())
        if found is None:
            raise HTTPException(404, f"champion inconnu : {text}")
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
            raise HTTPException(404, f"tri invalide : sort={sort_param!r} dir={dir_param!r}")
        for s in sorts:
            if s not in valid:
                raise HTTPException(404, f"tri inconnu : {s!r}")
        for d in dirs:
            if d not in queries.SORT_DIRECTIONS:
                raise HTTPException(404, f"sens de tri inconnu : {d!r}")
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
            raise HTTPException(404, f"valeur numérique invalide : {value!r}") from None
        if ge is not None and parsed < ge:
            raise HTTPException(404, f"valeur trop basse : {value!r}")
        if le is not None and parsed > le:
            raise HTTPException(404, f"valeur trop haute : {value!r}")
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
        "vision": (False, 0, None),
        "drakes": (False, 0, None),
        "soul": (True, 0, 100),
        "herald": (True, 0, 100),
        "tower1": (True, 0, 100),
        "cc": (False, 0, None),
        "cc_blend": (False, 0, 100),
        "scaling": (True, -100, 100),
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
        known = queries.available_windows(conn)
        if not known:
            raise HTTPException(503, "aucun score matérialisé (lancer python -m trio_lab.synergy)")
        if window is None:
            window = known[0]
        elif window not in known:
            raise HTTPException(404, f"fenêtre non matérialisée : {window}")
        platforms = queries.available_platforms(conn, window)
        if platform is None:
            platform = platforms[0]
        elif platform not in platforms:
            raise HTTPException(404, f"plateforme absente de la fenêtre : {platform}")
        freshness = queries.window_freshness(conn, window)
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
            raise HTTPException(404, "trio non scoré sur cette fenêtre/plateforme")
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
        cc_scores = queries.cc_theoretical_scores(conn)
        jgl_cc, mid_cc, sup_cc = cc_scores.get(jgl), cc_scores.get(mid), cc_scores.get(sup)
        members_cc = (jgl_cc, mid_cc, sup_cc)
        # Total seulement si les 3 membres sont résolus (sinon somme partielle
        # trompeuse — affichée comme « — » à la place).
        trio_cc_raw = sum(members_cc) if None not in members_cc else None
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
            "cc_theoretical": {"jgl": jgl_cc, "mid": mid_cc, "sup": sup_cc, "trio": trio_cc_raw},
            # Pourcentages 0-100 déjà matérialisés par synergy.compute (mêmes
            # valeurs que la tier list, jamais recalculés ici : évite toute
            # dérive et tout accès au fichier gelé côté service web — absent de
            # l'image Docker (cf. Dockerfile), seul `ccref.sync_theoretical`
            # (run local, one-shot) en dépend.
            "cc_scores": {
                "theoretical_pct": score["cc_theoretical_pct"],
                "empirical_pct": score["cc_empirical_pct"],
                "blended_pct": score["cc_blended_pct"],
            },
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
            raise HTTPException(404, "duo non scoré sur cette fenêtre/plateforme")
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
        cc_scores = queries.cc_theoretical_scores(conn)
        a_cc, b_cc = cc_scores.get(champ_a), cc_scores.get(champ_b)
        members_cc = (a_cc, b_cc)
        # Total seulement si les 2 membres sont résolus (sinon somme partielle
        # trompeuse — affichée comme « — » à la place).
        duo_cc_raw = sum(members_cc) if None not in members_cc else None
        member_wr = {
            "a": queries.member_wr(conn, patches, platform, role_a, champ_a, weights),
            "b": queries.member_wr(conn, patches, platform, role_b, champ_b, weights),
        }
        return {
            "score": score,
            "stats": stats,
            "member_wr": member_wr,
            "best_trios": best_trios,
            "cc_theoretical": {"a": a_cc, "b": b_cc, "duo": duo_cc_raw},
            # Pourcentages 0-100 déjà matérialisés par synergy.compute (mêmes
            # valeurs que la tier list, jamais recalculés ici — cf. `_trio_detail`.
            "cc_scores": {
                "theoretical_pct": score["cc_theoretical_pct"],
                "empirical_pct": score["cc_empirical_pct"],
                "blended_pct": score["cc_blended_pct"],
            },
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
            raise HTTPException(404, f"roles inconnu : {roles!r}")
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
            raise HTTPException(404, "champion non scoré dans ce rôle sur cette fenêtre")
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
        cc_theoretical = queries.cc_theoretical_scores(conn).get(champion_id)
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
            "cc_theoretical": cc_theoretical,
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
            raise HTTPException(404, f"rôle inconnu : {role!r}")
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            detail = _champion_detail(conn, window, platform, role, champion_id)
        return templates.TemplateResponse(request, "champion.html", {**context, **detail})

    # --- Simulateur de draft (Phase 8) ---
    #
    # État entièrement dans l'URL (query params), pas de session serveur —
    # même principe que window/platform ailleurs. Chaque pick redirige vers
    # /draft avec un paramètre de plus ; hx-boost (base.html) fait le swap.

    def _first_empty_slot(picks: dict) -> str | None:
        for key in DRAFT_SLOT_ORDER:
            side, role = key.split("_", 1)
            if picks[side][role] is None:
                return key
        return None

    def _draft_role_grid(
        conn,
        window: str,
        platform: str,
        role: str,
        locked_allies: list[tuple[str, int]],
        locked_enemy: int | None,
        banned: set[int],
    ) -> dict:
        """Roster complet (pickable) pour le rôle actif — trié par
        edge = Σ synergie alliés verrouillés + delta counter vs l'ennemi même
        rôle verrouillé (même unité partout, points de WR, donc sommable sans
        pondération arbitraire), puis par fiabilité (`low_sample`) et enfin
        par WR baseline pour les champions sans edge — sans ce 2e critère,
        un champion à 25 games peut passer devant un champion à 1280 games
        pour un écart de WR qui n'est que du bruit (`wr` n'est jamais lissé,
        contrairement à `edge`). Contrairement à l'ancienne version, jamais
        de liste tronquée : tout le roster reste visible/pickable, seuls les
        `DRAFT_RECOMMENDED_COUNT` premiers sont badgés « Recommandé »
        (retour utilisateur 2026-07-19, interface façon champ select).
        """
        edge: dict[int, float] = {}
        reliability: dict[int, float] = {}

        def _accumulate(rows: list[dict], value_key: str, id_key: str) -> None:
            for row in rows:
                cid = row[id_key]
                if cid in banned:
                    continue
                value = row[value_key]
                if value is None:
                    continue
                edge[cid] = edge.get(cid, 0.0) + value
                games_eff = row["games_eff"]
                reliability[cid] = min(reliability.get(cid, games_eff), games_eff)

        for ally_role, ally_champ in locked_allies:
            roles_pair = _DRAFT_ROLES_BY_PAIR.get(frozenset({ally_role, role}))
            if roles_pair is None:
                continue
            partners = queries.champion_best_partners(
                conn, window, platform, roles_pair, ally_role, ally_champ, 300, min_tier="faible"
            )
            _accumulate(partners, "synergy", "partner_champion")

        blind = locked_enemy is None
        if not blind:
            enemy_matchups = queries.matchup_candidates(
                conn, window, platform, DRAFT_ROLE_TO_TEAM_POSITION[role], locked_enemy, 300
            )
            _accumulate(enemy_matchups, "delta", "candidate_champion")

        # Sécurité "blind pick" : pire contre connu + nombre de contres
        # notables, seulement pertinent quand aucun ennemi même rôle n'est
        # encore verrouillé (sinon le delta du counter réel prime déjà,
        # cf. `edge`).
        safety = (
            queries.role_worst_matchups(
                conn,
                window,
                platform,
                DRAFT_ROLE_TO_TEAM_POSITION[role],
                min_games_eff=DRAFT_SAFETY_MIN_GAMES_EFF,
                notable_delta=DRAFT_NOTABLE_COUNTER_DELTA,
            )
            if blind
            else {}
        )

        baseline = queries.champion_role_baseline_list(
            conn, window, platform, DRAFT_ROLE_TO_TEAM_POSITION[role], 500
        )
        wr_by_champ = {r["candidate_champion"]: r["wr"] for r in baseline}
        games_by_champ = {r["candidate_champion"]: r["games"] for r in baseline}

        roster = []
        for cid in set(wr_by_champ) | set(edge):
            if cid in banned:
                continue
            games_eff = reliability.get(cid, games_by_champ.get(cid, 0))
            roster.append(
                {
                    "champion_id": cid,
                    "name": champ(cid).name,
                    "edge": edge.get(cid),
                    "wr": wr_by_champ.get(cid),
                    "games_eff": games_eff,
                    "low_sample": games_eff < DRAFT_MIN_GAMES_EFF,
                    "safety": safety.get(cid) if blind else None,
                }
            )
        # Fiabilité avant WR brut (retour utilisateur 2026-07-19) : sans
        # alliés/ennemis verrouillés, `wr` n'est pas lissé (contrairement à
        # `edge`, déjà lissé côté score_duo/score_matchup) — un champion à
        # 25 games peut sinon passer devant un champion à 1280 games pour un
        # écart de WR qui n'est que du bruit. `low_sample` (déjà calculé,
        # même seuil que le grisage visuel) trie tout le monde à fiabilité
        # suffisante avant les échantillons faibles, sans jamais les masquer.
        roster.sort(
            key=lambda r: (
                r["edge"] is None,
                r["low_sample"],
                -(r["edge"] or 0.0),
                -(r["wr"] or 0.0),
            )
        )
        for i, r in enumerate(roster):
            r["recommended"] = i < DRAFT_RECOMMENDED_COUNT and r["edge"] is not None

        return {"roster": roster, "blind": blind}

    @app.get("/draft", response_class=HTMLResponse)
    def draft_page(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        blue_top: str | None = None,
        blue_jgl: str | None = None,
        blue_mid: str | None = None,
        blue_bot: str | None = None,
        blue_sup: str | None = None,
        red_top: str | None = None,
        red_jgl: str | None = None,
        red_mid: str | None = None,
        red_bot: str | None = None,
        red_sup: str | None = None,
        bans: str | None = None,
        active: str | None = None,
    ):
        raw = {
            "blue": {
                "top": blue_top,
                "jgl": blue_jgl,
                "mid": blue_mid,
                "bot": blue_bot,
                "sup": blue_sup,
            },
            "red": {"top": red_top, "jgl": red_jgl, "mid": red_mid, "bot": red_bot, "sup": red_sup},
        }
        # État courant de l'URL, pour reconstruire les liens de pick/retrait
        # sans jamais perdre un slot déjà posé (même principe que filters_qs).
        current_params = {
            f"{side}_{role}": v for side, roles in raw.items() for role, v in roles.items()
        }
        current_params["bans"] = bans or ""

        def _draft_url(**overrides: str) -> str:
            params = {
                **current_params,
                **overrides,
                "window": window or "",
                "platform": platform or "",
            }
            return "/draft?" + urlencode({k: v for k, v in params.items() if v})

        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            picks = {
                side: {role: resolve_champion(v) for role, v in roles.items()}
                for side, roles in raw.items()
            }
            banned = {resolve_champion(b) for b in (bans or "").split(",") if b.strip()}
            banned |= {c for roles in picks.values() for c in roles.values() if c is not None}

            # Slot "actif" : celui dont la grille de champions est affichée —
            # un seul à la fois (façon champ select), pas une liste par slot
            # vide. Un param explicite invalide/déjà rempli retombe sur le
            # 1er slot vide (ordre fixe DRAFT_SLOT_ORDER).
            if (
                active not in DRAFT_SLOT_ORDER
                or picks[active.split("_", 1)[0]][active.split("_", 1)[1]] is not None
            ):
                active = _first_empty_slot(picks)
            current_params["active"] = active or ""

            grid = None
            if active is not None:
                side, role = active.split("_", 1)
                other_side = "red" if side == "blue" else "blue"
                locked_allies = [(r, c) for r, c in picks[side].items() if c is not None]
                grid = _draft_role_grid(
                    conn, window, platform, role, locked_allies, picks[other_side][role], banned
                )
                for item in grid["roster"]:
                    # Pas d'override `active` explicite : une fois ce slot
                    # rempli, le calcul ci-dessus retombe naturellement sur
                    # le slot vide suivant — avance automatique.
                    item["pick_url"] = _draft_url(**{active: item["name"]}, active="")

            slot_urls = {
                f"{side}_{role}": (
                    _draft_url(**{f"{side}_{role}": ""}, active=f"{side}_{role}")
                    if picks[side][role] is not None
                    else _draft_url(active=f"{side}_{role}")
                )
                for side in ("blue", "red")
                for role in DRAFT_ROLES
            }

        return templates.TemplateResponse(
            request,
            "draft.html",
            {
                **context,
                "picks": picks,
                "bans_raw": bans or "",
                "banned": banned,
                "active": active,
                "grid": grid,
                "slot_urls": slot_urls,
                "draft_roles": DRAFT_ROLES,
                "champion_names": sorted(c.name for c in champ_index().values()),
            },
        )

    # --- Dashboard "ce qui fait gagner" (Phase 8) ---

    def _win_prob_swing(intercept_row: dict | None, feature_row: dict | None) -> tuple | None:
        """(P0, P1) = probabilité de victoire dans le scénario "moyen" de la
        population (stats continues à la moyenne, aucun objectif pris) vs le
        même scénario avec CE facteur activé (+1 écart-type si continu,
        présent si booléen) — tout le reste inchangé. Les coefficients sont
        déjà à l'échelle du logit, donc P1 = sigmoid(intercept + coef) sans
        transformation supplémentaire. Lecture non technique en probabilité
        absolue plutôt qu'en odds ratio seul (retour utilisateur 2026-07-19 :
        un odds ratio surestime l'effet perçu quand l'issue est ~50 %, pas
        rare — cf. Persoskie & Ferrer 2017, *Am J Prev Med* 52(2):224-228)."""
        if intercept_row is None or feature_row is None:
            return None

        def sigmoid(x: float) -> float:
            return 1.0 / (1.0 + math.exp(-x))

        b0 = intercept_row["coef"]
        return sigmoid(b0), sigmoid(b0 + feature_row["coef"])

    def _combined_win_factors(all_rows: list[dict], behind_rows: list[dict]) -> list[dict]:
        """Une ligne par feature (ordre fixe WIN_FACTOR_FEATURES, pas l'ordre
        SQL), avec les 2 populations côte à côte — jamais 2 tableaux séparés :
        même ligne = même feature, garanti, pas juste par coïncidence d'ordre
        (retour utilisateur 2026-07-19). `intercept` jamais affiché comme
        ligne (pas actionnable pour un coach), seulement utilisé en interne
        pour convertir les odds ratio en probabilité absolue."""
        all_by_feature = {r["feature"]: r for r in all_rows}
        behind_by_feature = {r["feature"]: r for r in behind_rows}
        all_intercept = all_by_feature.get("intercept")
        behind_intercept = behind_by_feature.get("intercept")
        return [
            {
                "feature": f,
                "label": WIN_FACTOR_LABELS[f],
                "all": all_by_feature.get(f),
                "behind": behind_by_feature.get(f),
                "all_prob": _win_prob_swing(all_intercept, all_by_feature.get(f)),
                "behind_prob": _win_prob_swing(behind_intercept, behind_by_feature.get(f)),
            }
            for f in WIN_FACTOR_FEATURES
            if f in all_by_feature or f in behind_by_feature
        ]

    def _ordered_gold_factors(rows: list[dict]) -> list[dict]:
        """Une ligne par feature (ordre fixe GOLD_FACTOR_FEATURES), avec son
        bloc (draft/exécution) pour le regroupement visuel — les lignes de
        diagnostic `_r2_draft_only`/`_r2_full` sont exclues ici, extraites à
        part par l'appelant."""
        by_feature = {r["feature"]: r for r in rows}
        return [
            {
                "feature": f,
                "label": GOLD_FACTOR_LABELS[f],
                "block": GOLD_FACTOR_BLOCK_OF[f],
                "row": by_feature[f],
            }
            for f in GOLD_FACTOR_FEATURES
            if f in by_feature
        ]

    @app.get("/insights", response_class=HTMLResponse)
    def insights_page(request: Request, window: str | None = None, platform: str | None = None):
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            all_rows = queries.win_factors(conn, window, "all")
            behind_rows = queries.win_factors(conn, window, "behind_gold15")
            gold_rows = queries.gold_factors(conn, window)
        factors = _combined_win_factors(all_rows, behind_rows)
        all_n = all_rows[0]["n"] if all_rows else 0
        behind_n = behind_rows[0]["n"] if behind_rows else 0

        gold_by_feature = {r["feature"]: r for r in gold_rows}
        gold_factors_list = _ordered_gold_factors(gold_rows)
        gold_n = gold_rows[0]["n"] if gold_rows else 0
        r2_draft_only = gold_by_feature.get("_r2_draft_only")
        r2_full = gold_by_feature.get("_r2_full")

        return templates.TemplateResponse(
            request,
            "insights.html",
            {
                **context,
                "factors": factors,
                "all_n": all_n,
                "behind_n": behind_n,
                "gold_factors": gold_factors_list,
                "gold_n": gold_n,
                "r2_draft_only": r2_draft_only["coef"] if r2_draft_only else None,
                "r2_full": r2_full["coef"] if r2_full else None,
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
            raise HTTPException(404, f"facteur inconnu : {factor!r}")
        if role and role not in RIOT_ROLE_LABELS:
            raise HTTPException(404, f"rôle inconnu : {role!r}")
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

    def _flex_picks(conn, window: str, platform: str, *, role: str | None) -> list[dict]:
        distribution = queries.champion_role_distribution(conn, window, platform)
        totals: dict[int, int] = {}
        primary_role: dict[int, str] = {}
        primary_games: dict[int, int] = {}
        for row in distribution:
            cid, games = row["champion_id"], row["games"]
            totals[cid] = totals.get(cid, 0) + games
            if games > primary_games.get(cid, -1):
                primary_games[cid] = games
                primary_role[cid] = row["role"]

        profiles = {
            (r["champion_id"], r["role"]): r
            for r in queries.role_resource_profile(
                conn, window, platform, min_games=FLEX_MIN_PROFILE_GAMES
            )
        }
        baseline = queries.role_resource_baseline(conn, window, platform)

        picks: list[dict] = []
        for row in distribution:
            cid, row_role, games = row["champion_id"], row["role"], row["games"]
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
            deviation = abs(gold_ratio - 1)
            if deviation < FLEX_MIN_DEVIATION:
                continue  # profil ~= la moyenne du rôle : pas un vrai signal hybride
            name = champ(cid).name
            direction = "au-dessus" if gold_ratio > 1 else "en dessous"
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
                    "gold_ratio": gold_ratio,
                    "deviation": deviation,
                    "direction": direction,
                    "dmg_per_gold": profile["avg_dmg_per_gold"],
                    "baseline_dmg_per_gold": base["avg_dmg_per_gold"],
                    "sentence": (
                        f"{name} joue {RIOT_ROLE_LABELS[row_role]} dans {100 * share:.0f} %"
                        f" de ses games ({games}/{totals[cid]}) — profil de gold"
                        f" {100 * deviation:.0f} % {direction} de la moyenne du rôle."
                    ),
                }
            )
        picks.sort(key=lambda p: p["deviation"], reverse=True)
        return picks

    @app.get("/flex", response_class=HTMLResponse)
    def flex_page(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        role: str | None = None,
    ):
        if role and role not in RIOT_ROLE_LABELS:
            raise HTTPException(404, f"rôle inconnu : {role!r}")
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            picks = _flex_picks(conn, window, platform, role=role)
        return templates.TemplateResponse(
            request, "flex.html", {**context, "picks": picks, "role": role or ""}
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
            raise HTTPException(404, f"roles inconnu : {roles!r}")
        with request.app.state.pool.connection() as conn:
            window, platform, _ = resolve_context(conn, window, platform)
            detail = _duo_detail(conn, window, platform, roles, champ_a, champ_b)
        detail["score"] = _named(detail["score"])
        detail["best_trios"] = [_named(r) for r in detail["best_trios"]]
        return {"window": window, "platform": platform, "roles": roles, **detail}

    return app
