import os
import re
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime

import psutil
import requests

from config_manager import load_config


DEFAULT_PLACE_ID = "PUT_PLACE_ID_HERE"
INTERNET_ENDPOINTS = (
    ("www.roblox.com", 443),
    ("apis.roblox.com", 443),
)
DISCONNECT_MARKERS = (
    "failed to connect to the experience",
    "connection lost",
    "lost connection",
    "error code 277",
    "error code: 277",
    "error code 279",
    "error code: 279",
    "error code 268",
    "error code: 268",
    "error code 273",
    "error code: 273",
    "disconnected for being idle",
    "idled for 20 minutes",
    "you were kicked from this experience",
    "disconnect reason received",
    "game disconnected",
)

PLACE_ID = DEFAULT_PLACE_ID
REJOIN_DELAY = 5.0
CHECK_INTERVAL = 3.0
REJOIN_RETRY_DELAY = 10.0
MAX_REJOIN_RETRY_DELAY = 60.0
ROBLOX_START_TIMEOUT = 90.0
ROBLOX_STABLE_TIME = 30.0
DISCONNECT_TRANSITION_GRACE_SECONDS = 10.0
GAME_NAME = "Not Set"
GAME_URL = "https://www.roblox.com/games/"
UNIVERSE_ID: int | None = None

MONITOR_ACTIVE = True


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")


