# bar-replay-analytics

Tools to analyze your own [Beyond All Reason](https://www.beyondallreason.info/) games from their replays (`.sdfz`):
how your economy grew, what you built and when, how long your constructors stood idle, what you lost, to what and
where. Compare your numbers with your opponent's and with your earlier games to see what to work on next.

## Scripts

In the order they are usually run:

1. `sdfz_stats.py` reads a replay file alone, with no engine, game or map needed. It prints the header (engine and
   game version, map, teams) and per team metal and energy produced, used and wasted, damage, and units produced,
   killed and lost, in time windows. That is all a replay stores itself: no unit types, build order or positions.
2. `play_replay.py` plays the replay back in a headless engine at maximum speed, with the analytics widget
   `dbg_replay_analytics.lua` logging every unit event, income and builder state to a JSON file. It downloads
   the engine, game and map versions the replay needs if they are missing. A 27-minute 1v1 takes about 1.5 minutes.
3. `replay_events.py` turns that events file into a report per team: build order, milestones (factories, tech 2,
   metal extractors), a timeline of units, army value and income, what each builder did and how long it stood idle,
   kills and losses with their metal value, and where units died on a grid over the map.

`--help` lists each script's options.

## Installation

Needed: Python 3.10 or later (standard library only), git, and 7-Zip (`7z`, `7zz` or `7za` on `PATH`; on
Debian/Ubuntu `p7zip-full` or `7zip`) to unpack downloaded engines.

Clone the repository into `LuaUI/Widgets/` of your BAR data dir (the folder with `demos/`, `maps/`, `engine/` and
`LuaUI/`):

```
git clone https://github.com/backbone81/bar-replay-analytics.git "<BAR data dir>/LuaUI/Widgets/bar-replay-analytics"
```

This installs the analytics widget, and the scripts find the data dir from where they are. The widget is active only
in a headless playback; in the normal client, also when watching a replay, it removes itself, so it can stay
installed. To update, run `git pull` in the clone.

## Analyzing a game

After a game, its replay is in `<BAR data dir>/demos/`. Run the scripts from the clone, with `-I` because the replay
is untrusted input:

```
cd "<BAR data dir>/LuaUI/Widgets/bar-replay-analytics"
python3 -I sdfz_stats.py "../../../demos/<replay>.sdfz"         # quick overview, 5 min windows (--step 1: 1 min)
python3 -I play_replay.py "<replay>"                            # playback, writes events/<replay>_events.json
python3 -I replay_events.py "<replay>" > "events/<replay>.txt"  # the full report
```

`<replay>` is the replay's file name in `demos/`, `.sdfz` optional. `play_replay.py` prints its progress and where
the events file and the engine log (`events/<replay>.log`) went. Other widgets installed in `LuaUI/Widgets/` run
during playback too, so their log lines show what a widget of yours would have done in that game.

A playback changes the data dir like watching a replay in the client does: BAR saves its widget settings
(`LuaUI/Config/BYAR.lua`) and the engine overwrites `infolog.txt`.

## Events file

One JSON object, with one event per line:

```
{"events": [...], "game": {...}, "teams": [...], "teamStats": {"<teamID>": [...]}, "unitDefs": {"<unitDefID>": {...}},
 "end": {"frame": ..., "reason": ...}}
```

Each event holds its `frame` (30 per second) and its fields under one key, the event's name: `{"frame": 0,
"unitFinished": {"unitID": 2858, "unitDefID": 101, "unitTeam": 0}}`. Events are named after the call-in or `Spring`
function they come from, in lower camel case (`unitCreated` is `widget:UnitCreated`), and so are their fields
(`unitTeam`, `attackerDefID`, `currentLevel`). Unit types are given by `unitDefID`, positions as `x` and `z` in elmos,
rounded down. Fields without a value are left out. All events come from call-ins, nothing is sampled.

What does not change during the game, or the engine already records, is written once, after the events:

| Key | Fields |
| --- | --- |
| `game` | `mapName`, `mapSizeX`, `mapSizeZ` (in elmos), `gameName`, `gameVersion`, `maxUnits` (`Game.maxUnits`) |
| `teams` | list of `teamID`, `allyTeam`, `isAI`, `side` (as `Spring.GetTeamInfo`), `startUnit` (the commander's `unitDefID`: BAR's team rules param `startUnit`, which also tells the faction of a `random` side), `name` |
| `teamStats` | every team's statistics history by `teamID`, as `Spring.GetTeamStatsHistory` returns it: an entry every 15 s with `time`, `frame` and the totals since the start of the game (`metalProduced`, `metalUsed`, `metalExcess`, `metalSent`, `metalReceived`, the same for energy, `damageDealt`, `damageReceived`, `unitsProduced`, `unitsDied`, `unitsKilled`, `unitsSent`, `unitsReceived`, `unitsCaptured`, `unitsOutCaptured`) |
| `unitDefs` | the unit defs the events and teams refer to, by `unitDefID`, with these fields of `UnitDefs`: `name`, `translatedHumanName`, `metalCost`, `energyCost`, `buildTime`, `isFactory`, `isBuilder`, `canMove`, `buildDistance`, `canResurrect`, `extractsMetal`, `energyMake`, `windGenerator`, `maxWeaponRange`, `customParams` (`techlevel`, `unitgroup`, `iscommander`, as strings) |

`end` is written when the widget stops logging. Its `reason` is `gameover` if the game ended, otherwise `stalled` (the
replay stopped advancing), `never started` or `shutdown`. A file without `end` is incomplete: the engine crashed or
was stopped.

| Event | When | Fields |
| --- | --- | --- |
| `unitCreated` | a unit is started (or spawned) | `unitID`, `unitDefID`, `unitTeam`, `x`, `z`, `builderID` |
| `unitFinished` | a unit is finished | `unitID`, `unitDefID`, `unitTeam` |
| `unitDestroyed` | a unit dies | `unitID`, `unitDefID`, `unitTeam`, `x`, `z`, `buildProgress`, `attackerID`, `attackerDefID`, `attackerTeam`, `weaponDefID` (negative: `Game.envDamageTypes`, e.g. `SelfD`) |
| `unitGiven` | a unit changes team | `unitID`, `unitDefID`, `newTeam`, `oldTeam` |
| `unitIdle` | a commander's, constructor's or factory's queue ran empty, or it is finished or given without orders | `unitID`, `unitDefID`, `unitTeam` |
| `unitCommand` | a commander, constructor or factory is given a command (also states, stop, remove: not every command enters the queue) | `unitID`, `unitDefID`, `unitTeam`, `cmdID` (a build is `-unitDefID`), `cmdParams`, `cmdOpts` (`CommandOptions.coded`), `playerID`, `fromSynced`, `fromLua` |
| `unitCmdDone` | a commander, constructor or factory finished a command | `unitID`, `unitDefID`, `unitTeam`, `cmdID`, `cmdParams` |
| `teamDied` | a team died (resigned, left or wiped out); BAR then removes its units | `teamID` |
| `gameOver` | the game ends | `winningAllyTeams` |

## Development

To work on a clone outside the BAR data dir, link it into the data dir's `LuaUI/Widgets/` so BAR loads the widget
from it, and pass that data dir to the scripts:

```
ln -s "$PWD" "<BAR data dir>/LuaUI/Widgets/bar-replay-analytics"
python3 -I play_replay.py "<replay>" --data-dir "<BAR data dir>"
python3 -I replay_events.py "<replay>" --data-dir "<BAR data dir>"
```

Without `--data-dir`, such a clone uses `data/` next to the scripts (git-ignored) as a separate data dir, which then
needs its own `demos/` and the widget link in `LuaUI/Widgets/`; the engine, game and map are downloaded into it.
BAR loads `.lua` files only one folder level deep under `LuaUI/Widgets/`, so the widget is the only `.lua` file in
the repository root.

Lua is formatted with [StyLua](https://github.com/JohnnyMorganz/StyLua) 2.5.2 (`.stylua.toml`, as in Beyond All
Reason), Python checked with [ruff](https://docs.astral.sh/ruff/):

```
stylua --check .
ruff check --line-length 120 --extend-select E501 . && ruff format --check --line-length 120 .
```
