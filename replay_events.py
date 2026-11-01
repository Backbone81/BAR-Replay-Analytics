"""Report on the events a replay playback logged: build order, economy, builders and fights, per team.

    python3 -I replay_events.py <replay>_events.json                # windows of 5 min
    python3 -I replay_events.py <replay>_events.json --step 2       # 2 min windows
    python3 -I replay_events.py <replay> --build-order-minutes 8    # build order of the first 8 min

play_replay.py plays a replay back with the analytics widget, which writes events/<replay>_events.json next to this
script; the events file is looked up there. The teams are named from the replay's start script as in sdfz_stats.py,
so the replay is looked up in the data dir's demos/ like play_replay.py does. The report has a header, then per
team: build order of the first minutes, milestones (factories, first T2 unit, mex counts held, resurrections), a
timeline of alive buildings and units with army value, income and idle builders, what each builder did, kills and
losses, where units died on a grid over the map, and what was built, resurrected, killed and lost. Timeline, kills,
losses and where units died are in windows of --step minutes, the last one up to the end of the game. The event format
is described in README.md ("Analytics widget").
"""

import argparse
import bisect
import collections
import dataclasses
import importlib.util
import json
import math
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
EVENTS_DIR = HERE / "events"  # where the analytics widget in this folder writes
EVENTS_SUFFIX = "_events.json"

GAME_SPEED = 30  # frames per second of game time (GAME_SPEED in the engine, Game.gameSpeed in Lua)

# Engine command IDs (rts/Sim/Units/CommandAI/Command.h, CMD in Lua) a builder works on other than building a unit.
CMD_NAMES = {40: "repair", 90: "reclaim", 110: "restore", 125: "resurrect", 130: "capture"}

# A player who quits or resigns loses all units within seconds, with no attacker.
QUIT_MIN_UNITS = 20
QUIT_WINDOW_FRAMES = 60 * GAME_SPEED

# Output is one "label: value" per line, and right-aligned tables with the time windows first, as in sdfz_stats.py.
FIELD_WIDTH = len("length: ")
TIME_WIDTH = 11  # "10:00-15:00"
NAME_WIDTH = 28  # unit names in the build order
LINE_WIDTH = 120  # lists wrap at this width, between their items

# Timeline columns: (role, column, width).
ROLE_COLUMNS = [
    ("mex", "mex", 4),
    ("energy", "energy", 6),
    ("factory", "fac", 4),
    ("nano", "nano", 4),
    ("constructor", "con", 4),
    ("defence", "def", 4),
    ("army", "army", 4),
]
MILESTONE_MEX_COUNTS = (4, 8, 12, 16, 20)
# Where units died: a grid of GRID_SIZE x GRID_SIZE cells over the map; on a 1v1 map a cell is about a base.
GRID_SIZE = 4
GRID_COLUMNS = "ABCDEFGH"[:GRID_SIZE]  # west to east; rows are numbered north to south
GRID_CELL_WIDTH = 9

Event = dict[str, Any]
UnitDef = dict[str, Any]  # a "unitDef" event
Team = dict[str, Any]  # an entry of the "init" event's teams


def import_sibling(name: str) -> ModuleType:
    """Import a module next to this script; python3 -I leaves the script's folder off sys.path."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {name}.py from {HERE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


play_replay = import_sibling("play_replay")
sdfz_stats = play_replay.sdfz_stats


@dataclass
class Game:
    """The events file: unit types and teams from its first events, and all other events in order."""

    unit_defs: dict[str, UnitDef]  # UnitDef name -> its "unitDef" event
    teams: dict[int, Team]  # teamID -> team
    map_name: str
    map_size: tuple[int, int] | None  # (mapSizeX, mapSizeZ) in elmos; None in events files from before 2026-10-27
    game_type: str
    events: list[Event]
    event_count: int  # events in the events file, as play_replay.py counts them
    end_frame: int  # of the last event, or of the first quit after drop_quit_losses
    team_labels: dict[int, dict[str, str]]  # teamID -> sdfz_stats.team_labels of the replay; empty without it
    by_type: dict[str, list[Event]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.by_type = collections.defaultdict(list)
        for event in self.events:
            self.by_type[event["event"]].append(event)

    def of_type(self, event_type: str) -> list[Event]:
        """Return the events of one type, in order."""
        return self.by_type.get(event_type, [])

    def unit_def(self, def_name: str | None) -> UnitDef:
        """Return the "unitDef" event of a UnitDef name, empty if unknown."""
        return self.unit_defs.get(def_name, {}) if def_name else {}

    def human_name(self, def_name: str | None) -> str:
        return self.unit_def(def_name).get("humanName", def_name or "?")

    def team_title(self, team_id: int) -> str:
        """One line naming the team, as in sdfz_stats.py; the side is the lobby's ("random" stays "random")."""
        if team_id in self.team_labels:
            return sdfz_stats.team_title(team_id, self.team_labels[team_id])
        team = self.teams.get(team_id, {})
        return (
            f"team {team_id} (allyteam {team.get('allyTeamID', '?')}): {team.get('name', '?')},"
            f" lobby side {team.get('side', '?')}"
        )


