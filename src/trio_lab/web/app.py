"""Application FastAPI : pages Jinja2 (htmx en hx-boost) + API JSON de lecture.

Les routes sont des `def` synchrones (threadpool FastAPI) sur un pool psycopg
sync — pas d'event loop psycopg, donc pas de piège Windows. `create_app` prend
un DSN et un index champion injectables : les tests passent la base de test et
un index fixe (aucun appel Data Dragon).

    python -m trio_lab.web          # sert sur $PORT (défaut 8000)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool

from trio_lab import db
from trio_lab.synergy.compute import DUO_ROLES
from trio_lab.synergy.windows import make_window
from trio_lab.web import champions, queries, summary

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent

ROLE_LABELS = {"jgl": "Jungle", "mid": "Mid", "sup": "Support"}
ROLE_TO_TEAM_POSITION = {"jgl": "JUNGLE", "mid": "MIDDLE", "sup": "UTILITY"}
COUNTERS_SHOWN = 10  # pires et meilleurs matchups affichés sur la page détail
ALLIES_SHOWN = 10  # meilleurs alliés Top/ADC affichés sur la page détail
DUO_BEST_TRIOS_SHOWN = 10  # meilleurs 3e membres affichés sur la page détail duo
CHAMPION_PARTNERS_SHOWN = 5  # meilleurs partenaires par rôle affichés sur la page champion
CHAMPION_TRIOS_SHOWN = 10  # meilleurs trios affichés sur la page champion


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
    templates.env.filters.update(
        pct=_fmt_pct,
        pct100=_fmt_pct100,
        signed_pct=_fmt_signed_pct,
        signed_int=_fmt_signed_int,
        num=_fmt_num,
        duration=_fmt_duration,
        since=_fmt_since,
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
    _ROLE_PATTERN = "^(jgl|mid|sup)?$"
    _TRIO_SORT_PATTERN = f"^({'|'.join(queries.TRIO_SORTS)})$"
    _DUO_SORT_PATTERN = f"^({'|'.join(queries.DUO_SORTS)})$"
    _DIR_PATTERN = f"^({'|'.join(queries.SORT_DIRECTIONS)})$"
    _DUO_ROLES_PATTERN = f"^({'|'.join(queries.DUO_ROLES)})$"

    @app.get("/", response_class=HTMLResponse)
    def tierlist_page(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        champion: str | None = None,
        role: str | None = Query(None, pattern=_ROLE_PATTERN),
        min_games: int = Query(0, ge=0),
        min_tier: str = Query("faible", pattern="^(faible|moyen|eleve)$"),
        sort: str = Query("synergy", pattern=_TRIO_SORT_PATTERN),
        direction: str = Query("desc", pattern=_DIR_PATTERN, alias="dir"),
        page: int = Query(1, ge=1),
    ):
        role = role or None
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            champion_id = resolve_champion(champion)
            result = queries.trio_tierlist(
                conn,
                window,
                platform,
                champion_id=champion_id,
                role=role,
                min_games=min_games,
                min_tier=min_tier,
                sort=sort,
                direction=direction,
                page=page,
            )
        return templates.TemplateResponse(
            request,
            "tierlist.html",
            {
                **context,
                **result,
                "champion": champion or "",
                "role": role or "",
                "min_games": min_games,
                "min_tier": min_tier,
                "sort": sort,
                "direction": direction,
                "champion_names": sorted(c.name for c in champ_index().values()),
            },
        )

    @app.get("/duos", response_class=HTMLResponse)
    def duos_page(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        roles: str = Query("jgl_mid", pattern=_DUO_ROLES_PATTERN),
        min_games: int = Query(0, ge=0),
        min_tier: str = Query("faible", pattern="^(faible|moyen|eleve)$"),
        sort: str = Query("synergy", pattern=_DUO_SORT_PATTERN),
        direction: str = Query("desc", pattern=_DIR_PATTERN, alias="dir"),
        page: int = Query(1, ge=1),
    ):
        with request.app.state.pool.connection() as conn:
            window, platform, context = resolve_context(conn, window, platform)
            result = queries.duo_tierlist(
                conn,
                window,
                platform,
                roles,
                min_games=min_games,
                min_tier=min_tier,
                sort=sort,
                direction=direction,
                page=page,
            )
        return templates.TemplateResponse(
            request,
            "duos.html",
            {
                **context,
                **result,
                "roles": roles,
                "min_games": min_games,
                "min_tier": min_tier,
                "sort": sort,
                "direction": direction,
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
        counters = queries.trio_counters(conn, window, platform, jgl, mid, sup)
        allies = queries.trio_allies(conn, window, platform, jgl, mid, sup, ALLIES_SHOWN)
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
            "counters_worst": counters[:COUNTERS_SHOWN],
            "counters_best": counters[::-1][:COUNTERS_SHOWN],
            "allies_best": allies,
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
        rows = queries.duo_match_rows(
            conn,
            list(patch_window.patches),
            None if platform == "all" else platform,  # 'all' = toutes régions
            roles,
            champ_a,
            champ_b,
        )
        stats = summary.summarize(rows, weights)
        cc_scores = queries.cc_theoretical_scores(conn)
        a_cc, b_cc = cc_scores.get(champ_a), cc_scores.get(champ_b)
        members_cc = (a_cc, b_cc)
        # Total seulement si les 2 membres sont résolus (sinon somme partielle
        # trompeuse — affichée comme « — » à la place).
        duo_cc_raw = sum(members_cc) if None not in members_cc else None
        role_a, role_b = DUO_ROLES[roles]
        member_wr = {
            "a": queries.member_wr(
                conn, list(patch_window.patches), platform, role_a, champ_a, weights
            ),
            "b": queries.member_wr(
                conn, list(patch_window.patches), platform, role_b, champ_b, weights
            ),
        }
        return {
            "score": score,
            "stats": stats,
            "member_wr": member_wr,
            "best_trios": queries.duo_best_trios(
                conn, window, platform, roles, champ_a, champ_b, DUO_BEST_TRIOS_SHOWN
            ),
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
            champion_id=champion_id,
            role=role,
            min_tier="moyen",  # écarte les trios à 1-2 games (retour utilisateur, 2026-07-12)
            sort="synergy",
            direction="desc",
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
            "enemy_champion",
            "ally_champion",
        ):
            if key in out:
                out[key + "_name"] = champ(out[key]).name
        return out

    @app.get("/api/status")
    def api_status(request: Request):
        with request.app.state.pool.connection() as conn:
            return queries.collection_status(conn)

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
        champion: str | None = None,
        role: str | None = Query(None, pattern=_ROLE_PATTERN),
        min_games: int = Query(0, ge=0),
        min_tier: str = Query("faible", pattern="^(faible|moyen|eleve)$"),
        sort: str = Query("synergy", pattern=_TRIO_SORT_PATTERN),
        direction: str = Query("desc", pattern=_DIR_PATTERN, alias="dir"),
        page: int = Query(1, ge=1),
    ):
        with request.app.state.pool.connection() as conn:
            window, platform, _ = resolve_context(conn, window, platform)
            result = queries.trio_tierlist(
                conn,
                window,
                platform,
                champion_id=resolve_champion(champion),
                role=role or None,
                min_games=min_games,
                min_tier=min_tier,
                sort=sort,
                direction=direction,
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
        detail["counters_worst"] = [_named(r) for r in detail["counters_worst"]]
        detail["counters_best"] = [_named(r) for r in detail["counters_best"]]
        detail["allies_best"] = [_named(r) for r in detail["allies_best"]]
        return {"window": window, "platform": platform, **detail}

    @app.get("/api/duos")
    def api_duos(
        request: Request,
        window: str | None = None,
        platform: str | None = None,
        roles: str = Query("jgl_mid", pattern=_DUO_ROLES_PATTERN),
        min_games: int = Query(0, ge=0),
        min_tier: str = Query("faible", pattern="^(faible|moyen|eleve)$"),
        sort: str = Query("synergy", pattern=_DUO_SORT_PATTERN),
        direction: str = Query("desc", pattern=_DIR_PATTERN, alias="dir"),
        page: int = Query(1, ge=1),
    ):
        with request.app.state.pool.connection() as conn:
            window, platform, _ = resolve_context(conn, window, platform)
            result = queries.duo_tierlist(
                conn,
                window,
                platform,
                roles,
                min_games=min_games,
                min_tier=min_tier,
                sort=sort,
                direction=direction,
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
