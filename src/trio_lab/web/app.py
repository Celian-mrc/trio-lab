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
DRAFT_CANDIDATES_SHOWN = 15
# Seuil de grisage (pas de filtre, retour utilisateur 2026-07-19) : sous ce
# games_eff la suggestion reste affichée mais visuellement atténuée — même
# esprit que GOLD_DIFF_LOW_SAMPLE_PCT, cohérent avec le tier 'moyen' (≥ 50).
DRAFT_MIN_GAMES_EFF = 50.0
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

    def threshold_raw(request: Request) -> dict[str, str]:
        """Valeurs brutes des filtres de seuil (chaînes telles que soumises,
        `""` si absent) — repeuplent les champs du formulaire après filtrage."""
        return {key: request.query_params.get(f"min_{key}", "") for key in _THRESHOLD_SPECS}

    def min_values(raw: dict[str, str]) -> dict[str, float]:
        """Filtres « au moins X » combinables, sur toutes les colonnes triables
        — trouver les combos bons sur plusieurs axes à la fois, ce que le tri
        seul ne permet pas quand la 1re colonne triée est presque toujours
        unique (retour utilisateur, 2026-07-13)."""
        values: dict[str, float] = {}
        for key, (is_percent, ge, le) in _THRESHOLD_SPECS.items():
            parsed = _parse_optional_float(raw.get(key), ge=ge, le=le)
            if parsed is not None:
                values[key] = parsed / 100.0 if is_percent else parsed
        return values

    def filters_qs(thresholds_raw: dict[str, str], **base: object) -> str:
        """Querystring des filtres actifs (calculée côté Python, pas en Jinja
        `{% set %}` dans une boucle : la variable ne survivrait pas à la
        boucle) — réutilisée par la pagination et les liens de tri pour ne
        jamais perdre un filtre en changeant de page ou de tri."""
        params = dict(base)
        params.update({f"min_{k}": v for k, v in thresholds_raw.items()})
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
    _DUO_ROLES_PATTERN = f"^({'|'.join(queries.DUO_ROLES)})$"

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
        thresholds_raw = threshold_raw(request)
        thresholds = min_values(thresholds_raw)
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
                min_values=thresholds,
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
                "min_values": thresholds_raw,
                "filters_qs": filters_qs(
                    thresholds_raw,
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
        thresholds_raw = threshold_raw(request)
        thresholds = min_values(thresholds_raw)
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            result = queries.duo_tierlist(
                conn,
                window,
                platform,
                roles,
                champ_a_id=resolve_champion(champ_a),
                champ_b_id=resolve_champion(champ_b),
                min_games=min_games,
                min_tier=min_tier,
                min_values=thresholds,
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
                "champ_a_search": champ_a or "",
                "champ_b_search": champ_b or "",
                "min_games": min_games,
                "min_tier": min_tier,
                "min_values": thresholds_raw,
                "filters_qs": filters_qs(
                    thresholds_raw,
                    window=window,
                    platform=platform,
                    roles=roles,
                    champ_a=champ_a or "",
                    champ_b=champ_b or "",
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

    def _draft_role_suggestions(
        conn,
        window: str,
        platform: str,
        role: str,
        locked_allies: list[tuple[str, int]],
        locked_enemy: int | None,
        banned: set[int],
    ) -> list[dict]:
        """Candidats pour un rôle vide, triés par edge = Σ synergie alliés
        verrouillés + delta counter vs l'ennemi même rôle verrouillé — même
        unité partout (points de WR), donc sommable sans pondération
        arbitraire. `None` sans donnée pour une source, jamais pénalisé
        (juste pas de contribution) — affiché en bas plutôt qu'exclu.
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
                conn, window, platform, roles_pair, ally_role, ally_champ, 200, min_tier="faible"
            )
            _accumulate(partners, "synergy", "partner_champion")

        if locked_enemy is not None:
            enemy_matchups = queries.matchup_candidates(
                conn, window, platform, DRAFT_ROLE_TO_TEAM_POSITION[role], locked_enemy, 200
            )
            _accumulate(enemy_matchups, "delta", "candidate_champion")

        if not edge:
            # 1er pick du rôle : ni allié ni ennemi verrouillé, pas de
            # synergie/counter à calculer — repli sur le WR baseline.
            baseline = queries.champion_role_baseline_list(
                conn, window, platform, DRAFT_ROLE_TO_TEAM_POSITION[role], DRAFT_CANDIDATES_SHOWN
            )
            return [
                {
                    "champion_id": r["candidate_champion"],
                    "name": champ(r["candidate_champion"]).name,
                    "edge": None,
                    "wr": r["wr"],
                    "games_eff": r["games"],
                    "low_sample": False,
                }
                for r in baseline
                if r["candidate_champion"] not in banned
            ]

        ranked = sorted(edge.items(), key=lambda kv: kv[1], reverse=True)[:DRAFT_CANDIDATES_SHOWN]
        return [
            {
                "champion_id": cid,
                "name": champ(cid).name,
                "edge": value,
                "wr": None,
                "games_eff": reliability[cid],
                "low_sample": reliability[cid] < DRAFT_MIN_GAMES_EFF,
            }
            for cid, value in ranked
        ]

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

            suggestions: dict[str, list[dict]] = {}
            for side, other_side in (("blue", "red"), ("red", "blue")):
                locked_allies = [(r, c) for r, c in picks[side].items() if c is not None]
                for role in DRAFT_ROLES:
                    if picks[side][role] is not None:
                        continue
                    key = f"{side}_{role}"
                    role_suggestions = _draft_role_suggestions(
                        conn, window, platform, role, locked_allies, picks[other_side][role], banned
                    )
                    for s in role_suggestions:
                        s["pick_url"] = _draft_url(**{key: s["name"]})
                    suggestions[key] = role_suggestions

            slot_urls = {
                f"{side}_{role}": _draft_url(**{f"{side}_{role}": ""})
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
                "suggestions": suggestions,
                "slot_urls": slot_urls,
                "draft_roles": DRAFT_ROLES,
                "champion_names": sorted(c.name for c in champ_index().values()),
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
                min_values=min_values(threshold_raw(request)),
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
        with request.app.state.pool.connection() as conn:
            window, platform, _ = resolve_context(conn, window, platform)
            result = queries.duo_tierlist(
                conn,
                window,
                platform,
                roles,
                champ_a_id=resolve_champion(champ_a),
                champ_b_id=resolve_champion(champ_b),
                min_games=min_games,
                min_tier=min_tier,
                min_values=min_values(threshold_raw(request)),
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
