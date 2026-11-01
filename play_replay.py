"""Play a replay back in spring-headless and collect the events the Replay Analytics widget logs.

    python3 -I play_replay.py <replay file name> [--data-dir <dir>]

The script works in a BAR data dir (the folder with demos/, maps/, engine/ and LuaUI/), which it passes to the engine
as its write dir. By default that is the one this repository is cloned into as a widget,
<data dir>/LuaUI/Widgets/<this repository>/. --data-dir uses another one; a clone outside LuaUI/Widgets/ uses data/
next to this script.

In the data dir, the script uses:

    demos/          the replay
    engine/         the engine at the replay's version, downloaded if missing (needs 7-Zip: 7z, 7zz or 7za)
    maps/           the map, downloaded with the engine's pr-downloader if missing
    rapid/          the game at the replay's version (with packages/ and pool/), downloaded the same way if missing
    LuaUI/Widgets/  the analytics widget (this repository) and any other widgets, e.g. bar-advisor

The analytics widget writes events/<replay name>_events.json in its own folder, and the engine output goes next to
it as <replay name>.log. The script prints the progress, and stops the engine if the playback hangs: no new events
(or, while the engine loads, no new log output) for a minute.
"""

import argparse
import gzip
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Literal

HERE = Path(__file__).resolve().parent
# A clone in a BAR data dir's LuaUI/Widgets/ plays back in that data dir.
IN_WIDGETS = [part.lower() for part in HERE.parts[-3:-1]] == ["luaui", "widgets"]
DATA_DIR = HERE.parents[2] if IN_WIDGETS else HERE / "data"

ANALYTICS_WIDGET = "dbg_replay_analytics.lua"
ANALYTICS_FAILED = "Replay Analytics: cannot write"  # the widget's message when it cannot open its events file
ADVISOR_WIDGET = "snd_advisor.lua"

# Output is one "label: value" per line, the values aligned as in sdfz_stats.py.
FIELD_WIDTH = len("bar-replay-analytics: ")
GAME_SPEED = 30  # frames per second of game time (GAME_SPEED in the engine, Game.gameSpeed in Lua)
STALL_SECONDS = 60
PROGRESS_SECONDS = 15

WINDOWS = sys.platform == "win32"
SPRING_HEADLESS = "spring-headless.exe" if WINDOWS else "spring-headless"
PR_DOWNLOADER = "pr-downloader.exe" if WINDOWS else "pr-downloader"
MAP_ARCHIVE_SUFFIXES = {".sd7", ".sdz"}
PROGRESS_BAR = re.compile(r"\[Progress\]\s*(\d+)%.*?\d+/(\d+)")  # pr-downloader's, see pr_download
# pr-downloader settings of the BAR launcher (BYAR-Chobby dist_cfg/config.json): BAR's servers, not SpringRTS's.
PR_DOWNLOADER_ENV = {
    "PRD_HTTP_SEARCH_URL": "https://files-cdn.beyondallreason.dev/find",
    "PRD_RAPID_REPO_MASTER": "https://repos-cdn.beyondallreason.dev/repos.gz",
    "PRD_RAPID_USE_STREAMER": "false",
}
# Where the BAR lobby downloads engines from (BYAR-Chobby gui_steam_coop_handler.lua, GetEngineDownloadUrl), and
# RecoilEngine's GitHub releases, which BAR's CDN is fed from, as a fallback for Recoil tags.
ENGINE_CDN_URL = "https://files-cdn.beyondallreason.dev/download?category=engine_{platform}64&springname={tag}"
ENGINE_GITHUB_URL = (
    "https://github.com/beyond-all-reason/RecoilEngine/releases/download/{tag}/recoil_{tag}_amd64-{platform}.7z"
)
# Names taken from the replay end up in paths and command lines, so they must not contain a path separator or start
# like an option or a relative path: "2026.07.04", "Beyond All Reason test-31720-0f97aa5", "Argent Strata 1.1".
SAFE_NAME = re.compile(r"[A-Za-z0-9][^/\\:\x00-\x1f]*")


class PlaybackError(Exception):
    """A reason the replay cannot be played back; main() prints it and exits."""


