"""Read the start script and per-team statistics from a Recoil/BAR replay (.sdfz).

Needs only the replay file: no engine, game or map. Format reference (RecoilEngine): rts/System/LoadSave/demofile.h,
DemoRecorder.cpp and rts/Sim/Misc/TeamStatistics.h.

    python3 -I sdfz_stats.py replays/<file>.sdfz             # header, teams, a table per team in 5 min windows
    python3 -I sdfz_stats.py replays/<file>.sdfz --step 1    # 1 min windows (0.25: every 15 s)
    python3 -I sdfz_stats.py replays/<file>.sdfz --json out.json
"""

import argparse
import gzip
import json
import re
import struct
from collections import namedtuple
from itertools import pairwise
from typing import Any, TypedDict

GAME_SPEED = 30  # frames per second of game time (GAME_SPEED in the engine, Game.gameSpeed in Lua)

# DemoFileHeader, version 5. header_size tells where the start script begins.
DEMO_HEADER = struct.Struct("<16sii256s16sQiiiiiiiiiiii")
DemoHeader = namedtuple(
    "DemoHeader",
    "magic version header_size engine_version game_id unix_time script_size demo_stream_size game_time "
    "wallclock_time num_players player_stat_size player_stat_elem_size num_teams team_stat_size "
    "team_stat_elem_size team_stat_period winning_ally_teams_size",
)

# TeamStatistics: frame, 12 floats, 7 ints (80 bytes), each cumulative since the game started.
TEAM_STATISTICS = struct.Struct("<i12f7i")
TEAM_STATISTICS_FIELDS = [
    "frame",
    "metalUsed", "energyUsed", "metalProduced", "energyProduced",
    "metalExcess", "energyExcess", "metalReceived", "energyReceived",
    "metalSent", "energySent", "damageDealt", "damageReceived",
    "unitsProduced", "unitsDied", "unitsReceived", "unitsSent",
    "unitsCaptured", "unitsOutCaptured", "unitsKilled",
]  # fmt: skip

# Start script tokens: a section opening "[name] {", a closing "}", or "key=value;".
SCRIPT_TOKEN = re.compile(r"\[(\w+)\]\s*\{|(\})|(\w+)=([^;]*);")

# Table columns. Resources: (field, column in the windows as a per-second rate, column in the total row).
RESOURCES = [
    ("metalProduced", "m/s", "mProd"),
    ("metalUsed", "mUse/s", "mUse"),
    ("metalExcess", "mWaste/s", "mWaste"),
    ("energyProduced", "e/s", "eProd"),
    ("energyUsed", "eUse/s", "eUse"),
    ("energyExcess", "eWaste/s", "eWaste"),
]
# Counts, within the window: (field, column, width).
COUNTS = [
    ("unitsProduced", "built", 6),
    ("unitsKilled", "killed", 6),
    ("damageDealt", "dmgOut", 9),
    ("unitsDied", "lost", 6),
    ("damageReceived", "dmgIn", 9),
]
LABEL_WIDTH = 11
RESOURCE_WIDTH = 9

TeamStatistics = dict[str, float]
TeamLabel = dict[str, str]  # "side", "allyteam", "who"


class Replay(TypedDict):
    """The parsed replay; the keys follow the engine's names, as they also end up in --json."""

    engine: str  # engine version, a RecoilEngine tag
    gameTime: int  # seconds of game time
    wallclockTime: int  # seconds
    unixTime: int  # when the game started
    statPeriod: int  # seconds between two team statistics rows
    script: str  # start script
    winningAllyTeams: list[int]
    teams: dict[int, list[TeamStatistics]]  # teamID -> rows, oldest first


# Reading the replay