def _positive_number(value, fallback: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return fallback


def _valid_place_id(place_id: str) -> bool:
    return bool(place_id and place_id != DEFAULT_PLACE_ID and place_id.isdigit())


def get_universe_id(place_id: str) -> int | None:
    """Resolve a Roblox place to its containing experience."""
    if not _valid_place_id(place_id):
        return None

    headers = {"User-Agent": "Roblox-Auto-Rejoiner/1.0"}
    try:
        response = requests.get(
            f"https://apis.roblox.com/universes/v1/places/{place_id}/universe",
            headers=headers,
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        universe_id = payload.get("universeId") if isinstance(payload, dict) else None
        if isinstance(universe_id, str) and universe_id.isdigit():
            universe_id = int(universe_id)
        if isinstance(universe_id, int) and not isinstance(universe_id, bool):
            return universe_id if universe_id > 0 else None
    except (requests.RequestException, ValueError, TypeError) as exc:
        log(f"Could not fetch the game universe: {exc}")

    return None


def get_game_name(place_id: str, universe_id: int | None = None) -> str:
    """Return the Roblox experience name, with the place ID as a safe fallback."""
    if not _valid_place_id(place_id):
        return "Not Set"

    headers = {"User-Agent": "Roblox-Auto-Rejoiner/1.0"}
    try:
        universe_id = universe_id or get_universe_id(place_id)
        if universe_id:
            response = requests.get(
                f"https://games.roblox.com/v1/games?universeIds={universe_id}",
                headers=headers,
                timeout=5,
            )
            response.raise_for_status()
            games = response.json().get("data", [])
            if games and games[0].get("name"):
                return games[0]["name"]
    except (requests.RequestException, ValueError, TypeError) as exc:
        log(f"Could not fetch the game name: {exc}")
    return f"Place {place_id}"


def get_game_icon_url(universe_id: int) -> str | None:
    """Return a Roblox experience's icon URL."""
    if not isinstance(universe_id, int) or isinstance(universe_id, bool):
        return None
    if universe_id <= 0:
        return None

    try:
        response = requests.get(
            "https://thumbnails.roblox.com/v1/games/icons",
            params={
                "universeIds": str(universe_id),
                "size": "150x150",
                "format": "Png",
                "isCircular": "false",
            },
            headers={"User-Agent": "Roblox-Auto-Rejoiner/1.0"},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        thumbnails = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(thumbnails, list):
            raise ValueError("Roblox returned an unexpected game-icon response.")
        for thumbnail in thumbnails:
            if (
                isinstance(thumbnail, dict)
                and thumbnail.get("targetId") == universe_id
                and isinstance(thumbnail.get("imageUrl"), str)
            ):
                return thumbnail["imageUrl"]
    except (requests.RequestException, ValueError, TypeError) as exc:
        log(f"Could not fetch the game icon: {exc}")

    return None


def search_games(query: str, limit: int = 20) -> list[dict[str, object]]:
    """Search Roblox experiences and return selectable root Place IDs."""
    query = query.strip()
    if not query:
        raise ValueError("Enter a game name to search.")
    if limit < 1:
        raise ValueError("Search result limit must be positive.")

    session_id = str(uuid.uuid4())
    response = requests.get(
        "https://apis.roblox.com/search-api/omni-search",
        params={
            "searchQuery": query,
            "pageType": "Games",
            "sessionId": session_id,
            "globalSessionId": session_id,
            "pageToken": "",
        },
        headers={"User-Agent": "Roblox-Auto-Rejoiner/1.0"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Roblox returned an unexpected game-search response.")
    groups = payload.get("searchResults")
    if not isinstance(groups, list):
        raise ValueError("Roblox returned an unexpected game-search response.")

    games = []
    universe_ids = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        contents = group.get("contents")
        if not isinstance(contents, list):
            continue
        for game in contents:
            if not isinstance(game, dict) or game.get("contentType") != "Game":
                continue
            name = game.get("name")
            place_id = game.get("rootPlaceId")
            if not isinstance(name, str) or not name.strip():
                continue
            if isinstance(place_id, bool):
                continue
            if isinstance(place_id, str) and place_id.isdigit():
                place_id = int(place_id)
            if not isinstance(place_id, int) or place_id <= 0:
                continue
            universe_id = game.get("universeId")
            if not isinstance(universe_id, int) or isinstance(universe_id, bool):
                universe_id = 0
            if universe_id > 0:
                universe_ids.append(universe_id)
            creator = game.get("creatorName")
            player_count = game.get("playerCount")
            if not isinstance(creator, str) or not creator.strip():
                creator = "Unknown creator"
            if not isinstance(player_count, int) or isinstance(player_count, bool):
                player_count = 0
            games.append(
                {
                    "name": name.strip(),
                    "place_id": str(place_id),
                    "creator": creator.strip(),
                    "player_count": max(0, player_count),
                    "universe_id": universe_id or None,
                    "icon_url": None,
                }
            )
            if len(games) >= limit:
                break
        if len(games) >= limit:
            break

    if not games or not universe_ids:
        return games

    try:
        response = requests.get(
            "https://thumbnails.roblox.com/v1/games/icons",
            params={
                "universeIds": ",".join(str(universe_id) for universe_id in universe_ids),
                "size": "150x150",
                "format": "Png",
                "isCircular": "false",
            },
            headers={"User-Agent": "Roblox-Auto-Rejoiner/1.0"},
            timeout=10,
        )
        response.raise_for_status()
        thumbnail_payload = response.json()
        thumbnails = thumbnail_payload.get("data") if isinstance(thumbnail_payload, dict) else None
        if not isinstance(thumbnails, list):
            raise ValueError("Roblox returned an unexpected game-icon response.")
        icon_urls = {
            thumbnail["targetId"]: thumbnail["imageUrl"]
            for thumbnail in thumbnails
            if isinstance(thumbnail, dict)
            and isinstance(thumbnail.get("targetId"), int)
            and isinstance(thumbnail.get("imageUrl"), str)
        }
        for game in games:
            if game["universe_id"] is not None:
                game["icon_url"] = icon_urls.get(game["universe_id"])
    except (requests.RequestException, ValueError, TypeError) as exc:
        log(f"Could not fetch game icons: {exc}")

    return games
    return games


state = {
    "monitoring": True,
    "roblox_running": False,
    "crash_count": 0,
    "freeze_count": 0,
    "disconnect_count": 0,
    "last_crash": None,
    "last_freeze": None,
    "last_disconnect": None,
    "last_disconnect_reason": None,
    "last_rejoin": None,
    "rejoin_count": 0,
    "session_started_monotonic": None,
    "session_elapsed_seconds": 0,
    "monitor_start": datetime.now().isoformat(),
    "place_id": PLACE_ID,
    "game_name": GAME_NAME,
    "game_url": GAME_URL,
    "status_message": "Starting up...",
}
state_lock = threading.Lock()
_recovery_lock = threading.Lock()


def reload_monitor_config(fetch_game_name: bool = False) -> None:
    """Reload monitor settings without re-importing the module."""
    global PLACE_ID, REJOIN_DELAY, CHECK_INTERVAL, GAME_NAME, GAME_URL, UNIVERSE_ID

    config = load_config()
    PLACE_ID = str(config.get("PLACE_ID", DEFAULT_PLACE_ID)).strip()
    REJOIN_DELAY = _positive_number(config.get("REJOIN_DELAY"), 5.0)
    CHECK_INTERVAL = _positive_number(config.get("CHECK_INTERVAL"), 3.0, 0.5)
    if fetch_game_name:
        UNIVERSE_ID = get_universe_id(PLACE_ID)
        GAME_NAME = get_game_name(PLACE_ID, UNIVERSE_ID)
    else:
        UNIVERSE_ID = None
        GAME_NAME = "Not Set" if not _valid_place_id(PLACE_ID) else f"Place {PLACE_ID}"
    GAME_URL = f"https://www.roblox.com/games/{PLACE_ID}"

    with state_lock:
        state.update(
            place_id=PLACE_ID,
            game_name=GAME_NAME,
            game_url=GAME_URL,
        )


reload_monitor_config()


class RobloxLogMonitor:
    """Tails only new Roblox Player log data and reports disconnect markers."""

    def __init__(self, log_dir: str | None = None):
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        self.log_dir = log_dir or os.path.join(local_app_data, "Roblox", "logs")
        self.current_log: str | None = None
        self.position = 0
        self.pending_line = ""
        self.join_detected = False
        self.pending_disconnect_reason: str | None = None
        self.pending_disconnect_at: float | None = None
        self.last_join_universe_id: int | None = None
        self.last_join_at: float | None = None
        self.universe_ids: dict[str, int | None] = {PLACE_ID: UNIVERSE_ID}
        self._select_latest(start_at_end=True)

    def _get_universe_id(self, place_id: str) -> int | None:
        if place_id not in self.universe_ids:
            self.universe_ids[place_id] = get_universe_id(place_id)
        return self.universe_ids[place_id]

    def _take_expired_disconnect(self, now: float) -> str | None:
        if (
            self.pending_disconnect_reason is not None
            and self.pending_disconnect_at is not None
            and now - self.pending_disconnect_at
            >= DISCONNECT_TRANSITION_GRACE_SECONDS
        ):
            reason = self.pending_disconnect_reason
            self.pending_disconnect_reason = None
            self.pending_disconnect_at = None
            return reason
        return None

    def _record_disconnect_marker(self, line: str, now: float) -> None:
        for marker in DISCONNECT_MARKERS:
            if marker not in line:
                continue
            same_experience_join = (
                UNIVERSE_ID is not None
                and self.last_join_universe_id == UNIVERSE_ID
                and self.last_join_at is not None
                and now - self.last_join_at
                <= DISCONNECT_TRANSITION_GRACE_SECONDS
            )
            if same_experience_join:
                log(
                    "Ignoring a transient disconnect during a teleport "
                    "within the configured experience."
                )
                self.pending_disconnect_reason = None
                self.pending_disconnect_at = None
            elif self.pending_disconnect_reason is None:
                self.pending_disconnect_reason = marker
                self.pending_disconnect_at = now

    def get_latest_log(self) -> str | None:
        try:
            entries = [
                os.path.join(self.log_dir, name)
                for name in os.listdir(self.log_dir)
                if name.lower().endswith(".log")
            ]
            player_logs = [
                path
                for path in entries
                if "player" in os.path.basename(path).lower()
                and "crashhandler" not in os.path.basename(path).lower()
            ]
            candidates = player_logs or entries
            return max(candidates, key=os.path.getmtime) if candidates else None
        except (FileNotFoundError, OSError):
            return None

    def _select_latest(self, start_at_end: bool) -> bool:
        latest = self.get_latest_log()
        if not latest:
            return False
        self.current_log = latest
        self.pending_line = ""
        try:
            self.position = os.path.getsize(latest) if start_at_end else 0
        except OSError:
            self.position = 0
        return True

    def check_for_disconnect(self) -> str | None:
        now = time.monotonic()
        latest = self.get_latest_log()
        if not latest:
            return self._take_expired_disconnect(now)
        if latest != self.current_log:
            self.current_log = latest
            self.position = 0
            self.pending_line = ""

        try:
            size = os.path.getsize(latest)
            if size < self.position:
                self.position = 0
                self.pending_line = ""

            with open(latest, "rb") as log_file:
                log_file.seek(self.position)
                new_data = log_file.read(512 * 1024)
                self.position = log_file.tell()
        except OSError:
            return self._take_expired_disconnect(now)

        if not new_data:
            return self._take_expired_disconnect(now)

        text = self.pending_line + new_data.decode("utf-8", errors="ignore")
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.pending_line = lines.pop()
        else:
            self.pending_line = ""

        for line in lines:
            lowered = line.lower()
            if "joining game" in lowered and "place " in lowered:
                self.join_detected = True
                match = re.search(r"\bplace\s+(\d+)\b", lowered)
                if match:
                    self.last_join_universe_id = self._get_universe_id(match.group(1))
                    self.last_join_at = now
                    if (
                        self.pending_disconnect_reason is not None
                        and UNIVERSE_ID is not None
                        and self.last_join_universe_id == UNIVERSE_ID
                    ):
                        log(
                            "Ignoring a transient disconnect during a teleport "
                            "within the configured experience."
                        )
                        self.pending_disconnect_reason = None
                        self.pending_disconnect_at = None
            self._record_disconnect_marker(lowered, now)

        if self.pending_line:
            self._record_disconnect_marker(self.pending_line.lower(), now)

        return self._take_expired_disconnect(now)

    def reset_join_detection(self) -> None:
        self.join_detected = False


def is_roblox_running() -> bool:
    for process in psutil.process_iter(["name"]):
        try:
            name = process.info.get("name") or ""
            if name.lower() == "robloxplayerbeta.exe":
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def is_roblox_frozen() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            [
                "tasklist",
                "/FI",
                "IMAGENAME eq RobloxPlayerBeta.exe",
                "/FI",
                "STATUS eq NOT RESPONDING",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "RobloxPlayerBeta.exe" in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def kill_roblox() -> None:
    log("Force-closing Roblox processes...")
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "RobloxPlayerBeta.exe", "/T"],
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Could not close Roblox: {exc}")


def launch_roblox() -> bool:
    if not _valid_place_id(PLACE_ID):
        log("Place ID is not configured; Roblox was not launched.")
        return False

    uri = f"roblox://placeId={PLACE_ID}"
    log(f"Launching Roblox -> Place ID {PLACE_ID}")
    try:
        os.startfile(uri)
        return True
    except (AttributeError, OSError) as exc:
        log(f"Roblox URI launch failed: {exc}")

    versions_dir = os.path.expandvars(r"%LOCALAPPDATA%\Roblox\Versions")
    try:
        executables = []
        for version in os.scandir(versions_dir):
            executable = os.path.join(version.path, "RobloxPlayerBeta.exe")
            if version.is_dir() and os.path.isfile(executable):
                executables.append(executable)
        executable = max(executables, key=os.path.getmtime)
        subprocess.Popen([executable, "--app", "--launchtime=0", uri])
        return True
    except (OSError, ValueError) as exc:
        log(f"Direct Roblox launch failed: {exc}")
        return False


def internet_available(timeout: float = 3.0) -> bool:
    """Check that a Roblox endpoint is reachable, including working DNS."""
    for endpoint in INTERNET_ENDPOINTS:
        try:
            with socket.create_connection(endpoint, timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _monitoring_enabled() -> bool:
    with state_lock:
        return bool(state["monitoring"])


def _controlled_delay(seconds: float, pause_aware: bool = True) -> bool:
    deadline = time.monotonic() + seconds
    while MONITOR_ACTIVE and time.monotonic() < deadline:
        if pause_aware and not _monitoring_enabled():
            time.sleep(0.25)
            deadline += 0.25
            continue
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return MONITOR_ACTIVE


def wait_for_internet(pause_aware: bool = True) -> bool:
    announced = False
    while MONITOR_ACTIVE:
        if pause_aware and not _monitoring_enabled():
            time.sleep(0.25)
            continue
        if internet_available():
            if announced:
                log("Internet connection restored.")
            return True
        if not announced:
            log("No Roblox network connection. Waiting before relaunching...")
            announced = True
        with state_lock:
            state["status_message"] = "Waiting for internet..."
        if not _controlled_delay(5, pause_aware=pause_aware):
            break
    return False


def _wait_for_roblox(
    timeout: float = ROBLOX_START_TIMEOUT,
    pause_aware: bool = True,
    log_monitor: RobloxLogMonitor | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    running_since = None
    while MONITOR_ACTIVE:
        now = time.monotonic()
        if now >= deadline:
            break
        if pause_aware and not _monitoring_enabled():
            time.sleep(0.25)
            deadline += 0.25
            if running_since is not None:
                running_since += 0.25
            continue
        if log_monitor:
            disconnect_reason = log_monitor.check_for_disconnect()
            if disconnect_reason:
                log(f"Disconnect detected while joining ({disconnect_reason}).")
                return False
        joined_game = log_monitor is None or log_monitor.join_detected
        if joined_game and is_roblox_running():
            if running_since is None:
                running_since = now
            elif now - running_since >= ROBLOX_STABLE_TIME:
                return True
        else:
            running_since = None
        if not _controlled_delay(1, pause_aware=pause_aware):
            return False
    return False


def recover_roblox(
    reason: str,
    pause_aware: bool = True,
    log_monitor: RobloxLogMonitor | None = None,
    initial_join: bool = False,
) -> bool:
    """Keep retrying failed joins until one succeeds or monitoring stops."""
    if not _recovery_lock.acquire(blocking=False):
        return is_roblox_running()
    try:
        if initial_join:
            log("Starting the configured game join...")
        else:
            with state_lock:
                state["status_message"] = f"{reason}; rejoining in {REJOIN_DELAY:g}s..."
            log(f"Waiting {REJOIN_DELAY:g} seconds before rejoining...")
            if not _controlled_delay(REJOIN_DELAY, pause_aware=pause_aware):
                return False

        attempt = 1
        while MONITOR_ACTIVE:
            if not _valid_place_id(PLACE_ID):
                with state_lock:
                    state["status_message"] = "Invalid or missing Place ID"
                return False
            if not wait_for_internet(pause_aware=pause_aware):
                return False

            client_was_running = is_roblox_running()
            if log_monitor:
                log_monitor.reset_join_detection()
            with state_lock:
                state["status_message"] = (
                    f"Launching Roblox (attempt {attempt})..."
                )
            launch_requested = launch_roblox()
            if launch_requested and _wait_for_roblox(
                pause_aware=pause_aware,
                log_monitor=log_monitor,
            ):
                with state_lock:
                    state["last_rejoin"] = datetime.now().isoformat()
                    state["roblox_running"] = True
                    if not initial_join:
                        state["rejoin_count"] += 1
                    rejoin_count = state["rejoin_count"]
                    state["status_message"] = (
                        "Roblox client running - Monitoring"
                        if state["monitoring"]
                        else "Roblox client running - Paused"
                    )
                if initial_join:
                    log("Initial game join succeeded.")
                else:
                    log(f"Rejoined successfully ({rejoin_count} total).")
                return True

            if launch_requested and not client_was_running and is_roblox_running():
                log("Closing the failed Roblox launch before retrying.")
                kill_roblox()
                if not _controlled_delay(2, pause_aware=pause_aware):
                    return False

            if not launch_requested:
                failure_reason = "Roblox launch request failed"
            elif log_monitor and not log_monitor.join_detected:
                failure_reason = (
                    "Roblox opened but did not log a game join; check its sign-in, "
                    "network connection, and access to the configured experience"
                )
            else:
                failure_reason = "Roblox closed or disconnected before startup completed"
            retry_delay = min(
                REJOIN_RETRY_DELAY * (2 ** min(attempt - 1, 10)),
                MAX_REJOIN_RETRY_DELAY,
            )
            with state_lock:
                state["status_message"] = (
                    f"{failure_reason}; retrying in {retry_delay:g}s "
                    f"(attempt {attempt})..."
                )
            log(
                f"{failure_reason}. Retrying in {retry_delay:g} seconds "
                f"(attempt {attempt})..."
            )
            if not _controlled_delay(retry_delay, pause_aware=pause_aware):
                return False
            attempt += 1
        return False
    finally:
        _recovery_lock.release()


def monitor_loop(initial_join: bool = False) -> None:
    if not MONITOR_ACTIVE:
        return
    reload_monitor_config(fetch_game_name=True)
    log_monitor = RobloxLogMonitor()
    log("Monitor started. Watching for Roblox events...")

    with state_lock:
        state["monitor_start"] = datetime.now().isoformat()
        state["status_message"] = "Monitoring"
        state["rejoin_count"] = 0
        state["session_started_monotonic"] = time.monotonic()
        state["session_elapsed_seconds"] = 0

    was_running = is_roblox_running()
    with state_lock:
        state["roblox_running"] = was_running
    if was_running:
        log("Roblox is already open.")
    elif initial_join:
        was_running = recover_roblox(
            "Initial join",
            log_monitor=log_monitor,
            initial_join=True,
        )

    while MONITOR_ACTIVE:
        if not _monitoring_enabled():
            with state_lock:
                state["status_message"] = "Paused"
            time.sleep(0.25)
            continue

        now_running = is_roblox_running()
        with state_lock:
            state["roblox_running"] = now_running
            if state["status_message"] == "Paused":
                state["status_message"] = "Monitoring"

        if now_running and is_roblox_frozen():
            event_time = datetime.now().isoformat()
            log(f"Roblox is not responding (detected {event_time}).")
            with state_lock:
                state["freeze_count"] += 1
                state["last_freeze"] = event_time
                state["status_message"] = "Frozen; force-closing..."
            kill_roblox()
            _controlled_delay(2)
            was_running = recover_roblox("Frozen", log_monitor=log_monitor)
            continue

        disconnect_reason = log_monitor.check_for_disconnect() if now_running else None
        if now_running and disconnect_reason:
            event_time = datetime.now().isoformat()
            log(f"Roblox disconnect detected ({disconnect_reason}) at {event_time}.")
            with state_lock:
                state["disconnect_count"] += 1
                state["last_disconnect"] = event_time
                state["last_disconnect_reason"] = disconnect_reason
                state["status_message"] = "Disconnected; force-closing..."
            kill_roblox()
            _controlled_delay(2)
            was_running = recover_roblox("Disconnected", log_monitor=log_monitor)
            continue

        if was_running and not now_running:
            event_time = datetime.now().isoformat()
            log(f"Roblox closed unexpectedly at {event_time}.")
            with state_lock:
                state["crash_count"] += 1
                state["last_crash"] = event_time
            was_running = recover_roblox("Crashed", log_monitor=log_monitor)
            continue

        was_running = now_running
        _controlled_delay(CHECK_INTERVAL)

    with state_lock:
        state["roblox_running"] = is_roblox_running()
        state["status_message"] = "Stopped"
        session_started = state["session_started_monotonic"]
        if session_started is not None:
            state["session_elapsed_seconds"] = max(
                0,
                int(time.monotonic() - session_started),
            )
        state["session_started_monotonic"] = None
    log("Monitor stopped.")


def main() -> None:
    print("=" * 50)
    print("  Roblox Auto-Rejoin Monitor")
    print(f"  Place: {PLACE_ID}")
    print("=" * 50)
    try:
        monitor_loop()
    except KeyboardInterrupt:
        globals()["MONITOR_ACTIVE"] = False
        log("Stopped by user.")


if __name__ == "__main__":
    main()