def import_sibling(name: str) -> ModuleType:
    """Import a module next to this script; python3 -I leaves the script's folder off sys.path."""
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import {name}.py from {HERE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sdfz_stats = import_sibling("sdfz_stats")


def print_field(label: str, value: object) -> None:
    """Print one piece of information on its own line, as "label: value"; an empty label continues the one above."""
    print(f"{label + ':' if label else '':<{FIELD_WIDTH}}{value}")


def print_dir(label: str, folder: Path) -> None:
    """Print a folder and, on the next line, where it links to if it is (or is in) a link."""
    print_field(label, folder)
    if (target := folder.resolve()) != folder:
        print_field("", f"-> {target}")


def check_name(kind: str, name: str) -> str:
    """Return a name read from the (untrusted) replay if it is safe to use in a path or as a command argument."""
    if not SAFE_NAME.fullmatch(name):
        raise PlaybackError(f"the replay's {kind} {name!r} is not a plain name")
    return name


# Map and game


def normalize(name: str) -> str:
    """Map names and archive file names differ in case and punctuation: "Argent Strata 1.1" -> argent_strata_1_1."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def map_archives(maps_dir: Path) -> set[Path]:
    """Return the map archives (.sd7, .sdz) in maps_dir."""
    return {file for file in maps_dir.iterdir() if file.suffix.lower() in MAP_ARCHIVE_SUFFIXES and file.is_file()}


def find_map(maps_dir: Path, map_name: str) -> Path | None:
    """Return the archive in maps_dir whose file name matches map_name (see normalize), if any."""
    wanted = normalize(map_name)
    return next((file for file in sorted(map_archives(maps_dir)) if normalize(file.stem) == wanted), None)


def find_game(data_dir: Path, game_type: str) -> Path | None:
    """Return the rapid package of the game game_type ("Beyond All Reason test-31720-0f97aa5") in the data dir.

    pr-downloader lists the packages in rapid/<host>/<repo>/versions.gz ("tag,md5,depends,name" per line) and stores
    each one as packages/<md5>.sdp, with its files in pool/.
    """
    for versions in sorted(data_dir.glob("rapid/*/*/versions.gz")):
        with gzip.open(versions, "rt", encoding="utf-8", errors="replace") as file:
            for line in file:
                _tag, md5, _depends, name = line.rstrip("\n").split(",", 3)
                package = data_dir / "packages" / f"{md5}.sdp"
                if name == game_type and package.is_file():
                    return package
    return None


def pr_download(engine: Path, data_dir: Path, kind: Literal["map", "game"], name: str) -> bool:
    """Download a map or game into the data dir with the engine's pr-downloader; True if it worked.

    Uses the BAR launcher's settings, and prints pr-downloader's last lines if it fails.
    """
    pr_downloader = engine.parent / PR_DOWNLOADER
    if not pr_downloader.is_file():
        print(f"the engine has no {PR_DOWNLOADER} to download {name}")
        return False
    print(f"downloading {kind} {name}")
    start = last_progress = time.monotonic()
    output = []  # pr-downloader's lines without its progress bars
    with tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            [pr_downloader, "--filesystem-writepath", data_dir, f"--download-{kind}", name],
            env=os.environ | PR_DOWNLOADER_ENV,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        # The progress bar is redrawn with \r: "[Progress]  41% [=====     ] 13893524/34023778".
        for line in io.TextIOWrapper(process.stdout, errors="replace", newline=""):
            for part in line.split("\r"):
                if not (progress := PROGRESS_BAR.search(part)):
                    output.append(part.rstrip("\n"))
                    continue
                now = time.monotonic()
                percent, total = int(progress[1]), int(progress[2])
                if now - last_progress >= PROGRESS_SECONDS and total >= 1e6:  # bytes, not a count of small files
                    last_progress = now
                    print(f"  {now - start:4.0f} s: {percent}% of {total / 1e6:.0f} MB")
        process.wait()
        stderr.seek(0)
        output += stderr.read().decode(errors="replace").splitlines()
    if process.returncode != 0:
        print(f"{PR_DOWNLOADER} failed; its last lines:")
        print("\n".join([line for line in output if line.strip()][-20:]))
    return process.returncode == 0


def find_or_download_game(data_dir: Path, game_type: str, engine: Path) -> Path:
    """Return the rapid package of the game game_type (see find_game), downloaded if missing."""
    if game := find_game(data_dir, game_type):
        return game
    if pr_download(engine, data_dir, "game", game_type) and (game := find_game(data_dir, game_type)):
        return game
    raise PlaybackError(f"game {game_type} is not in {data_dir} and could not be downloaded")


def find_or_download_map(data_dir: Path, map_name: str, engine: Path) -> Path:
    """Return the map archive in the data dir's maps/, downloaded if missing.

    A download whose file name does not match the map name is renamed to the normalized map name, so find_map finds
    it next time; the engine reads the map's name from inside the archive.
    """
    maps_dir = data_dir / "maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    if map_archive := find_map(maps_dir, map_name):
        return map_archive
    before = map_archives(maps_dir)
    if not pr_download(engine, data_dir, "map", map_name):
        raise PlaybackError(f"map {map_name} is not in {maps_dir} and could not be downloaded")
    downloaded = map_archives(maps_dir) - before
    if len(downloaded) != 1:
        raise PlaybackError(f"downloading map {map_name} added {len(downloaded)} archives to {maps_dir}, not one")
    map_archive = downloaded.pop()
    if normalize(map_archive.stem) == normalize(map_name):
        return map_archive
    return map_archive.rename(maps_dir / (normalize(map_name) + map_archive.suffix))


# Engine


def is_recoil_tag(engine_version: str) -> bool:
    """Tell a RecoilEngine release tag ("2026.07.04") from an older Spring engine version ("105.1.1-... BAR105")."""
    return engine_version.startswith("2")


def engine_folder_name(engine_version: str) -> str:
    """Return the folder name the BAR lobby uses for an engine version in engine/, so a BAR install's data dir works.

    Follows BYAR-Chobby gui_steam_coop_handler.lua GetEnginePath: "2025.01.6" -> "rel2501.2025.01.6",
    "2026.07.04" -> "recoil_2026.07.04", older versions lower case with " BAR105" -> " bar".
    """
    if engine_version.startswith("2025.01"):
        return f"rel{engine_version[2:4]}{engine_version[5:7]}.{engine_version}".lower()
    if is_recoil_tag(engine_version):
        return f"recoil_{engine_version}".lower()
    return engine_version.replace(" BAR105", " bar").lower()


def find_7zip() -> str:
    """Return the path of a 7-Zip command line tool."""
    for name in ("7z", "7zz", "7za"):
        if tool := shutil.which(name):
            return tool
    raise PlaybackError("unpacking the engine needs 7-Zip: install 7z (p7zip-full or 7zip)")


def download(url: str, target: Path) -> bool:
    """Download url to target; False if the server has no such file or cannot be reached."""
    print(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "bar-replay-analytics"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as file:
            shutil.copyfileobj(response, file)
    except OSError as error:  # urllib's URLError and HTTPError are OSErrors
        print(f"  failed: {error}")
        return False
    print(f"  {target.stat().st_size / 1e6:.0f} MB")
    return True


def download_engine(engine_version: str, engine_dir: Path) -> None:
    """Download the engine archive and unpack it into engine_dir, which must not exist yet."""
    seven_zip = find_7zip()
    platform = "windows" if WINDOWS else "linux"
    tag = urllib.parse.quote(engine_version)
    urls = [ENGINE_CDN_URL.format(platform=platform, tag=tag)]
    if is_recoil_tag(engine_version):
        urls.append(ENGINE_GITHUB_URL.format(platform=platform, tag=tag))
    engine_dir.parent.mkdir(parents=True, exist_ok=True)
    # Unpack next to the target and rename at the end, so an interrupted run leaves no half engine behind.
    with tempfile.TemporaryDirectory(dir=engine_dir.parent, prefix=f".{engine_dir.name}-") as temp:
        archive = Path(temp) / "engine.7z"
        if not any(download(url, archive) for url in urls):
            raise PlaybackError(f"could not download engine {engine_version}; you can unpack it into {engine_dir}")
        unpacked = Path(temp) / "engine"
        result = subprocess.run(
            [seven_zip, "x", "-y", f"-o{unpacked}", archive],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise PlaybackError(f"unpacking {archive} failed:\n{result.stdout}{result.stderr}")
        if not (unpacked / SPRING_HEADLESS).is_file():
            raise PlaybackError(f"the engine archive has no {SPRING_HEADLESS}")
        unpacked.chmod(0o755)  # 7-Zip may create it private, like the temporary folder around it
        unpacked.rename(engine_dir)


def find_or_download_engine(data_dir: Path, engine_version: str) -> Path:
    """Return the spring-headless in the data dir's engine/<version folder>, downloaded if missing."""
    engine_dir = data_dir / "engine" / engine_folder_name(engine_version)
    if not (engine_dir / SPRING_HEADLESS).is_file():
        if engine_dir.exists():
            raise PlaybackError(f"{engine_dir} exists but has no {SPRING_HEADLESS}; remove it to download it again")
        download_engine(engine_version, engine_dir)
    return engine_dir / SPRING_HEADLESS


# Replay and widgets


def find_replay(data_dir: Path, name: str) -> Path:
    """Return the replay file name (".sdfz" optional) in the data dir's demos/."""
    demos_dir = data_dir / "demos"
    replay_file = demos_dir / (name if name.endswith(".sdfz") else f"{name}.sdfz")
    if not replay_file.is_file():
        raise PlaybackError(f"no replay {replay_file.name} in {demos_dir}")
    return replay_file


def find_widget_dir(widgets_dir: Path, widget_file: str) -> Path | None:
    """Return the folder in LuaUI/Widgets/ that holds widget_file; BAR loads user widgets one folder level deep."""
    widget = next(widgets_dir.glob(f"*/{widget_file}"), None)
    return widget.parent if widget else None


def find_analytics_widget(widgets_dir: Path) -> Path:
    """Return the folder of the analytics widget, installed like any user widget by cloning or linking this repo."""
    analytics_dir = find_widget_dir(widgets_dir, ANALYTICS_WIDGET)
    if not analytics_dir:
        raise PlaybackError(
            f"the analytics widget is not installed: no {ANALYTICS_WIDGET} in a folder of {widgets_dir}; clone or link"
            f" this repository there, e.g. ln -s {HERE} {widgets_dir / HERE.name}"
        )
    print_dir("bar-replay-analytics", analytics_dir)
    return analytics_dir


def print_advisor_status(widgets_dir: Path) -> None:
    """Say whether the bar-advisor widget is installed, as its [reminder] lines in the log depend on it."""
    if advisor_dir := find_widget_dir(widgets_dir, ADVISOR_WIDGET):
        print_dir("bar-advisor", advisor_dir)
    else:
        print_field("bar-advisor", f"not installed (no {ADVISOR_WIDGET} in a folder of LuaUI/Widgets/)")


# Playback


def last_frame(events: Path) -> int:
    """Return the game frame of the last complete event of the events file (one per line), 0 if there is none yet."""
    with events.open("rb") as file:
        # Large enough for an armyPositions event of a big army.
        file.seek(max(0, events.stat().st_size - 65536))
        lines = file.read().splitlines()
    for line in reversed(lines):
        try:
            return int(json.loads(line.removeprefix(b",").removesuffix(b","))["frame"])
        except (ValueError, KeyError, TypeError):  # a line cut off at the start of the window or still being written
            continue
    return 0


def stop_engine(process: subprocess.Popen) -> None:
    """Terminate the engine, and kill it if it does not exit within 10 s."""
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_engine(engine: Path, data_dir: Path, replay_file: Path, events: Path, log_file: Path) -> None:
    """Run the engine, print the progress, and stop it if the playback hangs.

    It hangs when the events file has not grown for STALL_SECONDS, or, before the analytics widget has created it while
    the engine loads, the engine log has not. The widget reports when it cannot write its file; then the replay would
    play in real time, so the engine is stopped at once.
    """
    print("playback:")
    start = time.monotonic()
    last_change = last_progress = start
    last_watched: tuple[Path, int] | None = None
    stop_reason = None
    with log_file.open("wb") as log:
        process = subprocess.Popen(
            [engine, "--isolation", "--write-dir", data_dir, replay_file],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=data_dir,
        )
        try:
            while process.poll() is None:
                time.sleep(1)
                now = time.monotonic()
                # Before the analytics widget has created its file, the engine is loading and only its log grows.
                events_started = events.exists()
                watched = events if events_started else log_file
                watched_size = watched.stat().st_size
                if (watched, watched_size) != last_watched:
                    last_change, last_watched = now, (watched, watched_size)
                if not events_started and ANALYTICS_FAILED in log_file.read_text(errors="replace"):
                    stop_reason = "the analytics widget could not write its events file"
                elif now - last_change > STALL_SECONDS:
                    stop_reason = f"no {'events' if events_started else 'engine output'} for {STALL_SECONDS} s"
                if stop_reason:
                    break
                if now - last_progress >= PROGRESS_SECONDS:
                    last_progress = now
                    if events_started:
                        minutes, seconds = divmod(last_frame(events) // GAME_SPEED, 60)
                        print(f"  {now - start:4.0f} s: game time {minutes}:{seconds:02d}")
                    else:
                        print(f"  {now - start:4.0f} s: loading")
        finally:  # also on Ctrl+C, so no engine is left running
            if process.poll() is None:
                stop_engine(process)
    print_field("engine exit", f"stopped, {stop_reason}" if stop_reason else f"exit code {process.returncode}")
    print_field("duration", f"{time.monotonic() - start:.0f} s")
    print_field("engine log", log_file)


def print_events_summary(events: Path) -> None:
    """Print how many events the analytics widget wrote and why it stopped ("gameover" if the game ended)."""
    with events.open(encoding="utf-8") as file:
        try:
            document = json.load(file)
        except json.JSONDecodeError:  # no end: the engine crashed or was stopped
            document = {}
    print_field("events", len(document["events"]) if "events" in document else "-")
    print_field("events file", events)
    if "end" in document:
        end = document["end"]
        print_field("end", f"{end['reason']} at frame {end['frame']}")
    else:
        print_field("end", "none, the events file is incomplete")


def play_replay(data_dir: Path, replay_name: str) -> None:
    """Play the replay back in the data dir and print where the analytics widget's events went."""
    print_dir("data dir", data_dir)
    replay_file = find_replay(data_dir, replay_name)
    widgets_dir = data_dir / "LuaUI" / "Widgets"
    analytics_dir = find_analytics_widget(widgets_dir)
    print_advisor_status(widgets_dir)

    replay = sdfz_stats.load(replay_file)
    game_setup = sdfz_stats.parse_script(replay["script"])["game"]  # the start script's [GAME] section
    engine_version = check_name("engine version", replay["engine"])
    game_type = check_name("gametype", game_setup.get("gametype", ""))
    map_name = check_name("mapname", game_setup.get("mapname", ""))
    print_field("map", map_name)
    print_field("game", game_type)
    print_field("engine", engine_version)

    engine = find_or_download_engine(data_dir, engine_version)
    game = find_or_download_game(data_dir, game_type, engine)
    map_archive = find_or_download_map(data_dir, map_name, engine)
    print_field("map archive", map_archive)
    print_field("game package", game)
    print_field("engine dir", engine.parent)

    # The analytics widget names its output after the replay (Game.demoPlayName, the file name without .sdfz).
    events = analytics_dir / "events" / f"{replay_file.stem}_events.json"
    events.parent.mkdir(exist_ok=True)
    events.unlink(missing_ok=True)
    run_engine(engine, data_dir, replay_file, events, events.with_name(f"{replay_file.stem}.log"))

    if not events.exists():
        raise PlaybackError(f"no {events.name} was written; check the log")
    print_events_summary(events)


def main() -> None:
    """Parse the command line and play the replay back."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("replay", help="file name of the replay in the data dir's demos/ (.sdfz optional)")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="BAR data dir to work in (default: the one this repository is cloned into as a widget, else data/ next"
        " to this script)",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # progress lines show at once also when piped or redirected
    try:
        play_replay(args.data_dir.resolve(), args.replay)
    except PlaybackError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