def load(path: str) -> Replay:
    """Read a replay's header, start script and trailer (winners and team statistics)."""
    with gzip.open(path) as file:
        data = file.read()
    header = DemoHeader._make(DEMO_HEADER.unpack_from(data))
    if not header.magic.startswith(b"spring demofile") or header.version != 5:
        raise ValueError("not a version 5 demo file")

    offset = header.header_size
    script = data[offset : offset + header.script_size].decode("utf-8", "replace")
    offset += header.script_size

    replay: Replay = {
        "engine": header.engine_version.split(b"\0")[0].decode(),
        "gameTime": header.game_time,
        "wallclockTime": header.wallclock_time,
        "unixTime": header.unix_time,
        "statPeriod": header.team_stat_period,
        "script": script,
        "winningAllyTeams": [],
        "teams": {},
    }
    # A crashed game leaves the stream size at 0 and writes no trailer.
    if header.demo_stream_size == 0:
        return replay

    # Trailer, in the order DemoRecorder.cpp writes it: winners (1 byte each), player stats, team stats.
    offset += header.demo_stream_size
    replay["winningAllyTeams"] = list(data[offset : offset + header.winning_ally_teams_size])
    offset += header.winning_ally_teams_size + header.player_stat_size
    if header.team_stat_elem_size != TEAM_STATISTICS.size:
        raise ValueError(f"unexpected TeamStatistics size {header.team_stat_elem_size}")
    row_counts = struct.unpack_from(f"<{header.num_teams}i", data, offset)
    offset += 4 * header.num_teams
    for team_id, row_count in enumerate(row_counts):
        rows = []
        for _ in range(row_count):
            rows.append(dict(zip(TEAM_STATISTICS_FIELDS, TEAM_STATISTICS.unpack_from(data, offset), strict=True)))
            offset += TEAM_STATISTICS.size
        end_running_totals(rows, header.game_time * GAME_SPEED)
        replay["teams"][team_id] = rows
    return replay


def end_running_totals(rows: list[TeamStatistics], end_frame: int) -> None:
    """Move the last row, the totals at game end, to the end of the game.

    The engine keeps the running totals as the last row and labels it in advance with the frame of the next 15 s
    sample (rts/Sim/Misc/Team.cpp), up to 15 s after the game ended. end_frame is from the header's whole seconds.
    A last row that adds nothing to the sample before it is dropped.
    """
    if len(rows) < 2:
        return
    last, previous = rows[-1], rows[-2]
    end_frame = min(last["frame"], end_frame)
    unchanged = all(last[field] == previous[field] for field in TEAM_STATISTICS_FIELDS if field != "frame")
    if unchanged or end_frame <= previous["frame"]:
        rows.pop()
    else:
        last["frame"] = end_frame


# Start script


def parse_script(script: str) -> dict[str, dict[str, str]]:
    """Return {section: {key: value}} for the start script's sections, keys in lower case.

    "game" holds the top-level keys (mapname, gametype, ...); "team0", "player0", "ai0" etc. hold the keys of that
    section without its subsections. AI sections also get "profile" from their options subsection.
    """
    root: dict[str, Any] = {}
    stack = [root]
    for section, close, key, value in SCRIPT_TOKEN.findall(script):
        if section:
            stack[-1][section.lower()] = child = {}
            stack.append(child)
        elif close:
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1][key.lower()] = value

    def values(section: dict[str, Any]) -> dict[str, str]:
        return {key: value for key, value in section.items() if isinstance(value, str)}

    game = root.get("game", {})
    sections = {"game": values(game)}
    for name, section in game.items():
        if isinstance(section, dict):
            sections[name] = values(section)
            if name.startswith("ai") and "profile" in section.get("options", {}):
                sections[name]["profile"] = section["options"]["profile"]
    return sections


def team_labels(sections: dict[str, dict[str, str]]) -> dict[int, TeamLabel]:
    """Return {teamID: {"side", "allyteam", "who"}}; "who" is the player's name or the AI's short name and profile."""
    labels = {}
    for section, values in sections.items():
        if section.startswith("team"):
            labels[int(section.removeprefix("team"))] = {
                "side": values.get("side", "?"),
                "allyteam": values.get("allyteam", "?"),
                "who": "?",
            }
    for section, values in sections.items():
        if "team" not in values:
            continue
        team_id = int(values["team"])
        if section.startswith("player") and values.get("spectator", "0") != "1":
            labels.setdefault(team_id, {})["who"] = values.get("name", section)
        elif section.startswith("ai"):
            who = f"{values.get('shortname', 'AI')} {values.get('profile', '')}"
            labels.setdefault(team_id, {})["who"] = who.strip()
    return labels


# Report


