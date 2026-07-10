#!/usr/bin/env python3
"""
BMTC Bus Tracker

Monitors a BMTC bus by calling BMTC's internal APIs and generates
notifications when the tracking becomes stale.

Run with -h for usage info.
"""

import atexit
import io
import json
import os
import subprocess
import sys
import time
import warnings
from argparse import ArgumentParser
from datetime import datetime, timedelta
from typing import Any, Optional

warnings.filterwarnings("ignore", message=".*urllib3.*doesn't match a supported version")

import requests


################################################################################
# Constants
################################################################################

VERSION = "0.9.0"
HTTP_TIMEOUT = 10
LIST_VEHICLES_URL = "https://bmtcmobileapi.karnataka.gov.in/WebAPI/ListVehicles"
TRIP_DETAILS_URL = "https://bmtcmobileapi.karnataka.gov.in/WebAPI/VehicleTripDetails_v2"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://nammabmtcapp.karnataka.gov.in",
    "Referer": "https://nammabmtcapp.karnataka.gov.in/",
    "User-Agent": "Mozilla/5.0",
    "deviceType": "WEB",
    "lan": "en",
}

DAY_NAMES = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASH_CMDS_DIR = "/home/snarangaprath/WORK/BASH_CMDS"
DEVELOPMENT_MODE = False


################################################################################
# Tracker State
################################################################################

TRACKER_RUNNING = "running"
TRACKER_IDLE = "idle"
TRACKER_OFFLINE = "offline"


################################################################################
# Logging Helpers
################################################################################

_verbose = False
_show_http_msgs = False


def log(message: str = "") -> None:
    """Print a message to stdout and write to the log file."""
    print(message)
    file_log(message)


def log_error(message: str) -> None:
    """Print an error message to stderr and write to the log file."""
    print(message, file=sys.stderr)
    file_log(message)


def log_separator() -> None:
    """Print a separator line."""
    log("=" * 56)


def print_header(title: str) -> None:
    """Print a section header with separator."""
    log_separator()
    log(title)
    log_separator()


def print_section(title: str) -> None:
    """Print a labelled section."""
    log(title)


def print_key_value(key: str, value: Any) -> None:
    """Print a labelled value pair."""
    log(f"{key:<18}: {value}")


def print_arrow() -> None:
    """Print a downward arrow."""
    log("  \u2193")


def print_blank() -> None:
    """Print a blank line."""
    log()


################################################################################
# File Logging
################################################################################


def _close_log() -> None:
    """Close the log file handle if open."""
    global _log_fp
    if _log_fp is not None:
        _log_fp.close()
        _log_fp = None


def _rotate_log() -> None:
    """Trim the log file to _log_trim_to_lines lines when it exceeds _log_max_lines."""
    global _log_fp, _log_line_count
    _close_log()
    try:
        with open(_log_file_path, "r") as f:
            lines = f.readlines()
        keep = lines[-_log_trim_to_lines:]
        with open(_log_file_path, "w") as f:
            f.writelines(keep)
    except (OSError, IOError):
        pass
    _log_fp = open(_log_file_path, "a")
    _log_line_count = _log_trim_to_lines


def file_log(message: str) -> None:
    """Write a timestamped line to the log file. Ignores empty messages."""
    global _log_fp, _log_line_count
    if _log_fp is None or message == "":
        return
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n"
    _log_fp.write(line)
    _log_fp.flush()
    _log_line_count += 1
    if _log_line_count > _log_max_lines:
        _rotate_log()


def init_logging(config: dict[str, Any]) -> None:
    """Initialize the log file from config: count existing lines and open for appending."""
    global _log_fp, _log_line_count, _log_file_path, _log_enabled, _log_max_lines, _log_trim_to_lines

    log_cfg = config.get("log", {})
    _log_enabled = log_cfg.get("enabled", True)
    _log_max_lines = log_cfg.get("max_lines", 2000)
    _log_trim_to_lines = log_cfg.get("trim_to_lines", 1000)

    if not _log_enabled:
        _log_fp = None
        return

    filename = log_cfg.get("file", "bmtc_tracker.log")
    _log_file_path = os.path.join(SCRIPT_DIR, filename)
    if os.path.isfile(_log_file_path):
        try:
            with open(_log_file_path, "r") as f:
                for _ in f:
                    _log_line_count += 1
        except (OSError, IOError):
            _log_line_count = 0
    _log_fp = open(_log_file_path, "a")
    atexit.register(_close_log)


################################################################################
# Configuration
################################################################################


