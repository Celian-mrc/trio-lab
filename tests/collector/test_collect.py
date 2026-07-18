"""Tests de l'orchestrateur : pipeline, dédup, exclusion, idempotence, reprise.

Aucun appel réseau ni Postgres réel : le client Riot est un fake (comme dans
macro-lab) et la couche `storage` est remplacée par un store en mémoire aux
mêmes sémantiques (PK match_id, journal, curseur joueurs). Les sémantiques SQL
réelles sont couvertes par `test_storage_pg.py` (intégration, gated).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import pytest

from trio_lab.collector import collect, patches

from ..stats._builders import build_timeline
from ._builders import build_detail

PATCH = "16.98"
_BOUNDS = {PATCH: (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 15, tzinfo=UTC))}


class _FakeRate:
    remaining = 99


class _FakeClient:
    """2 PUUIDs apex, un match partagé (dédup cross-joueurs) et un match court (exclu)."""

    def __init__(self, *args, **kwargs):
        self.rate = _FakeRate()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def get_apex_league(self, tier, *, platform):
        if tier == "challenger":
            return {"entries": [{"puuid": "p1"}, {"puuid": "p2"}]}
        return {"entries": []}

    async def get_league_entries(self, tier, division, *, platform, page):
        return []  # pas de sub-apex dans ce fake : 2 joueurs suffisent

    async def get_match_ids_by_puuid(self, puuid, **kwargs):
        return [f"EUW1_{puuid}", "EUW1_shared"]  # EUW1_shared commun aux deux PUUIDs

    async def get_match(self, match_id, *, platform):
        if "shared" in match_id:
            return build_detail(match_id, patch=PATCH, duration_s=100)  # < 5 min → exclu
        return build_detail(match_id, patch=PATCH)

    async def get_match_timeline(self, match_id, *, platform):
        return build_timeline(match_id)  # extractible : le pipeline appelle extract_match

    # Pas de get_league_entries_by_puuid ici : la récolte (_harvest_participants)
    # est testée isolément plus bas (test_harvest_participants_*) pour ne pas
    # faire cascader la file de tous les autres tests (les PUUIDs récoltés
    # rejoindraient sinon la file et feraient boule de neige dans ce fake, où
    # get_match_ids_by_puuid répond pour n'importe quel PUUID). Ici, l'appel
    # échoue (AttributeError) et est avalé par le best-effort de collect.py —
    # comportement identique aux autres tests, pas de régression.


class _FailingClient(_FakeClient):
    """Un seul joueur, dont le détail de match échoue systématiquement (panne simulée)."""

    async def get_apex_league(self, tier, *, platform):
        if tier == "challenger":
            return {"entries": [{"puuid": "p1"}]}
        return {"entries": []}

    async def get_match_ids_by_puuid(self, puuid, **kwargs):
        return ["EUW1_flaky"]

    async def get_match(self, match_id, *, platform):
        raise RuntimeError("boom")


class _FakeConn:
    async def close(self):
        return None


class _FakeStore:
    """Store en mémoire reproduisant les sémantiques de `storage` (PK, journal, file)."""

    DEFAULT_MAX_ATTEMPTS = 3

    def __init__(self):
        self.players: dict[str, dict] = {}  # puuid → {platform, fetched}
        self.matches: dict[str, tuple[dict, list[dict]]] = {}
        self.trio_stats: dict[str, tuple[list, list]] = {}
        self.journal: dict[str, dict] = {}
        self.archived: list[str] = []

    def requeue_players(self):
        """Simule le recyclage de la file (joueurs redevenus les plus anciens)."""
        for entry in self.players.values():
            entry["fetched"] = False

    async def upsert_players(self, conn, rows):
        for row in rows:
            # Curseur préservé en cas de redécouverte (ON CONFLICT).
            self.players.setdefault(row.puuid, {"platform": row.platform, "fetched": False})
        return len(rows)

    async def next_player(self, conn, *, platform):
        return next(
            (
                puuid
                for puuid, entry in self.players.items()
                if entry["platform"] == platform and not entry["fetched"]
            ),
            None,
        )

    async def mark_player_fetched(self, conn, puuid):
        self.players[puuid]["fetched"] = True

    async def unknown_puuids(self, conn, puuids):
        return [p for p in puuids if p not in self.players]

    async def filter_new_match_ids(self, conn, match_ids):
        done = set(self.matches) | {
            m
            for m, entry in self.journal.items()
            if entry["status"] in ("excluded", "error_permanent")
        }
        return [m for m in match_ids if m not in done]

    async def journal_exclusion(self, conn, match_id, *, platform, reason):
        self.journal[match_id] = {"status": "excluded", "reason": reason, "attempts": 0}

    async def journal_failure(self, conn, match_id, *, platform, error, max_attempts=3):
        entry = self.journal.setdefault(
            match_id, {"status": "error_retryable", "reason": error, "attempts": 0}
        )
        entry["attempts"] += 1
        entry["status"] = (
            "error_permanent" if entry["attempts"] >= max_attempts else "error_retryable"
        )
        return entry["status"]

    async def insert_match(self, conn, row, participants, trio_stats=None, objective_events=None):
        if row["match_id"] in self.matches:
            return False
        self.matches[row["match_id"]] = (row, participants)
        self.trio_stats[row["match_id"]] = (trio_stats or [], objective_events or [])
        self.journal.pop(row["match_id"], None)
        return True

    def archive_timeline(self, data_dir, platform, patch, match_id, timeline):
        self.archived.append(match_id)
        return data_dir


@pytest.fixture
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(patches, "PATCH_DATES", dict(_BOUNDS))
    monkeypatch.setattr(collect, "RiotClient", _FakeClient)
    monkeypatch.setattr(collect, "storage", fake)
    monkeypatch.setattr(collect.db, "connect", _fake_connect)
    return fake


async def _fake_connect(dsn=None):
    return _FakeConn()


async def test_pipeline_dedup_exclude_and_store(store, tmp_path):
    counts = await collect.run(platforms=["euw1"], patch=PATCH, target=100, data_dir=tmp_path)

    # 3 ids uniques : EUW1_p1, EUW1_p2, EUW1_shared (dédup cross-joueurs) ;
    # EUW1_shared exclu (durée), les 2 autres téléchargés et archivés.
    assert counts["downloaded"] == 2
    assert counts["excluded"] == 1
    assert counts["players_scanned"] == 2
    assert set(store.matches) == {"EUW1_p1", "EUW1_p2"}
    assert store.journal["EUW1_shared"] == {
        "status": "excluded",
        "reason": "duration",
        "attempts": 0,
    }
    assert sorted(store.archived) == ["EUW1_p1", "EUW1_p2"]
    # Les 10 participants extraits accompagnent chaque match.
    _row, participants = store.matches["EUW1_p1"]
    assert len(participants) == 10
    # Les stats trio (Phase 2) sont extraites et transmises dans la même écriture.
    trio_rows, _events = store.trio_stats["EUW1_p1"]
    assert [r["team_id"] for r in trio_rows] == [100, 200]
    assert trio_rows[0]["jgl_champion"] == 2  # builder : JUNGLE équipe 100 = champion 2


async def test_cc_reliability_constant_is_applied_at_extraction(store, tmp_path, monkeypatch):
    """CC_TIME_RELIABILITY (constante, cf. ccref.reliability) atténue `cc_time_s`
    à l'ingestion (cf. extract.py) — plus de chargement DB depuis ce champion 5."""
    monkeypatch.setattr(collect, "CC_TIME_RELIABILITY", {2: 0.5})  # champ 2 = jgl équipe 100
    await collect.run(platforms=["euw1"], patch=PATCH, target=100, data_dir=tmp_path)

    trio_rows, _events = store.trio_stats["EUW1_p1"]
    # Sans correction : 2×(2+3+5) = 20 (cf. test_extract.py). Avec jgl (champ 2,
    # cc brut 4) atténué à 2 : 2 + 6 + 10 = 18.
    assert trio_rows[0]["cc_time_s"] == 18