def mmss(frame: float) -> str:
    """Game time of a frame as minutes:seconds."""
    minutes, seconds = divmod(round(frame / GAME_SPEED), 60)
    return f"{minutes}:{seconds:02d}"


def team_title(team_id: int, label: TeamLabel) -> str:
    """One line naming the team. The side is the lobby's: a replay does not record what a "Random" pick became."""
    return f"team {team_id} (allyteam {label.get('allyteam')}): {label.get('who')}, lobby side {label.get('side')}"


def table_line(label: str, resource_cells: list[str], count_cells: list[str]) -> str:
    """One right-aligned table line: label, then the resource columns, then the count columns."""
    resources = " ".join(f"{cell:>{RESOURCE_WIDTH}}" for cell in resource_cells)
    counts = " ".join(f"{cell:>{width}}" for cell, (_, _, width) in zip(count_cells, COUNTS, strict=True))
    return f"{label:>{LABEL_WIDTH}} {resources} {counts}"


def print_header(replay: Replay, sections: dict[str, dict[str, str]], labels: dict[int, TeamLabel]) -> None:
    game = sections["game"]
    print(f"map:    {game.get('mapname')}")
    print(f"game:   {game.get('gametype')}")
    print(f"engine: {replay['engine']}")
    print(f"length: {mmss(replay['gameTime'] * GAME_SPEED)}")
    print("teams:")
    for team_id, label in sorted(labels.items()):
        print(f"  {team_title(team_id, label)}")
    print(f"winning allyteams: {replay['winningAllyTeams'] or 'none recorded (resign/quit or crash)'}")


def print_team_table(rows: list[TeamStatistics], stride: int) -> None:
    """One line per window of stride statistics rows (the last one up to the end of the game), then the game total.

    Windows show resources as per-second rates and the counts within the window; the total shows resource totals.
    """
    count_cells = [column for _, column, _ in COUNTS]

    def counts(first: int, last: int) -> list[str]:
        return [str(round(rows[last][field] - rows[first][field])) for field, _, _ in COUNTS]

    def window(first: int, last: int) -> str:
        return f"{mmss(rows[first]['frame'])}-{mmss(rows[last]['frame'])}"

    print(table_line("time", [column for _, column, _ in RESOURCES], count_cells))
    bounds = [*range(0, len(rows), stride)]
    if bounds[-1] != len(rows) - 1:
        bounds.append(len(rows) - 1)
    for first, last in pairwise(bounds):
        seconds = (rows[last]["frame"] - rows[first]["frame"]) / GAME_SPEED
        rates = [f"{(rows[last][field] - rows[first][field]) / seconds:.1f}" for field, _, _ in RESOURCES]
        print(table_line(window(first, last), rates, counts(first, last)))

    first, last = 0, len(rows) - 1
    totals = [f"{rows[last][field] - rows[first][field]:.0f}" for field, _, _ in RESOURCES]
    print()
    print(table_line("total", [column for _, _, column in RESOURCES], count_cells))
    print(table_line(window(first, last), totals, counts(first, last)))


def print_report(replay: Replay, step_minutes: float) -> None:
    """Print the header, then each team's table in windows of step_minutes (rounded to the statistics period)."""
    sections = parse_script(replay["script"])
    labels = team_labels(sections)
    print_header(replay, sections, labels)
    # All teams have the same sample frames; cut to the shortest in case one ends early.
    length = min((len(rows) for rows in replay["teams"].values()), default=0)
    if length < 2:
        print("no team statistics (crashed game?)")
        return
    stride = max(1, round(step_minutes * 60 / replay["statPeriod"]))
    for team_id, rows in sorted(replay["teams"].items()):
        print(f"\n{team_title(team_id, labels.get(team_id, {}))}")
        print_team_table(rows[:length], stride)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("replay")
    parser.add_argument("--step", type=float, default=5.0, help="window length in minutes (default 5)")
    parser.add_argument("--json", help="also write the parsed replay to this file")
    args = parser.parse_args()

    replay = load(args.replay)
    if args.json:
        with open(args.json, "w") as file:
            json.dump(replay, file)
    print_report(replay, args.step)


if __name__ == "__main__":
    main()
