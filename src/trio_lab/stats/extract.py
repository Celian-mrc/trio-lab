"""Extraction des stats trio depuis (detail, timeline) — module pur, zéro I/O.

Produit les lignes des tables `match_trio_stats` (2 lignes, une par équipe) et
`match_objective_events` (events ordonnés). Consommé par le collector à
l'ingestion et par le backfill sur archives.

Mappings repris de l'adapter Riot de macro-lab (règles validées là-bas) :
- attribution d'équipe : `CHAMPION_KILL` → équipe du tueur (exécution → opposé
  de la victime) ; `BUILDING_KILL` → **opposé** de `teamId` (le propriétaire
  subit) ; `ELITE_MONSTER_KILL` → `killerTeamId` ;
- monstres : `HORDE`→VOID_GRUB, `RIFTHERALD`→RIFT_HERALD, `BARON_NASHOR`→BARON,
  `DRAGON` (+ sous-types internes), `ELDER_DRAGON` à part (Atakhan n'existe
  plus cette saison, non suivi) ;
- frames natives : frame d'indice m = snapshot à t = m×60000 ms.

⚠️ `DRAGON_SOUL_GIVEN` n'est PAS l'obtention de l'âme : c'est l'**annonce du
type d'âme** après le 2e drake (`teamId: 0`, constaté sur timelines 16.13).
L'âme se déduit du cumul de drakes non-elder (`SOUL_DRAKES`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trio_lab.collector.parsing import ParseError, winning_team_of

TEAMS = (100, 200)
TRIO_ROLES = ("JUNGLE", "MIDDLE", "UTILITY")
GOLD_MINUTES = (5, 10, 15, 20, 25, 30, 35)
MINUTE_MS = 60_000
# Fenêtre de la kill participation « early » (cf. PROJECT.md).
KP_WINDOW_MS = 15 * MINUTE_MS
# Drakes non-elder nécessaires à l'âme.
SOUL_DRAKES = 4

# `monsterSubType` Riot → sous-type interne (repris de macro-lab `_common.py`).
DRAGON_SUBTYPE_MAP: dict[str, str] = {
    "FIRE_DRAGON": "infernal",
    "AIR_DRAGON": "cloud",
    "EARTH_DRAGON": "mountain",
    "WATER_DRAGON": "ocean",
    "HEXTECH_DRAGON": "hextech",
    "CHEMTECH_DRAGON": "chemtech",
}

# `monsterType` Riot (hors DRAGON) → event_type interne.
MONSTER_MAP: dict[str, str] = {
    "BARON_NASHOR": "BARON",
    "RIFTHERALD": "RIFT_HERALD",
    "HORDE": "VOID_GRUB",
}


def team_of(participant_id: int) -> int:
    """participantId (1..10) → team_id (1..5 = 100, 6..10 = 200)."""
    return 100 if participant_id <= 5 else 200


def _opposite(team_id: int) -> int:
    return 200 if team_id == 100 else 100


@dataclass(frozen=True)
class TrioMembers:
    """Le trio jgl/mid/supp d'une équipe : participantIds et championIds alignés."""

    team_id: int
    pids: tuple[int, int, int]  # ordre : JUNGLE, MIDDLE, UTILITY
    champion_ids: tuple[int, int, int]


def trios_of(detail: dict[str, Any]) -> dict[int, TrioMembers]:
    """Identifie le trio de chaque équipe. Lève `ParseError` si un rôle manque."""
    by_team_role: dict[tuple[int, str], tuple[int, int]] = {}
    for p in detail["info"].get("participants", []):
        key = (p.get("teamId"), p.get("teamPosition"))
        if key in by_team_role:
            raise ParseError(f"rôle dupliqué : {key!r}")
        by_team_role[key] = (p.get("participantId"), p.get("championId"))

    trios: dict[int, TrioMembers] = {}
    for team in TEAMS:
        members = []
        for role in TRIO_ROLES:
            member = by_team_role.get((team, role))
            if member is None or member[0] is None or member[1] is None:
                raise ParseError(f"trio incomplet : rôle {role} manquant (équipe {team})")
            members.append(member)
        trios[team] = TrioMembers(
            team_id=team,
            pids=tuple(m[0] for m in members),
            champion_ids=tuple(m[1] for m in members),
        )
    return trios