async def test_archiving_can_be_disabled(store, tmp_path, monkeypatch):
    """ARCHIVE_TIMELINES=0 (Railway, fs éphémère) : ingestion sans JSON.gz."""
    monkeypatch.setattr(collect.config, "ARCHIVE_TIMELINES", False)
    counts = await collect.run(platforms=["euw1"], patch=PATCH, target=100, data_dir=tmp_path)
    assert counts["downloaded"] == 2  # l'ingestion Postgres est inchangée
    assert store.archived == []


async def test_second_run_downloads_nothing(store, tmp_path):
    await collect.run(platforms=["euw1"], patch=PATCH, target=100, data_dir=tmp_path)
    store.requeue_players()
    counts2 = await collect.run(platforms=["euw1"], patch=PATCH, target=100, data_dir=tmp_path)

    # Tout est déjà en base ou journalisé : aucun re-téléchargement.
    # (Un compteur jamais incrémenté est absent du dict retourné.)
    assert counts2.get("downloaded", 0) == 0
    assert counts2.get("excluded", 0) == 0
    assert len(store.matches) == 2


async def test_target_stops_collection(store, tmp_path):
    counts = await collect.run(platforms=["euw1"], patch=PATCH, target=1, data_dir=tmp_path)
    assert counts["downloaded"] == 1