class Windows:
    """Windows of step_minutes from the game start; the last one ends at end_frame and also takes later events."""

    def __init__(self, step_minutes: float, end_frame: int) -> None:
        step = max(1, round(step_minutes * 60 * GAME_SPEED))
        end_frame = max(end_frame, 1)
        self.starts = list(range(0, end_frame, step))
        self.bounds = list(pairwise([*self.starts, end_frame]))  # (first frame, end frame)

    def __len__(self) -> int:
        return len(self.bounds)

    def index(self, frame: int) -> int:
        """Return the index of the window frame falls into."""
        return min(max(bisect.bisect_right(self.starts, frame) - 1, 0), len(self.bounds) - 1)

    def label(self, index: int) -> str:
        start, end = self.bounds[index]
        return f"{mmss(start)}-{mmss(end)}"


# Reading the events file


def find_events(name: str) -> Path:
    """Return the events file name ("_events.json" optional) in EVENTS_DIR."""
    events_file = EVENTS_DIR / (name if name.endswith(EVENTS_SUFFIX) else f"{name}{EVENTS_SUFFIX}")
    if not events_file.is_file():
        sys.exit(f"no events file {events_file.name} in {EVENTS_DIR}")
    return events_file


def read_team_labels(data_dir: Path, events_file: Path) -> tuple[Path | None, dict[int, dict[str, str]]]:
    """Return the replay the events file is named after and its teams' labels, or (None, {}) if it is not there.

    The events only have the names the engine tells a widget: for an AI not run by the local player, which in a
    playback is every AI, Spring.GetAIInfo gives no short name ("UNKNOWN") and no options, so no profile.
    """
    try:
        replay_file = play_replay.find_replay(data_dir, events_file.name.removesuffix(EVENTS_SUFFIX))
        replay = sdfz_stats.load(replay_file)
    except (play_replay.PlaybackError, OSError, ValueError):
        return None, {}
    return replay_file, sdfz_stats.team_labels(sdfz_stats.parse_script(replay["script"]))


def flatten(event: dict[str, Any]) -> Event:
    """Return an event of the events file, {"frame": ..., "<name>": {...}}, as {"event": name, "frame": ..., ...}."""
    [(name, fields)] = [(key, value) for key, value in event.items() if key != "frame"]
    return {"event": name, "frame": event["frame"], **fields}


def load(path: Path, team_labels: dict[int, dict[str, str]]) -> Game:
    """Read the events file the analytics widget wrote: {"events": [...], "end": {...}}.

    Exits if the file is incomplete (no "end": the engine crashed or was stopped). The end becomes an "end" event.
    """
    with path.open(encoding="utf-8") as file:
        try:
            document = json.load(file)
        except json.JSONDecodeError as error:
            sys.exit(f"{path.name} is incomplete or invalid: {error}")
    if "end" not in document:
        sys.exit(f"{path.name} is incomplete: it has no end")
    unit_defs, init, events = {}, {}, []
    for event in [*map(flatten, document["events"]), {"event": "end", **document["end"]}]:
        if event["event"] == "unitDef":
            unit_defs[event["name"]] = event
        elif event["event"] == "init":
            init = event
        else:
            events.append(event)
    teams = {team["teamID"]: team for team in init.get("teams", [])}
    end_frame = max((event["frame"] for event in events), default=0)
    event_count = len(document["events"])
    map_name, game_type = init.get("map", "?"), init.get("game", "?")
    map_size = (init["mapSizeX"], init["mapSizeZ"]) if "mapSizeX" in init and "mapSizeZ" in init else None
    return Game(unit_defs, teams, map_name, map_size, game_type, events, event_count, end_frame, team_labels)


def find_quits(game: Game) -> dict[int, tuple[int, int]]:
    """Return {teamID: (frame, units)} of teams that quit or resigned: a burst of deaths without attacker at the end."""
    quits = {}
    for team_id in game.teams:
        burst = [
            event["frame"]
            for event in game.of_type("unitDestroyed")
            if event["teamID"] == team_id
            and event.get("attackerTeamID") is None
            and event["frame"] > game.end_frame - QUIT_WINDOW_FRAMES
        ]
        if len(burst) >= QUIT_MIN_UNITS:
            quits[team_id] = (min(burst), len(burst))
    return quits


def drop_quit_losses(game: Game, quits: dict[int, tuple[int, int]]) -> Game:
    """Return the game without the quit bursts' deaths, ending at the first quit."""
    if not quits:
        return game
    quit_frames = {team_id: frame for team_id, (frame, _) in quits.items()}
    events = [
        event
        for event in game.events
        if not (
            event["event"] == "unitDestroyed"
            and event.get("attackerTeamID") is None
            and event["frame"] >= quit_frames.get(event["teamID"], math.inf)
        )
    ]
    return dataclasses.replace(game, events=events, end_frame=min(quit_frames.values()))