# --- events d'objectifs (table match_objective_events) ---


def objective_events(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    """Events d'objectifs ordonnés : drakes (+ type), héraut, grubs,
    Nashor, elder, tours (+ emplacement), first blood.

    `seq` est 1-indexé, chronologique. `team_id` = équipe qui prend l'objectif.
    """
    rows: list[dict[str, Any]] = []
    for frame in timeline["info"]["frames"]:
        for e in frame["events"]:
            parsed = _parse_objective(e)
            if parsed is not None:
                rows.append(parsed)
    rows.sort(key=lambda r: r["ts_s"])
    for seq, row in enumerate(rows, start=1):
        row["seq"] = seq
    return rows


def _parse_objective(e: dict[str, Any]) -> dict[str, Any] | None:
    etype = e["type"]
    pos = e.get("position") or {}

    if etype == "ELITE_MONSTER_KILL":
        team = e.get("killerTeamId")
        if team not in TEAMS:
            return None  # attribution inconnue : on ne devine pas
        monster, sub = e.get("monsterType"), e.get("monsterSubType")
        if monster == "DRAGON":
            if sub == "ELDER_DRAGON":
                return _row(e, "ELDER_DRAGON", None, team, pos)
            return _row(e, "DRAGON", DRAGON_SUBTYPE_MAP.get(sub, sub), team, pos)
        internal = MONSTER_MAP.get(monster)
        return _row(e, internal, None, team, pos) if internal else None

    if etype == "BUILDING_KILL" and e.get("buildingType") == "TOWER_BUILDING":
        # `teamId` = propriétaire de la tour détruite → le preneur est l'opposé.
        return _row(e, "TOWER", e.get("towerType"), _opposite(e["teamId"]), pos)

    if etype == "CHAMPION_SPECIAL_KILL" and e.get("killType") == "KILL_FIRST_BLOOD":
        killer = e.get("killerId")
        if not killer:
            return None
        return _row(e, "FIRST_BLOOD", None, team_of(killer), pos)

    return None


def _row(
    e: dict[str, Any], event_type: str, subtype: str | None, team: int, pos: dict[str, Any]
) -> dict[str, Any]:
    return {
        "seq": None,  # rempli après tri chronologique
        "ts_s": e["timestamp"] // 1000,
        "event_type": event_type,
        "subtype": subtype,
        "team_id": team,
        "pos_x": pos.get("x"),
        "pos_y": pos.get("y"),
    }


# --- résumés par équipe ---


def team_objectives(events: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Colonnes objectifs de `match_trio_stats`, par équipe, depuis les events extraits."""
    stats = {
        team: {
            "grubs_taken": 0,
            "herald_taken": False,
            "drakes_taken": 0,
            "soul_taken": False,
            "nashor_first": False,
            "nashor_first_s": None,
            "first_tower": False,
            "towers_destroyed": 0,
        }
        for team in TEAMS
    }
    for e in events:  # déjà triés par ts_s
        team = stats[e["team_id"]]
        etype = e["event_type"]
        if etype == "VOID_GRUB":
            team["grubs_taken"] += 1
        elif etype == "RIFT_HERALD":
            team["herald_taken"] = True
        elif etype == "DRAGON":
            team["drakes_taken"] += 1
            if team["drakes_taken"] >= SOUL_DRAKES:
                team["soul_taken"] = True
        elif etype == "BARON" and not any(stats[t]["nashor_first"] for t in TEAMS):
            team["nashor_first"] = True
            team["nashor_first_s"] = e["ts_s"]
        elif etype == "TOWER":
            if not any(stats[t]["first_tower"] for t in TEAMS):
                team["first_tower"] = True
            team["towers_destroyed"] += 1
    return stats


def gold_diffs(
    timeline: dict[str, Any], trios: dict[int, TrioMembers]
) -> dict[int, dict[str, int | None]]:
    """Avantage gold du trio vs trio adverse à 5/10/…/35 min, par équipe.

    Frame d'indice m = snapshot à la minute m (frames natives Riot) ; `None`
    au-delà de la dernière frame complète (partie finie avant).
    """
    frames = timeline["info"]["frames"]
    last_minute = frames[-1]["timestamp"] // MINUTE_MS
    diffs = {team: {} for team in TEAMS}
    for minute in GOLD_MINUTES:
        col = f"gold_diff_{minute}"
        if minute > last_minute:
            diffs[100][col] = diffs[200][col] = None
            continue
        pf = frames[minute]["participantFrames"]
        gold = {team: sum(pf[str(pid)]["totalGold"] for pid in trios[team].pids) for team in TEAMS}
        diffs[100][col] = gold[100] - gold[200]
        diffs[200][col] = gold[200] - gold[100]
    return diffs


JUNGLE_CS_DIFF_MINUTE = 15


def jungle_cs_diff(
    timeline: dict[str, Any], trios: dict[int, TrioMembers]
) -> dict[int, int | None]:
    """Écart de CS jungle du jungler vs le jungler adverse, à la 15e minute.

    Lu depuis `jungleMinionsKilled` (frame native de la timeline, distinct du
    `minionsKilled` de lane) — capture la domination jungle early (invades,
    pathing) avant que les rotations macro et les objectifs de mi/fin de
    partie ne redistribuent le farm. `None` au-delà de la dernière frame
    complète (partie finie avant 15 min), même logique que `gold_diffs`.
    """
    frames = timeline["info"]["frames"]
    if frames[-1]["timestamp"] // MINUTE_MS < JUNGLE_CS_DIFF_MINUTE:
        return {team: None for team in TEAMS}
    pf = frames[JUNGLE_CS_DIFF_MINUTE]["participantFrames"]
    jungle_cs = {team: pf[str(trios[team].pids[0])]["jungleMinionsKilled"] for team in TEAMS}
    return {
        100: jungle_cs[100] - jungle_cs[200],
        200: jungle_cs[200] - jungle_cs[100],
    }


def combat_stats(
    detail: dict[str, Any],
    timeline: dict[str, Any],
    trios: dict[int, TrioMembers],
    cc_reliability: dict[int, float] | None = None,
) -> dict[int, dict[str, Any]]:
    """first blood trio, KP<15 du trio, part des dégâts, vision, CC, plaques équipe.

    KP<15 = kills de l'équipe avant 15:00 où un membre du trio est tueur ou
    assistant ÷ kills de l'équipe sur la même fenêtre (exécutions comprises au
    dénominateur) ; `None` si l'équipe n'a aucun kill avant 15:00.

    `cc_reliability` : `{champion_id: coefficient ∈ (0, 1]}` (cf. ccref.reliability)
    appliqué à `timeCCingOthers` avant sommation trio — certains champions
    (Nocturne confirmé) gonflent cette stat Riot sans immobiliser réellement
    plus que la moyenne. `None`/absent = 1.0, aucune correction.

    `jgl_cc_time_s`/`mid_cc_time_s`/`sup_cc_time_s` : la même valeur (déjà
    corrigée par `cc_reliability`) par membre, en plus du total `cc_time_s` —
    la donnée existe déjà par participant à cet instant, seule la ventilation
    par rôle manquait (migration 020).

    `jgl_dmg_per_gold`/`mid_dmg_per_gold`/`sup_dmg_per_gold` (migration 021) :
    dégâts aux champions ÷ gold gagné, par membre — signal d'efficacité
    indépendant de l'allocation de ressources (un champion qui rend beaucoup
    avec peu de gold). `None` si `goldEarned` vaut 0 (jamais observé en
    pratique, gold de départ compris, mais on ne divise pas par 0).

    `wards_placed`/`wards_killed` (migration 021) : décomposition du score de
    vision agrégé (`vision_score`), niveau trio comme lui.
    """
    trio_pids = {team: set(trios[team].pids) for team in TEAMS}
    stats: dict[int, dict[str, Any]] = {team: {} for team in TEAMS}
    # Ordre aligné sur TrioMembers.pids (JUNGLE, MIDDLE, UTILITY).
    role_cc_fields = ("jgl_cc_time_s", "mid_cc_time_s", "sup_cc_time_s")
    role_dmg_per_gold_fields = ("jgl_dmg_per_gold", "mid_dmg_per_gold", "sup_dmg_per_gold")

    # Kills early depuis la timeline.
    team_kills = dict.fromkeys(TEAMS, 0)
    trio_kills = dict.fromkeys(TEAMS, 0)
    for frame in timeline["info"]["frames"]:
        for e in frame["events"]:
            if e["type"] != "CHAMPION_KILL" or e["timestamp"] >= KP_WINDOW_MS:
                continue
            killer = e.get("killerId", 0)
            team = team_of(killer) if killer else _opposite(team_of(e["victimId"]))
            team_kills[team] += 1
            involved = {killer, *e.get("assistingParticipantIds", [])}
            if involved & trio_pids[team]:
                trio_kills[team] += 1
    for team in TEAMS:
        stats[team]["kill_participation_pre15"] = (
            trio_kills[team] / team_kills[team] if team_kills[team] else None
        )

    # Stats de fin de match depuis le detail.
    damage = {team: {"trio": 0, "team": 0} for team in TEAMS}
    for team in TEAMS:
        stats[team].update(
            first_blood_trio=False,
            vision_score=0,
            cc_time_s=0,
            plates_taken=0,
            wards_placed=0,
            wards_killed=0,
            jgl_cc_time_s=None,
            mid_cc_time_s=None,
            sup_cc_time_s=None,
            jgl_dmg_per_gold=None,
            mid_dmg_per_gold=None,
            sup_dmg_per_gold=None,
        )
    for p in detail["info"]["participants"]:
        team, pid = p["teamId"], p["participantId"]
        damage[team]["team"] += p.get("totalDamageDealtToChampions", 0)
        stats[team]["plates_taken"] += p.get("challenges", {}).get("turretPlatesTaken", 0)
        if pid in trio_pids[team]:
            member_damage = p.get("totalDamageDealtToChampions", 0)
            damage[team]["trio"] += member_damage
            stats[team]["vision_score"] += p.get("visionScore", 0)
            stats[team]["wards_placed"] += p.get("wardsPlaced", 0)
            stats[team]["wards_killed"] += p.get("wardsKilled", 0)
            reliability = (cc_reliability or {}).get(p.get("championId"), 1.0)
            cc = round(p.get("timeCCingOthers", 0) * reliability)
            stats[team]["cc_time_s"] += cc
            stats[team][role_cc_fields[trios[team].pids.index(pid)]] = cc
            gold = p.get("goldEarned", 0)
            dmg_per_gold = member_damage / gold if gold else None
            stats[team][role_dmg_per_gold_fields[trios[team].pids.index(pid)]] = dmg_per_gold
            if p.get("firstBloodKill") or p.get("firstBloodAssist"):
                stats[team]["first_blood_trio"] = True
    for team in TEAMS:
        stats[team]["damage_share"] = (
            damage[team]["trio"] / damage[team]["team"] if damage[team]["team"] else None
        )
    return stats


# --- assemblage ---


def extract_match(
    detail: dict[str, Any],
    timeline: dict[str, Any],
    cc_reliability: dict[int, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(2 lignes `match_trio_stats`, N lignes `match_objective_events`) d'un match.

    Lève `ParseError` si les données sont inexploitables (trio incomplet,
    timeline d'un autre match, gagnant indéterminé). `cc_reliability` : cf.
    `combat_stats`.
    """
    match_id = detail["metadata"]["matchId"]
    tl_match_id = timeline["metadata"]["matchId"]
    if match_id != tl_match_id:
        raise ParseError(f"timeline {tl_match_id!r} ≠ detail {match_id!r}")

    trios = trios_of(detail)
    winning_team = winning_team_of(detail)
    events = objective_events(timeline)
    objectives = team_objectives(events)
    gold = gold_diffs(timeline, trios)
    combat = combat_stats(detail, timeline, trios, cc_reliability)
    jgl_cs = jungle_cs_diff(timeline, trios)

    trio_rows = []
    for team in TEAMS:
        jgl, mid, sup = trios[team].champion_ids
        trio_rows.append(
            {
                "match_id": match_id,
                "team_id": team,
                "jgl_champion": jgl,
                "mid_champion": mid,
                "sup_champion": sup,
                "win": team == winning_team,
                **gold[team],
                **objectives[team],
                **combat[team],
                "jgl_cs_diff_15": jgl_cs[team],
            }
        )
    event_rows = [{"match_id": match_id, **e} for e in events]
    return trio_rows, event_rows