async def test_unknown_patch_fails_fast(store, tmp_path):
    with pytest.raises(ValueError, match="9.99"):
        await collect.run(platforms=["euw1"], patch="9.99", target=1, data_dir=tmp_path)


async def test_failures_become_permanent_after_max_attempts(store, tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "RiotClient", _FailingClient)

    for attempt in range(1, 4):
        store.requeue_players()
        counts = await collect.run(
            platforms=["euw1"], patch=PATCH, target=100, max_attempts=3, data_dir=tmp_path
        )
        if attempt < 3:
            assert counts["errors"] == 1
            assert store.journal["EUW1_flaky"]["status"] == "error_retryable"

    assert store.journal["EUW1_flaky"]["status"] == "error_permanent"
    # Run suivant : l'échec permanent est filtré, plus aucune tentative.
    store.requeue_players()
    counts4 = await collect.run(platforms=["euw1"], patch=PATCH, target=100, data_dir=tmp_path)
    assert counts4.get("errors", 0) == 0
    assert not store.matches


class _FlakyFanoutClient(_FakeClient):
    """Le fan-out échoue une fois (429 résiduel simulé) puis fonctionne."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed_once = False

    async def get_match_ids_by_puuid(self, puuid, **kwargs):
        if not self.failed_once:
            self.failed_once = True
            raise RuntimeError("429, message='Too Many Requests'")
        return await super().get_match_ids_by_puuid(puuid, **kwargs)


async def test_loop_survives_transient_fanout_error(store, tmp_path, monkeypatch):
    """Un 429 ayant épuisé les retries du client ne tue pas la boucle 24/24."""
    monkeypatch.setattr(collect, "RiotClient", _FlakyFanoutClient)
    monkeypatch.setattr(collect, "RETRY_PAUSE_S", 0)
    counts = await collect.run(platforms=["euw1"], patch=PATCH, target=100, data_dir=tmp_path)

    assert counts["loop_errors"] == 1
    # Le joueur en échec n'a pas été marqué scanné : il a été retenté, tout est là.
    assert counts["downloaded"] == 2
    assert counts["players_scanned"] == 2


async def test_loop_reconnects_after_error(store, tmp_path, monkeypatch):
    """Une erreur de boucle reconnecte : pas de retentative indéfinie sur une
    connexion morte (vécu en prod : coupure Postgres pendant un resize,
    boucle d'échecs sans fin tant que la connexion n'était pas recréée)."""
    monkeypatch.setattr(collect, "RiotClient", _FlakyFanoutClient)
    monkeypatch.setattr(collect, "RETRY_PAUSE_S", 0)
    connect_calls = 0

    async def counting_connect(dsn=None):
        nonlocal connect_calls
        connect_calls += 1
        return _FakeConn()

    monkeypatch.setattr(collect.db, "connect", counting_connect)
    await collect.run(platforms=["euw1"], patch=PATCH, target=100, data_dir=tmp_path)

    # 1 connexion initiale + 1 reconnexion après l'unique erreur de boucle.
    assert connect_calls == 2


class _PerPlatformClient(_FakeClient):
    """Joueurs et matchs distincts par plateforme (insensible à l'ordonnancement)."""

    async def get_apex_league(self, tier, *, platform):
        if tier == "challenger":
            return {"entries": [{"puuid": f"{platform}-p1"}]}
        return {"entries": []}

    async def get_match_ids_by_puuid(self, puuid, **kwargs):
        return [f"{puuid.upper()}_M1"]

    async def get_match(self, match_id, *, platform):
        return build_detail(match_id, patch=PATCH)


async def test_multi_platform_counts_are_aggregated(store, tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "RiotClient", _PerPlatformClient)
    counts = await collect.run(
        platforms=["euw1", "na1"], patch=PATCH, target=100, data_dir=tmp_path
    )
    # Chaque boucle plateforme scanne son joueur et télécharge son match ;
    # les compteurs remontés par `run` sont l'agrégat des deux.
    assert counts["players_scanned"] == 2
    assert counts["downloaded"] == 2
    assert set(store.matches) == {"EUW1-P1_M1", "NA1-P1_M1"}


class _HarvestClient:
    """Fake dédié à `_harvest_participants` : rang par PUUID programmable."""

    async def get_league_entries_by_puuid(self, puuid, *, platform):
        # "puuid-1" sous Emerald (hors scope) ; le reste au tier demandé.
        if puuid == "puuid-1":
            return [{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II"}]
        return [{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD", "rank": "III"}]


async def test_harvest_participants_adds_only_eligible_and_unknown(monkeypatch):
    """Récolte isolée (pas via `collect.run`) : évite que les PUUIDs récoltés
    rejoignent la file et fassent boule de neige dans le fake partagé."""
    fake_store = _FakeStore()
    fake_store.players["puuid-5"] = {"platform": "euw1", "fetched": True}  # déjà connu
    monkeypatch.setattr(collect, "storage", fake_store)
    detail = build_detail("EUW1_1", patch=PATCH)
    counts: Counter[str] = Counter()

    await collect._harvest_participants(
        _HarvestClient(), None, detail, platform="euw1", counts=counts
    )

    # 10 participants ("puuid-1".."puuid-10") : "puuid-1" écarté (GOLD),
    # "puuid-5" déjà connu (pas revérifié), les 8 autres ajoutés.
    assert counts["harvested"] == 8
    assert "puuid-1" not in fake_store.players
    expected = {"puuid-5", *(f"puuid-{i}" for i in (2, 3, 4, 6, 7, 8, 9, 10))}
    assert set(fake_store.players) == expected


async def test_harvest_participants_survives_a_single_puuid_failure(monkeypatch):
    """Un PUUID en échec (compte supprimé, timeout) n'empêche pas les autres."""

    class _FlakyOnPuuid3(_HarvestClient):
        async def get_league_entries_by_puuid(self, puuid, *, platform):
            if puuid == "puuid-3":
                raise RuntimeError("compte introuvable")
            return await super().get_league_entries_by_puuid(puuid, platform=platform)

    fake_store = _FakeStore()
    monkeypatch.setattr(collect, "storage", fake_store)
    detail = build_detail("EUW1_1", patch=PATCH)
    counts: Counter[str] = Counter()

    await collect._harvest_participants(
        _FlakyOnPuuid3(), None, detail, platform="euw1", counts=counts
    )

    assert "puuid-3" not in fake_store.players
    assert "puuid-4" in fake_store.players  # les autres candidats ont bien été traités