# Units and formatting


def mmss(frame: float) -> str:
    """Game time of a frame as minutes:seconds."""
    minutes, seconds = divmod(int(frame // GAME_SPEED), 60)
    return f"{minutes}:{seconds:02d}"


def role(unit_def: UnitDef) -> str:
    """Return the role of a unit type: commander, constructor, nano (construction turret), factory, mex, energy,
    defence (static, with a weapon), army (mobile, with a weapon) or other."""
    is_mobile = unit_def.get("isMobile")
    if unit_def.get("isFactory"):
        return "factory"
    if unit_def.get("extractsMetal"):
        return "mex"
    if unit_def.get("isBuilder"):
        if unit_def.get("isCommander"):
            return "commander"
        return "constructor" if is_mobile else "nano"
    if not is_mobile and (
        unit_def.get("unitGroup") == "energy" or unit_def.get("energyMake") or unit_def.get("windGenerator")
    ):
        return "energy"
    if unit_def.get("maxWeaponRange"):
        return "army" if is_mobile else "defence"
    return "other"


def metal_value(game: Game, events: list[Event]) -> float:
    """Metal cost of the events' units, counting partly built ones by their build progress."""
    return sum(
        game.unit_def(event["unitDef"]).get("metalCost", 0) * (event.get("buildProgress") or 1) for event in events
    )


def unit_counts(game: Game, counts: Counter[str]) -> list[str]:
    """{UnitDef name: count} -> ["6 Grunt", "4 Thug"], the most common first."""
    return [f"{count} {game.human_name(name)}" for name, count in counts.most_common()]


def runs(items: list[tuple[int, str]]) -> list[str]:
    """[(frame, label)] -> ["0:09  2x A", "0:29  B"], with consecutive equal labels merged."""
    merged: list[list] = []
    for frame, label in items:
        if merged and merged[-1][1] == label:
            merged[-1][2] += 1
        else:
            merged.append([frame, label, 1])
    return [f"{mmss(frame):>5}  {f'{count}x ' if count > 1 else ''}{label}" for frame, label, count in merged]


def percent(part: float, whole: float) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def unit_minutes(frames: float) -> str:
    return f"{frames / (60 * GAME_SPEED):.0f}"


def print_field(label: str, value: object) -> None:
    """Print one "label: value" line, the values aligned."""
    print(f"{label + ':':<{FIELD_WIDTH}}{value}")


def print_list(prefix: str, items: list[str]) -> None:
    """Print prefix and the items separated by commas ("-" if none), wrapped between items, aligned under the first."""
    if not items:
        print(f"{prefix}-")
        return
    lines = [[items[0]]]
    for item in items[1:]:
        # Each line but the last ends with ", " cut to ",": room for the item and that comma.
        if len(prefix) + len(", ".join([*lines[-1], item])) + 1 > LINE_WIDTH:
            lines.append([item])
        else:
            lines[-1].append(item)
    indent = " " * len(prefix)
    print(prefix + f",\n{indent}".join(", ".join(line) for line in lines))


def print_heading(text: str) -> None:
    """Print a section's heading, after a blank line."""
    print(f"\n{text}")


def print_row(label: str, cells: list[object], widths: list[int]) -> None:
    """Print one table row: the label in the time column, then the cells, all right-aligned."""
    print(f"{label:>{TIME_WIDTH}} " + " ".join(f"{cell!s:>{width}}" for cell, width in zip(cells, widths, strict=True)))


# Header


def print_header(game: Game, replay_file: Path | None, length_frame: int, quits: dict[int, tuple[int, int]]) -> None:
    print_field("replay", replay_file or "not found in the data dir's demos/; team names from the events file")
    print_field("map", game.map_name)
    print_field("game", game.game_type)
    print_field("length", mmss(length_frame))
    print_field("events", game.event_count)
    print("teams:")
    for team_id in game.teams:
        print(f"  {game.team_title(team_id)}")
    for team_id, (frame, units) in quits.items():
        print_field("quit", f"team {team_id} at {mmss(frame)}, {units} units removed without attacker (not losses)")


# Build order and milestones


def resurrected_ids(game: Game) -> set[int]:
    """unitIDs of resurrected units: created finished, in the same frame, by a unit that can resurrect.

    The engine creates a resurrected unit already built (CBuilder::UpdateResurrect); BAR's units that can resurrect
    (Lazarus, Graverobber ...) have no build options, but the frame check also keeps out a build of one that does.
    """
    finished_frames = {event["unitID"]: event["frame"] for event in game.of_type("unitFinished")}
    return {
        event["unitID"]
        for event in game.of_type("unitCreated")
        if game.unit_def(event.get("builderDef")).get("canResurrect")
        and finished_frames.get(event["unitID"]) == event["frame"]
    }


def finished_units(game: Game, team_id: int) -> list[Event]:
    """The team's "unitFinished" events of units it built: without the start units and resurrected ones."""
    resurrected = resurrected_ids(game)
    return [
        event
        for event in game.of_type("unitFinished")
        if event["teamID"] == team_id and event["frame"] > 0 and event["unitID"] not in resurrected
    ]


def resurrected_units(game: Game, team_id: int) -> list[Event]:
    """The "unitCreated" events of the units the team resurrected."""
    resurrected = resurrected_ids(game)
    return [
        event for event in game.of_type("unitCreated") if event["teamID"] == team_id and event["unitID"] in resurrected
    ]


def print_build_order(game: Game, team_id: int, minutes: float) -> None:
    """The units the team finished in the first minutes, with what built them."""
    created = {event["unitID"]: event for event in game.of_type("unitCreated")}
    end_frame = minutes * 60 * GAME_SPEED
    print_heading(f"build order (units finished up to {mmss(end_frame)}):")
    print(f"{'finished':>{TIME_WIDTH}}  {'unit':<{NAME_WIDTH}} built by")
    for event in finished_units(game, team_id):
        if event["frame"] > end_frame:
            break
        builder_def = created.get(event["unitID"], {}).get("builderDef")
        builder = game.human_name(builder_def) if builder_def else "-"
        print(f"{mmss(event['frame']):>{TIME_WIDTH}}  {game.human_name(event['unitDef']):<{NAME_WIDTH}} {builder}")


def mex_count_frames(game: Game, team_id: int) -> dict[int, int]:
    """Return {count: frame} of when the team first held each of MILESTONE_MEX_COUNTS finished mexes at once."""
    mexes, frames = set(), {}
    for event in game.events:
        if event["event"] in ("unitDestroyed", "unitGiven"):
            mexes.discard(event["unitID"])
        if (
            event["event"] in ("unitFinished", "unitGiven")
            and event["teamID"] == team_id
            and role(game.unit_def(event["unitDef"])) == "mex"
        ):
            mexes.add(event["unitID"])
            for count in MILESTONE_MEX_COUNTS:
                if len(mexes) >= count:
                    frames.setdefault(count, event["frame"])
    return frames


def print_milestones(game: Game, team_id: int) -> None:
    """Each factory type's first one, the first T2+ mobile unit, mex counts held and the first resurrected unit."""
    created = {event["unitID"]: event for event in game.of_type("unitCreated")}
    finished = finished_units(game, team_id)
    milestones = []  # (frame, text)
    factory_defs = set()
    for event in finished:
        unit_def = game.unit_def(event["unitDef"])
        if unit_def.get("isFactory") and event["unitDef"] not in factory_defs:
            factory_defs.add(event["unitDef"])
            started = created.get(event["unitID"], {}).get("frame", event["frame"])
            tech_level = unit_def.get("techLevel", 1)
            milestones.append(
                (event["frame"], f"factory {unit_def['humanName']} (T{tech_level}, started {mmss(started)})")
            )
    first_tech2 = next(
        (
            event
            for event in finished
            if game.unit_def(event["unitDef"]).get("techLevel", 1) >= 2
            and game.unit_def(event["unitDef"]).get("isMobile")
        ),
        None,
    )
    if first_tech2:
        milestones.append((first_tech2["frame"], f"first T2+ mobile unit {game.human_name(first_tech2['unitDef'])}"))
    milestones += [(frame, f"{count} mexes held") for count, frame in mex_count_frames(game, team_id).items()]
    resurrected = resurrected_units(game, team_id)
    if resurrected:
        counts = ", ".join(unit_counts(game, Counter(event["unitDef"] for event in resurrected)))
        text = f"first of {len(resurrected)} units resurrected ({metal_value(game, resurrected):.0f} metal): {counts}"
        milestones.append((resurrected[0]["frame"], text))

    print_heading("milestones:")
    for frame, text in sorted(milestones):
        print(f"{mmss(frame):>{TIME_WIDTH}}  {text}")
    if not first_tech2:
        print(f"{'-':>{TIME_WIDTH}}  first T2+ mobile unit: none")
    if not resurrected:
        print(f"{'-':>{TIME_WIDTH}}  resurrected units: none")


# Timeline


def alive_at_window_ends(game: Game, windows: Windows) -> dict[int, list[Counter[str]]]:
    """Return {teamID: [{UnitDef name: finished units alive} at the end of each window]}."""
    alive: dict[int, dict[int, str]] = {team_id: {} for team_id in game.teams}  # unitID -> UnitDef name
    snapshots: dict[int, list[Counter[str]]] = {team_id: [] for team_id in game.teams}

    def take_snapshot() -> None:
        for team_id, units in alive.items():
            snapshots[team_id].append(Counter(units.values()))

    taken = 0  # snapshots so far, each before the first event at or after its window's end
    for event in game.events:
        while taken < len(windows) and event["frame"] >= windows.bounds[taken][1]:
            take_snapshot()
            taken += 1
        if event["event"] == "unitFinished" and event["teamID"] in alive:
            alive[event["teamID"]][event["unitID"]] = event["unitDef"]
        elif event["event"] in ("unitDestroyed", "unitGiven"):
            for units in alive.values():
                units.pop(event["unitID"], None)
            if event["event"] == "unitGiven" and event["teamID"] in alive:
                alive[event["teamID"]][event["unitID"]] = event["unitDef"]
    for _ in range(taken, len(windows)):
        take_snapshot()
    return snapshots


def print_timeline(game: Game, team_id: int, windows: Windows, alive: list[Counter[str]]) -> None:
    """Finished units alive by role and army metal at each window's end, and resources averaged over the window.

    The resources are the widget's "resources" events (every 5 s): metal and energy income, idle builders (commanders
    and constructors without orders) and factories, and how often energy stalled: the storage nearly empty while the
    pull (what the team wants to spend) exceeds the income.
    """
    resources: list[list[Event]] = [[] for _ in range(len(windows))]
    for event in game.of_type("resources"):
        if event["teamID"] == team_id:
            resources[windows.index(event["frame"])].append(event)

    columns = [column for _, column, _ in ROLE_COLUMNS]
    columns += ["armyMetal", "m/s", "e/s", "idleBuilders", "idleFactories", "eStall"]
    widths = [width for _, _, width in ROLE_COLUMNS] + [9, 6, 6, 12, 13, 6]
    print_heading("timeline (units alive at the end of each window; resources averaged over it):")
    print_row("time", columns, widths)
    for index, (counts, rows) in enumerate(zip(alive, resources, strict=True)):
        if not rows:
            continue
        roles = Counter()
        army_metal = 0
        for def_name, count in counts.items():
            unit_def = game.unit_def(def_name)
            roles[role(unit_def)] += count
            if role(unit_def) == "army":
                army_metal += count * unit_def.get("metalCost", 0)

        def average(value: Callable[[Event], float], rows: list[Event] = rows) -> float:
            return sum(value(row) for row in rows) / len(rows)

        def is_energy_stalled(row: Event) -> bool:
            energy = row["energy"]
            return energy["current"] < 0.05 * max(energy["storage"], 1) and energy["pull"] > energy["income"]

        cells = [roles[unit_role] for unit_role, _, _ in ROLE_COLUMNS]
        cells += [
            f"{army_metal:.0f}",
            f"{average(lambda row: row['metal']['income']):.1f}",
            f"{average(lambda row: row['energy']['income']):.0f}",
            f"{average(lambda row: row['idleBuilderCount']):.1f}",
            f"{average(lambda row: row['idleFactoryCount']):.1f}",
            percent(average(is_energy_stalled), 1),
        ]
        print_row(windows.label(index), cells, widths)


# Builders


@dataclass
class BuilderActivity:
    """What one commander, constructor or nano did from when it was finished until it died or the game ended.

    The states are the widget's "builderState" events: working (building, assisting, repairing, reclaiming ...),
    busy (has orders but no work yet, mostly walking) or idle (no orders).
    """

    unit_id: int
    def_name: str
    role: str
    start: int
    stop: int
    started: list[Event]  # "unitCreated" events of the units it started
    resurrected: list[Event]  # "unitCreated" events of the units it resurrected
    state_frames: Counter[str] = field(default_factory=Counter)  # state -> frames
    assisted: Counter[str] = field(default_factory=Counter)  # UnitDef name of another builder's build -> frames
    tasks: Counter[str] = field(default_factory=Counter)  # CMD_NAMES name -> frames
    idle_stretches: list[tuple[int, int]] = field(default_factory=list)  # (first frame, end frame)

    @property
    def life(self) -> int:
        return self.stop - self.start


def builder_activities(game: Game, team_id: int, idle_seconds: int) -> list[BuilderActivity]:
    """Return the team's builders' activities, in the order they were finished; idle stretches of idle_seconds+."""
    created = {event["unitID"]: event for event in game.of_type("unitCreated")}
    finished_frames = {event["unitID"]: event["frame"] for event in game.of_type("unitFinished")}
    gone_frames = {
        event["unitID"]: event["frame"] for event in game.of_type("unitDestroyed") + game.of_type("unitGiven")
    }
    states = collections.defaultdict(list)
    for event in game.of_type("builderState"):
        states[event["unitID"]].append(event)
    resurrected = resurrected_ids(game)
    started, resurrected_by = collections.defaultdict(list), collections.defaultdict(list)
    for event in game.of_type("unitCreated"):
        if event.get("builderID") is not None:
            by = resurrected_by if event["unitID"] in resurrected else started
            by[event["builderID"]].append(event)

    activities = []
    unit_ids = [unit_id for unit_id in states if created.get(unit_id, {}).get("teamID") == team_id]
    for unit_id in sorted(unit_ids, key=lambda unit_id: finished_frames.get(unit_id, 0)):
        def_name = created[unit_id]["unitDef"]
        activity = BuilderActivity(
            unit_id,
            def_name,
            role(game.unit_def(def_name)),
            start=finished_frames.get(unit_id, 0),
            stop=min(gone_frames.get(unit_id, game.end_frame), game.end_frame),
            started=started[unit_id],
            resurrected=resurrected_by[unit_id],
        )
        if activity.life <= 0:
            continue
        for current, following in pairwise([*states[unit_id], {"frame": activity.stop}]):
            begin, end = max(current["frame"], activity.start), min(following["frame"], activity.stop)
            if end <= begin:
                continue
            activity.state_frames[current["state"]] += end - begin
            if current["state"] == "idle" and end - begin >= idle_seconds * GAME_SPEED:
                activity.idle_stretches.append((begin, end))
            if current["state"] != "working":
                continue
            command = current.get("command", 0)  # negative: building that UnitDef
            if command >= 0:
                activity.tasks[CMD_NAMES.get(command, f"command {command}")] += end - begin
            elif (target := created.get(current.get("targetID"), {})).get("builderID") != unit_id:
                activity.assisted[target.get("unitDef", "?")] += end - begin
        activities.append(activity)
    return activities


def print_builder_totals(activities: list[BuilderActivity]) -> None:
    """One row per role: how many, the share of their time per state, and unit-minutes idle, assisting and on tasks."""
    columns = ["units", "work", "busy", "idle", "idleMin", "assistMin", "repairMin", "reclaimMin"]
    widths = [max(len(column), 6) for column in columns]
    print_row("role", columns, widths)
    for unit_role in ("commander", "constructor", "nano"):
        group = [activity for activity in activities if activity.role == unit_role]
        if not group:
            continue
        life = sum(activity.life for activity in group)
        states = sum((activity.state_frames for activity in group), Counter())
        tasks = sum((activity.tasks for activity in group), Counter())
        assisted = sum(sum(activity.assisted.values()) for activity in group)
        cells = [
            len(group),
            percent(states["working"], life),
            percent(states["busy"], life),
            percent(states["idle"], life),
            unit_minutes(states["idle"]),
            unit_minutes(assisted),
            unit_minutes(tasks["repair"]),
            unit_minutes(tasks["reclaim"]),
        ]
        print_row(unit_role, cells, widths)


def nano_position(game: Game, activity: BuilderActivity) -> str:
    """Where a nano stands: "1128,2920, 380 from the nearest factory", and whether that is out of its build range."""
    created = {event["unitID"]: event for event in game.of_type("unitCreated")}
    nano = created[activity.unit_id]
    factories = [
        created[event["unitID"]]["position"]
        for event in game.of_type("unitFinished")
        if event["teamID"] == nano["teamID"]
        and game.unit_def(event["unitDef"]).get("isFactory")
        and created.get(event["unitID"], {}).get("position")
    ]
    if not nano.get("position") or not factories:
        return ""
    x, z = nano["position"]
    distance = min(math.dist((x, z), factory) for factory in factories)
    # To the factory's centre, further than to its edge, which is what the build range is measured to.
    build_distance = game.unit_def(activity.def_name).get("buildDistance")
    out_of_range = f", out of build range {build_distance:.0f}" if build_distance and distance > build_distance else ""
    return f"{x:.0f},{z:.0f}, {distance:.0f} from the nearest factory{out_of_range}"


def print_builder(game: Game, activity: BuilderActivity, number: int, idle_seconds: int) -> None:
    """One builder's states, idle stretches, what it started and resurrected, whose builds it assisted, other tasks."""
    life, states = activity.life, activity.state_frames
    print(f"\n{game.human_name(activity.def_name)} #{number}:")
    width = len("alive: ")

    def field(label: str, value: object) -> None:
        print(f"  {label + ':':<{width}}{value}")

    def entries(label: str, items: list[str]) -> None:
        print(f"  {label}:")
        for item in items:
            print(f"    {item}")

    field("alive", f"{mmss(activity.start)}-{mmss(activity.stop)}")
    if activity.role == "nano" and (position := nano_position(game, activity)):
        field("at", position)
    field("work", percent(states["working"], life))
    field("busy", percent(states["busy"], life))
    field("idle", percent(states["idle"], life))
    if activity.idle_stretches:
        idle = sum(end - begin for begin, end in activity.idle_stretches)
        count = len(activity.idle_stretches)
        stretches = [f"{mmss(begin):>5}  {(end - begin) // GAME_SPEED} s" for begin, end in activity.idle_stretches]
        entries(f"idle stretches ({count}x >= {idle_seconds} s, {mmss(idle)} in all)", stretches)
    if activity.started:
        entries("started", runs([(event["frame"], game.human_name(event["unitDef"])) for event in activity.started]))
    if activity.resurrected:
        resurrected = [(event["frame"], game.human_name(event["unitDef"])) for event in activity.resurrected]
        entries("resurrected", runs(resurrected))
    # How long, not when in the game: "Warden: 0:33"; under a second is left out.
    for label, durations in (
        ("assisted", {game.human_name(name): frames for name, frames in activity.assisted.most_common()}),
        ("tasks", dict(activity.tasks.most_common())),
    ):
        durations = {name: frames for name, frames in durations.items() if frames >= GAME_SPEED}
        if durations:
            name_width = max(len(name) for name in durations) + len(": ")
            entries(
                f"{label} (for how long)",
                [f"{name + ':':<{name_width}}{mmss(frames)}" for name, frames in durations.items()],
            )


def print_builders(game: Game, team_id: int, idle_seconds: int, nano_details: bool) -> None:
    """Totals per role, then each commander and constructor (and nano with nano_details).

    Nanos often stand behind the front as repair stations, so their idle time is no mistake; constructors are what
    starts new buildings.
    """
    activities = builder_activities(game, team_id, idle_seconds)
    if not activities:
        print_heading("builders: no builderState events (play the replay back again with the current widget)")
        return
    print_heading(
        "builders (work: building, assisting, repairing, reclaiming; busy: has orders but no work yet, walking):"
    )
    print_builder_totals(activities)
    numbers = Counter()  # UnitDef name -> how many of the type so far, to number them
    for activity in activities:
        numbers[activity.def_name] += 1
        if activity.role != "nano" or nano_details:
            print_builder(game, activity, numbers[activity.def_name], idle_seconds)


# Losses


def print_kills(game: Game, team_id: int, windows: Windows) -> None:
    """Enemy units the team killed and units it lost, with their metal value, per window."""
    lost, lost_metal = Counter(), Counter()
    killed, killed_metal = Counter(), Counter()
    for event in game.of_type("unitDestroyed"):
        index = windows.index(event["frame"])
        if event["teamID"] == team_id:
            lost[index] += 1
            lost_metal[index] += metal_value(game, [event])
        elif event.get("attackerTeamID") == team_id:
            killed[index] += 1
            killed_metal[index] += metal_value(game, [event])
    columns = ["killed", "killedMetal", "lost", "lostMetal"]
    widths = [max(len(column), 6) for column in columns]
    print_heading("kills and losses (units, and their metal value; partly built ones by their build progress):")
    print_row("time", columns, widths)
    for index in range(len(windows)):
        cells = [killed[index], f"{killed_metal[index]:.0f}", lost[index], f"{lost_metal[index]:.0f}"]
        print_row(windows.label(index), cells, widths)


def grid_cell(game: Game, position: list[float]) -> str:
    """Name the grid cell of a map position: "A1" is the north-west corner."""
    size_x, size_z = game.map_size
    column = min(GRID_SIZE - 1, max(0, int(GRID_SIZE * position[0] / size_x)))
    row = min(GRID_SIZE - 1, max(0, int(GRID_SIZE * position[1] / size_z)))
    return f"{GRID_COLUMNS[column]}{row + 1}"


def print_grid(cells: Counter[str], markers: dict[str, str]) -> None:
    """Print a count per grid cell, each with its markers in front ("@1 31")."""
    print(f"{'':>{TIME_WIDTH}} " + " ".join(f"{column:>{GRID_CELL_WIDTH}}" for column in GRID_COLUMNS))
    for row in range(1, GRID_SIZE + 1):
        names = [f"{column}{row}" for column in GRID_COLUMNS]
        cells_text = [f"{markers.get(name, '')} {cells[name]}".strip() for name in names]
        print(f"{row:>{TIME_WIDTH}} " + " ".join(f"{text:>{GRID_CELL_WIDTH}}" for text in cells_text))


def print_death_grid(game: Game, team_id: int, windows: Windows) -> None:
    """Where the team's mobile units and buildings died, on a grid over the map with every team's start marked.

    Per window, the cells with deaths, and how many of them static defences killed: many deaths in an enemy's start
    cell, killed by defences, are an attack on a fortified base.
    """
    if not game.map_size:
        print_heading("where units died: no map size in the events file (play the replay back again)")
        return
    markers = collections.defaultdict(str)  # cell -> "@0@1": the teams whose commander started there
    for event in game.of_type("unitCreated"):
        if event["frame"] == 0 and event.get("position") and role(game.unit_def(event["unitDef"])) == "commander":
            markers[grid_cell(game, event["position"])] += f"@{event['teamID']}"
    for mobile, kind in ((True, "mobile units"), (False, "buildings")):
        cells, by_defences = Counter(), Counter()
        window_cells = [Counter() for _ in range(len(windows))]
        window_by_defences = [Counter() for _ in range(len(windows))]
        for event in game.of_type("unitDestroyed"):
            if (
                event["teamID"] != team_id
                or not event.get("position")
                or bool(game.unit_def(event["unitDef"]).get("isMobile")) != mobile
            ):
                continue
            cell, index = grid_cell(game, event["position"]), windows.index(event["frame"])
            cells[cell] += 1
            window_cells[index][cell] += 1
            if role(game.unit_def(event.get("attackerDef"))) == "defence":
                by_defences[cell] += 1
                window_by_defences[index][cell] += 1
        if not cells:
            continue
        print_heading(
            f"where {kind} died ({GRID_SIZE}x{GRID_SIZE} grid of the map, {GRID_COLUMNS[0]}-{GRID_COLUMNS[-1]} west to"
            f" east, 1-{GRID_SIZE} north to south; @N: team N's start):"
        )
        print_grid(cells, markers)
        total, total_by_defences = cells.total(), by_defences.total()
        print(f"{'':>{TIME_WIDTH}}  killed by static defences: {total_by_defences} of {total}")
        print()
        for index in range(len(windows)):
            items = [
                f"{cell} {count}"
                + (f" ({window_by_defences[index][cell]} by defences)" if window_by_defences[index][cell] else "")
                for cell, count in window_cells[index].most_common()
            ]
            print_list(f"{windows.label(index):>{TIME_WIDTH}}  ", items)


def print_trades(game: Game, team_id: int, windows: Windows) -> None:
    """Per window: army units and constructors finished, units resurrected, enemy units killed, units lost, and the
    enemy unit types that killed them."""
    built = [Counter() for _ in range(len(windows))]
    resurrected = [Counter() for _ in range(len(windows))]
    killed = [Counter() for _ in range(len(windows))]
    lost = [Counter() for _ in range(len(windows))]
    killers = [Counter() for _ in range(len(windows))]
    for event in finished_units(game, team_id):
        if role(game.unit_def(event["unitDef"])) in ("army", "constructor"):
            built[windows.index(event["frame"])][event["unitDef"]] += 1
    for event in resurrected_units(game, team_id):
        resurrected[windows.index(event["frame"])][event["unitDef"]] += 1
    for event in game.of_type("unitDestroyed"):
        if event["teamID"] != team_id and event.get("attackerTeamID") == team_id:
            killed[windows.index(event["frame"])][event["unitDef"]] += 1
        elif event["teamID"] == team_id:
            index = windows.index(event["frame"])
            lost[index][event["unitDef"]] += 1
            if event.get("attackerDef"):
                killers[index][event["attackerDef"]] += 1
    width = len("resurrected: ")
    print_heading(
        "trades (built: army units and constructors finished; killed: enemy units; killed by: the enemy unit types):"
    )
    for index in range(len(windows)):
        if index:
            print()
        print_list(f"{windows.label(index):>{TIME_WIDTH}}  {'built:':<{width}}", unit_counts(game, built[index]))
        print_list(f"{'':>{TIME_WIDTH}}  {'resurrected:':<{width}}", unit_counts(game, resurrected[index]))
        print_list(f"{'':>{TIME_WIDTH}}  {'killed:':<{width}}", unit_counts(game, killed[index]))
        print_list(f"{'':>{TIME_WIDTH}}  {'lost:':<{width}}", unit_counts(game, lost[index]))
        print_list(f"{'':>{TIME_WIDTH}}  {'killed by:':<{width}}", unit_counts(game, killers[index]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "events", help=f"file name of the events file in events/ next to this script ({EVENTS_SUFFIX} optional)"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=play_replay.DATA_DIR,
        help="BAR data dir with the replay in demos/, as for play_replay.py (default: the one this repository is"
        " cloned into as a widget, else data/ next to this script)",
    )
    parser.add_argument("--step", type=float, default=5.0, help="window length in minutes (default 5)")
    parser.add_argument(
        "--build-order-minutes",
        type=float,
        default=5.0,
        help="list the units finished in this many minutes from the start as build order (default 5)",
    )
    parser.add_argument(
        "--idle-builder-seconds",
        type=int,
        default=15,
        help="list a builder's idle stretches of at least this many seconds (default 15)",
    )
    parser.add_argument(
        "--nano-details", action="store_true", help="list nano turrets one by one too (default: in the totals only)"
    )
    args = parser.parse_args()

    events_file = find_events(args.events)
    replay_file, team_labels = read_team_labels(args.data_dir.resolve(), events_file)
    game = load(events_file, team_labels)
    length_frame = game.end_frame
    quits = find_quits(game)
    game = drop_quit_losses(game, quits)
    windows = Windows(args.step, game.end_frame)
    alive = alive_at_window_ends(game, windows)

    print_header(game, replay_file, length_frame, quits)
    for team_id in game.teams:
        print(f"\n\n{game.team_title(team_id)}")
        print_build_order(game, team_id, args.build_order_minutes)
        print_milestones(game, team_id)
        print_timeline(game, team_id, windows, alive[team_id])
        print_builders(game, team_id, args.idle_builder_seconds, args.nano_details)
        print_kills(game, team_id, windows)
        print_death_grid(game, team_id, windows)
        print_trades(game, team_id, windows)


if __name__ == "__main__":
    main()