def load_config(config_path: str) -> dict[str, Any]:
    """Load configuration from a JSON file."""
    with open(config_path, "r") as f:
        return json.load(f)


def find_config() -> str:
    """Locate config.json in the current directory or script directory."""
    candidates = [
        os.path.join(os.getcwd(), "config.json"),
        os.path.join(SCRIPT_DIR, "config.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    log_error("Error: config.json not found.")
    sys.exit(1)


def parse_cli_args() -> Any:
    """Parse command-line arguments."""
    parser = ArgumentParser(description="BMTC Bus Tracker")
    parser.add_argument(
        "--bus-num",
        dest="bus_num",
        default=None,
        help="Override bus number from config.json",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        dest="verbose",
        default=False,
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version and exit",
    )
    parser.add_argument(
        "--always-track",
        action="store_true",
        dest="always_track",
        default=False,
        help="Ignore schedule and track continuously",
    )
    parser.add_argument(
        "--show-http-msgs",
        action="store_true",
        dest="show_http_msgs",
        default=False,
        help="Print HTTP request and response messages",
    )
    return parser.parse_args()


def validate_config(config: dict[str, Any]) -> None:
    """Validate required configuration fields, exiting on failure."""
    required = ["bus_number", "poll_interval_secs", "offline_after_mins", "schedule"]
    for key in required:
        if key not in config:
            log_error(f"Error: config.json missing required key '{key}'.")
            sys.exit(1)
    if not isinstance(config["schedule"], list) or len(config["schedule"]) == 0:
        log_error("Error: config.json 'schedule' must be a non-empty list.")
        sys.exit(1)
    for entry in config["schedule"]:
        for key in ("name", "enabled", "days", "start", "end",
                     "source", "destination", "alert"):
            if key not in entry:
                log_error(f"Error: schedule entry missing '{key}'.")
                sys.exit(1)
        for day in entry["days"]:
            if day not in DAY_NAMES:
                log_error(
                    f"Error: invalid day '{day}' in schedule entry "
                    f"'{entry['name']}'."
                )
                sys.exit(1)
        try:
            datetime.strptime(entry["start"], "%H:%M")
            datetime.strptime(entry["end"], "%H:%M")
        except ValueError:
            log_error(
                f"Error: invalid time format in schedule entry '{entry['name']}'."
            )
            sys.exit(1)
        alert = entry["alert"]
        if not isinstance(alert, dict):
            log_error(f"Error: 'alert' in schedule entry '{entry['name']}' must be an object.")
            sys.exit(1)
        for key in ("alert_start_location", "alert_end_location", "notification"):
            if key not in alert:
                log_error(
                    f"Error: schedule entry '{entry['name']}' alert missing '{key}'."
                )
                sys.exit(1)


################################################################################
# BMTC API Helpers
################################################################################


def _api_post(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
) -> Any:
    """Make an API POST request with standard headers and timeout."""
    if _show_http_msgs:
        print_blank()
        log("--- HTTP REQUEST ---")
        log(f"POST {url}")
        log("Headers:")
        for k, v in HEADERS.items():
            log(f"  {k}: {v}")
        log("Body:")
        for line in json.dumps(payload, indent=2).splitlines():
            log(f"  {line}")

    resp = session.post(url, headers=HEADERS, json=payload, timeout=HTTP_TIMEOUT)

    if _show_http_msgs:
        print_blank()
        log("--- HTTP RESPONSE ---")
        log(f"Status: {resp.status_code}")
        log("Headers:")
        for k, v in resp.headers.items():
            log(f"  {k}: {v}")
        log("Body:")
        try:
            pretty = json.dumps(resp.json(), indent=2)
            for line in pretty.splitlines():
                log(f"  {line}")
        except Exception:
            for line in resp.text.splitlines():
                log(f"  {line}")

    resp.raise_for_status()
    return resp.json()


def fetch_trip_details(
    session: requests.Session, vehicle_id: int
) -> Optional[dict[str, Any]]:
    """
    Fetch live trip details for a given vehicle ID.

    Retries exactly once on failure.
    Returns the parsed JSON dict, or None if both attempts fail.
    """
    payload: dict[str, Any] = {"vehicleId": vehicle_id}
    for attempt in range(2):
        try:
            return _api_post(session, TRIP_DETAILS_URL, payload)
        except (requests.RequestException, json.JSONDecodeError) as e:
            if attempt == 0:
                if _verbose:
                    log_error(f"Retrying after error: {e}")
                continue
            if _verbose:
                log_error(f"Error: trip details fetch failed: {e}")
            return None
    return None


def resolve_vehicle_id(session: requests.Session, bus_num: str) -> int:
    """
    Resolve a vehicle ID from a bus registration number.

    Calls the ListVehicles API and finds the matching vehicle.
    Exits on failure.
    """
    payload: dict[str, Any] = {"vehicleRegNo": bus_num}
    try:
        body = _api_post(session, LIST_VEHICLES_URL, payload)
    except (requests.RequestException, json.JSONDecodeError) as e:
        log_error(f"Error: vehicle lookup failed: {e}")
        sys.exit(1)

    if not isinstance(body, dict):
        log_error("Error: unexpected response format from ListVehicles.")
        sys.exit(1)

    vehicles = body.get("data", [])
    if not isinstance(vehicles, list):
        log_error("Error: 'data' field is not a list.")
        sys.exit(1)

    for vehicle in vehicles:
        if not isinstance(vehicle, dict):
            continue
        reg_no = vehicle.get("vehicleregno", "").strip().upper()
        if reg_no == bus_num.strip().upper():
            vid = vehicle.get("vehicleid")
            if vid is None:
                log_error("Error: vehicle found but 'vehicleid' missing.")
                sys.exit(1)
            return int(vid)

    log_error(f"Error: vehicle '{bus_num}' not found in API response.")
    sys.exit(1)


################################################################################
# BMTC Response Accessors
################################################################################


def get_route_details(trip_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract RouteDetails list from a trip data response."""
    raw = trip_data.get("RouteDetails")
    if isinstance(raw, list):
        return raw
    return []


def get_live_location(trip_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract LiveLocation list from a trip data response."""
    raw = trip_data.get("LiveLocation")
    if isinstance(raw, list):
        return raw
    return []


def get_first_route(trip_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the first RouteDetails entry, or None."""
    routes = get_route_details(trip_data)
    if routes:
        return routes[0]
    return None


def get_first_location(trip_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the first LiveLocation entry, or None."""
    locations = get_live_location(trip_data)
    if locations:
        return locations[0]
    return None


def get_source_station(trip_data: dict[str, Any]) -> Optional[str]:
    """Return the source station from RouteDetails, or None."""
    route = get_first_route(trip_data)
    if route:
        return route.get("sourcestation")
    return None


def get_destination_station(trip_data: dict[str, Any]) -> Optional[str]:
    """Return the destination station from RouteDetails, or None."""
    route = get_first_route(trip_data)
    if route:
        return route.get("destinationstation")
    return None


def get_previous_stop(trip_data: dict[str, Any]) -> Optional[str]:
    """Return the previous stop from LiveLocation, or None."""
    loc = get_first_location(trip_data)
    if loc:
        return loc.get("previousstop")
    return None


def get_next_stop(trip_data: dict[str, Any]) -> Optional[str]:
    """Return the next stop from LiveLocation, or None."""
    loc = get_first_location(trip_data)
    if loc:
        return loc.get("nextstop")
    return None


################################################################################
# TripInfo
################################################################################


def extract_trip_info(trip_data: dict[str, Any]) -> dict[str, Any]:
    """
    Extract all relevant fields from a raw BMTC API response into a flat dict.

    This isolates the rest of the code from the BMTC JSON structure.
    """
    loc = get_first_location(trip_data)
    route = get_first_route(trip_data)

    last_refresh_str = (loc.get("lastrefreshon") if loc else None) or ""
    last_refresh = parse_timestamp(last_refresh_str) if last_refresh_str else None

    info: dict[str, Any] = {
        "source": route.get("sourcestation") if route else None,
        "destination": route.get("destinationstation") if route else None,
        "previous_stop": loc.get("previousstop") if loc else None,
        "next_stop": loc.get("nextstop") if loc else None,
        "location": loc.get("location") if loc else None,
        "latitude": loc.get("latitude") if loc else None,
        "longitude": loc.get("longitude") if loc else None,
        "heading": loc.get("heading") if loc else None,
        "trip_status": loc.get("trip_status") if loc else None,
        "last_refresh_str": last_refresh_str,
        "last_refresh": last_refresh,
        "is_idle": len(get_route_details(trip_data)) == 0,
        "_raw_location_valid": loc is not None,
    }
    return info


def determine_tracker_state(
    trip_info: dict[str, Any],
    offline_after: timedelta,
) -> str:
    """
    Return TRACKER_RUNNING, TRACKER_IDLE, or TRACKER_OFFLINE.

    Offline takes precedence: if last_refresh is stale, the tracker is offline
    regardless of RouteDetails.
    """
    if trip_info["last_refresh"] is not None:
        diff = datetime.now() - trip_info["last_refresh"]
        if diff > offline_after:
            return TRACKER_OFFLINE

    if trip_info["is_idle"]:
        return TRACKER_IDLE

    return TRACKER_RUNNING


################################################################################
# Time Helpers
################################################################################


def parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse a timestamp string into a datetime object, trying multiple formats."""
    cleaned = ts_str.strip()
    for fmt in (
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def time_str_to_time(t_str: str):
    """Convert 'HH:MM' string to a time object."""
    return datetime.strptime(t_str, "%H:%M").time()


def is_in_window(now: datetime, entry: dict[str, Any]) -> bool:
    """
    Check whether *now* falls inside a single schedule entry.

    Returns True if the entry is enabled, today's day name matches,
    and the current time is between start and end (inclusive).
    """
    if not entry.get("enabled", False):
        return False
    if now.strftime("%a") not in entry["days"]:
        return False
    start = time_str_to_time(entry["start"])
    end = time_str_to_time(entry["end"])
    current = now.time()
    return start <= current <= end


def find_next_window(
    now: datetime, schedule: list[dict[str, Any]]
) -> Optional[datetime]:
    """
    Find the earliest datetime after *now* when any enabled window starts.

    Searches up to 14 days ahead.  Returns None if no window exists.
    """
    best: Optional[datetime] = None
    for entry in schedule:
        if not entry.get("enabled", False):
            continue
        start_time = time_str_to_time(entry["start"])
        for day_offset in range(14):
            candidate_date = now + timedelta(days=day_offset)
            if candidate_date.strftime("%a") not in entry["days"]:
                continue
            candidate_dt = datetime.combine(candidate_date.date(), start_time)
            if candidate_dt <= now:
                continue
            if best is None or candidate_dt < best:
                best = candidate_dt
    return best


def get_active_window_name(now: datetime, schedule: list[dict[str, Any]]) -> Optional[str]:
    """Return the name of the first enabled schedule window covering *now*, or None."""
    for entry in schedule:
        if is_in_window(now, entry):
            return entry["name"]
    return None


def format_timedelta(delta: timedelta) -> str:
    """Format a timedelta as a human-readable string (e.g. '3 min 20 sec')."""
    total = int(delta.total_seconds())
    minutes = total // 60
    seconds = total % 60
    if minutes >= 60:
        hours = minutes // 60
        minutes = minutes % 60
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds:
            parts.append(f"{seconds}s")
        return " ".join(parts) if parts else "0s"
    if minutes > 0:
        return f"{minutes} min {seconds} sec"
    return f"{seconds} sec"


def format_schedule_days(days: list[str]) -> str:
    """Condense a list of day abbreviations into a readable range."""
    sorted_days = sorted(days, key=lambda d: DAY_NAMES[d])
    if len(sorted_days) < 2:
        return "-".join(sorted_days)
    indices = [DAY_NAMES[d] for d in sorted_days]
    if indices == list(range(indices[0], indices[-1] + 1)):
        return f"{sorted_days[0]}-{sorted_days[-1]}"
    return "-".join(sorted_days)


def format_wait_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


################################################################################
# Notification State
################################################################################

_stale_notified = False
_travel_alert_fired: dict[str, bool] = {}
_was_idle = False
_last_good_refresh: Optional[datetime] = None

_log_fp: Optional[io.TextIOWrapper] = None
_log_line_count: int = 0
_log_file_path: str = ""
_log_enabled: bool = True
_log_max_lines: int = 2000
_log_trim_to_lines: int = 1000


################################################################################
# Notifications
################################################################################


def _find_script(name: str) -> Optional[str]:
    """Locate a shell script in PATH or the known BASH_CMDS_DIR."""
    for dir_path in os.environ.get("PATH", "").split(":"):
        full = os.path.join(dir_path, name)
        if os.path.isfile(full):
            return full
    full = os.path.join(BASH_CMDS_DIR, name)
    if os.path.isfile(full):
        return full
    return None


def notify_stale(message: str) -> None:
    """Send stale notification via pushover and show it on screen."""
    build_script = _find_script("buildresultshow.sh")
    if build_script:
        try:
            subprocess.run(
                [build_script, "60", message],
                timeout=10,
                capture_output=True,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log_error(f"Warning: buildresultshow.sh failed: {e}")
    else:
        log_error("Warning: buildresultshow.sh not found; skipping display notification.")

    push_script = _find_script("pushover_msg_send.sh")
    if push_script:
        try:
            subprocess.run(
                [push_script, message],
                timeout=10,
                capture_output=True,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log_error(f"Warning: pushover_msg_send.sh failed: {e}")
    else:
        log_error("Warning: pushover_msg_send.sh not found; skipping push notification.")


def notify_resumed() -> None:
    """Clear the stale notification display."""
    kill_script = _find_script("kill_buildresultshow.sh")
    if kill_script:
        try:
            subprocess.run([kill_script], timeout=10, capture_output=True)
            return
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log_error(f"Warning: kill_buildresultshow.sh failed: {e}")

    try:
        subprocess.run(
            ["pkill", "-f", "result_show.py"],
            timeout=5,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log_error(f"Warning: pkill fallback failed: {e}")


################################################################################
# Tracking
################################################################################


def check_tracking(
    trip_info: dict[str, Any],
    bus_num: str,
    offline_after: timedelta,
) -> bool:
    """
    Evaluate the freshness of tracking data.

    Compares *last_refresh* against the current time.  If the difference
    exceeds *offline_after*, tracking is considered stale.

    Generates exactly ONE notification when tracking goes stale, and
    clears it when tracking resumes.

    Returns True if tracking is OK, False if stale.
    """
    global _stale_notified

    if not trip_info["_raw_location_valid"]:
        log("No LiveLocation data available.")
        print_blank()
        return True

    if not trip_info["last_refresh_str"]:
        log("No 'lastrefreshon' field in response.")
        print_blank()
        return True

    if trip_info["last_refresh"] is None:
        log(f"Could not parse lastrefreshon: {trip_info['last_refresh_str']}")
        print_blank()
        return True

    now = datetime.now()
    diff = now - trip_info["last_refresh"]

    print_key_value("Last Refresh", trip_info["last_refresh"])
    print_key_value("Current Time", now)
    print_key_value("Difference", format_timedelta(diff))
    print_blank()

    if not trip_info["is_idle"]:
        prev_stop = trip_info["previous_stop"] or "N/A"
        next_stop = trip_info["next_stop"] or "N/A"
        log("Bus is between:")
        log(f"  {prev_stop}")
        print_arrow()
        log(f"  {next_stop}")
        if trip_info["heading"]:
            print_key_value("Heading", trip_info["heading"])
        if trip_info["location"]:
            print_key_value("Location", trip_info["location"])
        if trip_info["trip_status"]:
            print_key_value("Trip Status", trip_info["trip_status"])
        lat = trip_info["latitude"] or "N/A"
        lon = trip_info["longitude"] or "N/A"
        print_key_value("Coordinates", f"{lat}, {lon}")
        print_blank()

    is_stale = diff > offline_after

    if is_stale:
        log("Tracking appears stale")
        if not _stale_notified:
            loc_name = trip_info["location"] or "Unknown"
            msg = (
                f"Bus {bus_num} tracking is stale. "
                f"Last refresh: {trip_info['last_refresh_str']}. "
                f"Location: {loc_name}"
            )
            notify_stale(msg)
            _stale_notified = True
    else:
        log("Tracking OK")
        if _stale_notified:
            notify_resumed()
            _stale_notified = False

    print_blank()
    return not is_stale


################################################################################
# Travel Alerts
################################################################################


def _normalize(name: Optional[str]) -> str:
    """Normalize a station name for tolerant comparison."""
    if not name:
        return ""
    import re as _re
    name = _re.sub(r"\s+", " ", name.strip().lower())
    name = _re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
    return _re.sub(r"\s+", " ", name)


def matches_route(trip_info: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Check if the current trip route matches the entry source/destination."""
    return (
        _normalize(trip_info["source"]) == _normalize(entry["source"])
        and _normalize(trip_info["destination"]) == _normalize(entry["destination"])
    )


def is_approaching_start(trip_info: dict[str, Any], alert: dict[str, Any]) -> bool:
    """Check if the bus's next stop is the alert start location (approaching)."""
    return _normalize(trip_info["next_stop"]) == _normalize(alert["alert_start_location"])


def is_at_end(trip_info: dict[str, Any], alert: dict[str, Any]) -> bool:
    """Check if the bus's next stop is the alert end location (segment passed)."""
    return _normalize(trip_info["next_stop"]) == _normalize(alert["alert_end_location"])


def matches_day(trip_info: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Check if today's weekday is in the entry's configured days."""
    return datetime.now().strftime("%a") in entry["days"]


def _build_stop_list(trip_data: dict[str, Any]) -> list[str]:
    """Extract ordered stop names from RouteDetails in route order."""
    routes = get_route_details(trip_data)
    stops = []
    for r in routes:
        name = r.get("stationname")
        if name:
            stops.append(name)
    return stops


def _check_alert_positional(
    trip_info: dict[str, Any],
    trip_data: dict[str, Any],
    entry: dict[str, Any],
) -> None:
    """
    Position-based travel alert fallback when nextstop/previousstop are null.

    Builds an ordered stop list from RouteDetails, locates the bus
    within it, and compares against alert_start_location / alert_end_location
    indices to fire/dismiss notifications.
    """
    global _travel_alert_fired

    stops = _build_stop_list(trip_data)
    if not stops:
        return

    if _verbose:
        log("Route stop list:")
        for i, s in enumerate(stops):
            log(f"  {i}: {s}")

    norm_stops = [_normalize(s) for s in stops]
    alert = entry["alert"]
    norm_start = _normalize(alert["alert_start_location"])
    norm_end = _normalize(alert["alert_end_location"])

    try:
        start_idx = norm_stops.index(norm_start)
    except ValueError:
        return
    try:
        end_idx = norm_stops.index(norm_end)
    except ValueError:
        return

    # Determine where the bus currently is along the stop list
    current_idx = None
    next_stop = trip_info.get("next_stop")
    prev_stop = trip_info.get("previous_stop")
    location = trip_info.get("location")

    if next_stop:
        norm_next = _normalize(next_stop)
        try:
            next_idx = norm_stops.index(norm_next)
            current_idx = next_idx - 1
            if current_idx < 0:
                current_idx = 0
        except ValueError:
            pass

    if current_idx is None and prev_stop:
        norm_prev = _normalize(prev_stop)
        try:
            current_idx = norm_stops.index(norm_prev)
        except ValueError:
            pass

    if current_idx is None and location:
        norm_loc = _normalize(location)
        for i, s in enumerate(stops):
            ns = norm_stops[i]
            if ns in norm_loc or norm_loc in ns:
                current_idx = i
                break

    if current_idx is None:
        return

    name = entry["name"]
    was_fired = _travel_alert_fired.get(name, False)

    if current_idx >= end_idx:
        if was_fired:
            notify_resumed()
            _travel_alert_fired.pop(name, None)
        entry["_state"] = "COMPLETED"
        return

    if current_idx >= start_idx:
        if not was_fired:
            _fire_travel_alert(entry)
            _travel_alert_fired[name] = True
        return

    if was_fired:
        notify_resumed()
        _travel_alert_fired.pop(name, None)


def _fire_travel_alert(entry: dict[str, Any]) -> None:
    """Send the travel alert notification and log the formatted block."""
    alert = entry["alert"]
    log_separator()
    print_blank()
    print_section("TRAVEL ALERT")
    print_blank()
    log("Alert Name")
    log(entry["name"])
    print_blank()
    log("Source")
    log(entry["source"])
    print_blank()
    log("Destination")
    log(entry["destination"])
    print_blank()
    log("Bus Segment")
    log(alert["alert_start_location"])
    print_arrow()
    log(alert["alert_end_location"])
    print_blank()
    log("Notification")
    log(alert["notification"])
    print_blank()
    log_separator()
    print_blank()

    notify_stale(alert["notification"])


def check_travel_alerts(
    trip_info: dict[str, Any],
    trip_data: dict[str, Any],
    schedule: list[dict[str, Any]],
) -> None:
    """Evaluate alerts from enabled schedule entries against the current trip."""
    global _travel_alert_fired

    if not schedule:
        return
    if trip_info["is_idle"]:
        return
    if not trip_info["source"] or not trip_info["destination"]:
        return

    if DEVELOPMENT_MODE:
        stops = _build_stop_list(trip_data)
        if stops:
            loc = trip_info.get("location")
            current_idx = None
            if loc:
                norm_loc = _normalize(loc)
                norm_stops = [_normalize(s) for s in stops]
                for i, ns in enumerate(norm_stops):
                    if ns in norm_loc or norm_loc in ns:
                        current_idx = i
                        break
            log("Route stop list:")
            for i, s in enumerate(stops):
                marker = "  <<<<<" if i == current_idx else ""
                log(f"  {i}: {s}{marker}")

    for entry in schedule:
        if not entry.get("enabled", False):
            continue
        if entry.get("_state") == "COMPLETED":
            continue
        alert = entry.get("alert")
        if not alert:
            continue

        if not matches_day(trip_info, entry):
            _travel_alert_fired.pop(entry["name"], None)
            continue

        if not matches_route(trip_info, entry):
            _travel_alert_fired.pop(entry["name"], None)
            continue

        if trip_info.get("previous_stop") and trip_info.get("next_stop"):
            approaching = is_approaching_start(trip_info, alert)
            at_end = is_at_end(trip_info, alert)
            was_fired = _travel_alert_fired.get(entry["name"], False)

            if approaching and not was_fired:
                _fire_travel_alert(entry)
                _travel_alert_fired[entry["name"]] = True
            elif at_end and was_fired:
                notify_resumed()
                _travel_alert_fired.pop(entry["name"], None)
                entry["_state"] = "COMPLETED"
            elif not approaching and not at_end and not was_fired:
                _check_alert_positional(trip_info, trip_data, entry)
        else:
            _check_alert_positional(trip_info, trip_data, entry)


################################################################################
# Idle State
################################################################################


def print_idle_status(trip_info: dict[str, Any]) -> None:
    """Print the idle status block showing current location."""
    location_name = "Unknown"
    if trip_info["_raw_location_valid"]:
        location_name = trip_info["location"] or "Unknown"
        lat = trip_info["latitude"]
        lon = trip_info["longitude"]
        if lat is not None and lon is not None:
            location_name = f"{location_name} ({lat}, {lon})"

    log_separator()
    print_blank()
    print_section("Bus Status")
    print_blank()
    log("Idle (No Active Trip)")
    print_blank()
    log("Current Location")
    log(location_name)
    print_blank()
    log("Waiting for next trip...")
    print_blank()
    log_separator()
    print_blank()


################################################################################
# Monitoring
################################################################################


def monitor(
    session: requests.Session,
    vehicle_id: int,
    bus_num: str,
    offline_after: timedelta,
    poll_interval: int,
    schedule: Optional[list[dict[str, Any]]] = None,
    schedule_name: Optional[str] = None,
) -> None:
    """Perform a single poll of vehicle tracking data."""
    global _was_idle, _last_good_refresh, _stale_notified

    log_separator()
    log(datetime.now().strftime("%a %b %d %H:%M:%S"))
    if schedule_name:
        log(f"[{schedule_name}] Checking {bus_num}")
    else:
        log(f"Checking {bus_num}")
    log_separator()
    print_blank()

    trip_data = fetch_trip_details(session, vehicle_id)
    if trip_data is None:
        if _last_good_refresh and datetime.now() - _last_good_refresh > offline_after:
            if not _stale_notified:
                msg = (
                    f"Bus {bus_num} tracking is stale. "
                    f"No API response received since {_last_good_refresh}."
                )
                notify_stale(msg)
                _stale_notified = True
        log(f"Network/API error. Retrying in {poll_interval} seconds...")
        print_blank()
        return

    trip_info = extract_trip_info(trip_data)

    if trip_info["last_refresh"] is not None:
        if _last_good_refresh is None or trip_info["last_refresh"] > _last_good_refresh:
            _last_good_refresh = trip_info["last_refresh"]

    state = determine_tracker_state(trip_info, offline_after)

    if state == TRACKER_OFFLINE:
        check_tracking(trip_info, bus_num, offline_after)
        return

    if state == TRACKER_IDLE:
        check_tracking(trip_info, bus_num, offline_after)
        if not _was_idle:
            _travel_alert_fired.clear()
            _was_idle = True
        print_idle_status(trip_info)
        return

    if state == TRACKER_RUNNING:
        check_tracking(trip_info, bus_num, offline_after)
        if _was_idle:
            _was_idle = False
        if schedule:
            check_travel_alerts(trip_info, trip_data, schedule)
        return


################################################################################
# Startup Banner
################################################################################


def print_startup_banner(
    config: dict[str, Any],
    bus_num: str,
    vehicle_id: int,
    always_track: bool,
) -> None:
    """Print a one-time startup summary with full configuration details."""
    log_separator()
    log(f"BMTC Bus Tracker v{VERSION}")
    log_separator()
    print_blank()
    print_key_value("Bus Number", bus_num)
    print_key_value("Vehicle ID", vehicle_id)
    print_blank()
    print_key_value("Poll Interval", f"{config['poll_interval_secs']} sec")
    print_key_value("Offline Alert", f"{config['offline_after_mins']} min")
    print_blank()

    if always_track:
        log("Mode                : Continuous tracking (schedule ignored)")
    else:
        enabled_entries = [e for e in config["schedule"] if e.get("enabled", False)]
        for i, entry in enumerate(enabled_entries):
            days_str = format_schedule_days(entry["days"])
            alert = entry["alert"]
            log(entry["name"])
            log(days_str)
            log(f"{entry['start']} - {entry['end']}")
            log(entry["source"])
            print_arrow()
            log(entry["destination"])
            log("Alert")
            log(alert["alert_start_location"])
            print_arrow()
            log(alert["alert_end_location"])
            log("Notification")
            log(alert["notification"])
            if i < len(enabled_entries) - 1:
                log("-" * 56)
    print_blank()

    log_separator()
    print_blank()


################################################################################
# Main
################################################################################


def main() -> None:
    """Entry point."""
    global _verbose, _show_http_msgs

    args = parse_cli_args()

    if args.version:
        log(f"bmtc_tracker.py version {VERSION}")
        sys.exit(0)

    _verbose = args.verbose
    _show_http_msgs = args.show_http_msgs

    config_path = find_config()
    config = load_config(config_path)
    init_logging(config)
    validate_config(config)

    bus_num = args.bus_num or config["bus_number"]
    poll_interval = config["poll_interval_secs"]
    offline_after = timedelta(minutes=config["offline_after_mins"])
    always_track = args.always_track
    schedule = config["schedule"]
    for entry in schedule:
        entry["_state"] = "ACTIVE"

    session = requests.Session()
    vehicle_id = resolve_vehicle_id(session, bus_num)

    print_startup_banner(config, bus_num, vehicle_id, always_track)

    _active_schedule: Optional[str] = None
    _current_date = datetime.now().date()

    while True:
        if always_track:
            monitor(session, vehicle_id, bus_num, offline_after, poll_interval, schedule, "Always")
            time.sleep(poll_interval)
            continue

        now = datetime.now()

        new_date = now.date()
        if new_date != _current_date:
            _current_date = new_date
            for entry in schedule:
                entry["_state"] = "ACTIVE"
            _travel_alert_fired.clear()
            log("New day. All schedules reset.")
            print_blank()

        active_schedule_list = [e for e in schedule if e.get("_state") != "COMPLETED"]
        active = get_active_window_name(now, active_schedule_list)

        if active:
            if active != _active_schedule:
                if _active_schedule is not None:
                    log(f"{_active_schedule} Schedule Ended")
                    print_blank()
                log(f"{active} Schedule Started")
                print_blank()
                _active_schedule = active
            monitor(session, vehicle_id, bus_num, offline_after, poll_interval, schedule, active)

            active_entry = next((e for e in schedule if e["name"] == active), None)
            if active_entry and active_entry.get("_state") == "COMPLETED":
                log_separator()
                log(f"{active} schedule completed.")
                alert = active_entry["alert"]
                log("Bus has crossed")
                log(alert["alert_end_location"])
                log("Monitoring stopped for this schedule.")
                log_separator()
                print_blank()
                _active_schedule = None
                remaining = [e for e in schedule if e.get("_state") != "COMPLETED"]
                next_window = find_next_window(now, remaining)
                if next_window is None:
                    log("No upcoming monitoring windows found in the next 14 days.")
                    log("Sleeping 1 hour.")
                    print_blank()
                    time.sleep(3600)
                    continue
                sleep_secs = (next_window - datetime.now()).total_seconds()
                if sleep_secs > 0:
                    wait_str = format_wait_duration(sleep_secs)
                    log("Waiting for next schedule...")
                    log(wait_str)
                    print_blank()
                    time.sleep(sleep_secs)
                continue
            time.sleep(poll_interval)
        else:
            if _active_schedule is not None:
                log(f"{_active_schedule} Schedule Ended")
                print_blank()
                _active_schedule = None

            next_window = find_next_window(now, active_schedule_list)
            if next_window is None:
                log("No upcoming monitoring windows found in the next 14 days.")
                log("Sleeping 1 hour.")
                print_blank()
                time.sleep(3600)
                continue

            sleep_secs = (next_window - datetime.now()).total_seconds()
            if sleep_secs <= 0:
                continue

            wait_str = format_wait_duration(sleep_secs)
            log_separator()
            print_blank()
            log("Outside Monitoring Window")
            print_blank()
            log("Current Time")
            log(f"{now.strftime('%a %b %d %H:%M:%S')}")
            print_blank()
            log("Next Monitoring Window")
            log(f"{next_window.strftime('%a %b %d %H:%M:%S')}")
            print_blank()
            log("Sleeping For")
            log(wait_str)
            print_blank()
            log_separator()
            print_blank()
            time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
