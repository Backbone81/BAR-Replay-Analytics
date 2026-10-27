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
   `dbg_replay_analytics.lua` logging every unit event, income and builder state to a JSON lines file. It downloads
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
python3 -I play_replay.py "<replay>"                            # playback, writes events/<replay>_events.jsonl
python3 -I replay_events.py "<replay>" > "events/<replay>.txt"  # the full report
```

`<replay>` is the replay's file name in `demos/`, `.sdfz` optional. `play_replay.py` prints its progress and where
the events file and the engine log (`events/<replay>.log`) went. Other widgets installed in `LuaUI/Widgets/` run
during playback too, so their log lines show what a widget of yours would have done in that game.

A playback changes the data dir like watching a replay in the client does: BAR saves its widget settings
(`LuaUI/Config/BYAR.lua`) and the engine overwrites `infolog.txt`.

## Events file

One JSON object per line. Every event has `event` (its type) and `frame` (30 per second). Unit types are given by
their def name (`unitDef`, e.g. `corcom`), positions as `[x, z]` in map coordinates (elmos). Fields without a value
are left out.

| `event` | When | Fields |
| --- | --- | --- |
| `init` | first | `map`, `mapSizeX`, `mapSizeZ` (in elmos), `game` (name and version), `teams`: list of `teamID`, `allyTeamID`, `isAI`, `side`, `name` |
| `unitDef` | first time a unit type shows up | `name`, `humanName`, `metalCost`, `energyCost`, `buildTime`, `techLevel`, `unitGroup`, `isFactory`, `isBuilder`, `isCommander` (BAR's `customParams.iscommander`), `isMobile`, `buildDistance` (builders), `canResurrect`, `canFly`, `extractsMetal`, `energyMake`, `windGenerator`, `maxWeaponRange` |
| `unitCreated` | a unit is started (or spawned) | `unitID`, `unitDef`, `teamID`, `position`, `builderID`, `builderDef` |
| `unitFinished` | a unit is finished | `unitID`, `unitDef`, `teamID` |
| `unitDestroyed` | a unit dies | `unitID`, `unitDef`, `teamID`, `position`, `buildProgress`, `attackerDef`, `attackerTeamID`, `weapon` |
| `unitGiven` | a unit changes team | `unitID`, `unitDef`, `teamID` (new), `oldTeamID` |
| `resources` | every 5 s per team | `teamID`, `metal` and `energy` (each `current`, `storage`, `pull`, `income`, `expense`), `unitCount`, `builderCount`, `idleBuilderCount`, `factoryCount`, `idleFactoryCount` |
| `armyPositions` | every 30 s per team | `teamID`, `units`: list of `unitDef`, `position` (mobile units that are not builders) |
| `builderState` | a commander's, constructor's or nano turret's state changes (sampled every 0.5 s) | `unitID`, `state` (`working`, `busy`: has orders but no work yet, mostly walking, or `idle`: no orders), `command` (the engine command ID; negative: building that unit def), `targetID` |
| `gameOver` | the game ends | `winningAllyTeamIDs` |
| `end` | last | `reason`: `gameover`, `stalled` (the replay stopped advancing), `never started` or `shutdown` |

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
