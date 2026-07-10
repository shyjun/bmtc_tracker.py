#!/usr/bin/env python3
"""
BMTC Bus Tracker

Monitors a BMTC bus by calling BMTC's internal APIs and generates
notifications when the tracking becomes stale.

Usage:
    python bmtc_tracker.py [-h] [-v] [--version] [--bus-num=KA57F4864] [--always-track]
"""

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

VERSION = "1.0.0"
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


################################################################################
# Logging
################################################################################

_verbose = False
_show_http_msgs = False


def log(message: str = "") -> None:
    """Print a message to stdout."""
    print(message)


def log_error(message: str) -> None:
    """Print an error message to stderr."""
    print(message, file=sys.stderr)


def log_separator() -> None:
    """Print the standard separator line."""
    log("=" * 56)


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
        for key in ("name", "enabled", "days", "start", "end"):
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

    travel_alerts = config.get("travel_alerts", [])
    if not isinstance(travel_alerts, list):
        log_error("Error: config.json 'travel_alerts' must be a list.")
        sys.exit(1)
    for entry in travel_alerts:
        for key in ("name", "enabled", "days", "source", "destination",
                     "alert_start_location", "alert_end_location", "notification"):
            if key not in entry:
                log_error(
                    f"Error: travel_alert entry missing '{key}'."
                )
                sys.exit(1)
        for day in entry["days"]:
            if day not in DAY_NAMES:
                log_error(
                    f"Error: invalid day '{day}' in travel_alert "
                    f"'{entry['name']}'."
                )
                sys.exit(1)


################################################################################
# BMTC APIs
################################################################################


def _api_post(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
) -> Any:
    """Make an API POST request with standard headers and timeout."""
    if _show_http_msgs:
        log()
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
        log()
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


def fetch_trip_details(
    session: requests.Session, vehicle_id: int
) -> Optional[dict[str, Any]]:
    """
    Fetch live trip details for a given vehicle ID.

    Returns the parsed JSON dict, or None on error.
    """
    payload: dict[str, Any] = {"vehicleId": vehicle_id}
    try:
        return _api_post(session, TRIP_DETAILS_URL, payload)
    except (requests.RequestException, json.JSONDecodeError) as e:
        if _verbose:
            log_error(f"Error: trip details fetch failed: {e}")
        return None


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
    trip_data: dict[str, Any],
    bus_num: str,
    offline_after: timedelta,
) -> bool:
    """
    Evaluate the freshness of tracking data.

    Compares *lastrefreshon* against the current time.  If the difference
    exceeds *offline_after*, tracking is considered stale.

    Generates exactly ONE notification when tracking goes stale, and
    clears it when tracking resumes.

    Returns True if tracking is OK, False if stale.
    """
    global _stale_notified

    live = trip_data.get("LiveLocation")
    if not live or not isinstance(live, list) or len(live) == 0:
        log("No LiveLocation data available.")
        log()
        return True

    loc = live[0]
    last_refresh_str = loc.get("lastrefreshon", "") or ""
    if not last_refresh_str:
        log("No 'lastrefreshon' field in response.")
        log()
        return True

    last_refresh = parse_timestamp(last_refresh_str)
    if last_refresh is None:
        log(f"Could not parse lastrefreshon: {last_refresh_str}")
        log()
        return True

    now = datetime.now()
    diff = now - last_refresh

    previous_stop = loc.get("previousstop", "N/A") or "N/A"
    next_stop = loc.get("nextstop", "N/A") or "N/A"
    latitude = loc.get("latitude", "N/A")
    longitude = loc.get("longitude", "N/A")
    heading = loc.get("heading", "") or ""
    location_name = loc.get("location", "") or ""
    trip_status = loc.get("trip_status", "") or ""

    log(f"Last Refresh  : {last_refresh}")
    log(f"Current Time  : {now}")
    log(f"Difference    : {format_timedelta(diff)}")
    log()
    log("Bus is between:")
    log(f"  {previous_stop}")
    log("  \u2193")
    log(f"  {next_stop}")
    if heading:
        log(f"Heading       : {heading}")
    if location_name:
        log(f"Location      : {location_name}")
    if trip_status:
        log(f"Trip Status   : {trip_status}")
    log(f"Coordinates   : {latitude}, {longitude}")
    log()

    is_stale = diff > offline_after

    if is_stale:
        log("Tracking appears stale")
        if not _stale_notified:
            msg = (
                f"Bus {bus_num} tracking is stale. "
                f"Last refresh: {last_refresh_str}. "
                f"Location: {location_name or previous_stop}"
            )
            notify_stale(msg)
            _stale_notified = True
    else:
        log("Tracking OK")
        if _stale_notified:
            notify_resumed()
            _stale_notified = False

    log()
    return not is_stale


################################################################################
# Travel Alerts
################################################################################


def _normalize(name: Optional[str]) -> str:
    """Normalize a station name for tolerant comparison."""
    if not name:
        return ""
    import re as _re
    return _re.sub(r"\s+", " ", name.strip().lower())


def matches_segment(
    actual_prev: Optional[str],
    actual_next: Optional[str],
    expected_start: str,
    expected_end: str,
) -> bool:
    """Check if the actual bus segment matches the expected alert segment."""
    return (
        _normalize(actual_prev) == _normalize(expected_start)
        and _normalize(actual_next) == _normalize(expected_end)
    )


def _fire_travel_alert(alert: dict[str, Any]) -> None:
    """Send the travel alert notification and log the formatted block."""
    name = alert["name"]
    log_separator()
    log()
    log("TRAVEL ALERT")
    log()
    log(f"Alert Name")
    log(f"{name}")
    log()
    log("Source")
    log(f"{alert['source']}")
    log()
    log("Destination")
    log(f"{alert['destination']}")
    log()
    log("Bus Segment")
    log(f"{alert['alert_start_location']}")
    log("\u2193")
    log(f"{alert['alert_end_location']}")
    log()
    log("Notification")
    log(f"{alert['notification']}")
    log()
    log_separator()
    log()

    notify_stale(alert["notification"])


def check_travel_alerts(
    trip_data: dict[str, Any],
    travel_alerts: list[dict[str, Any]],
) -> None:
    """Evaluate every enabled travel alert against the current trip data."""
    global _travel_alert_fired

    if not travel_alerts:
        return

    route_details = trip_data.get("RouteDetails")
    live = trip_data.get("LiveLocation")

    if (
        not route_details
        or not isinstance(route_details, list)
        or len(route_details) == 0
    ):
        return
    if not live or not isinstance(live, list) or len(live) == 0:
        return

    route = route_details[0]
    loc = live[0]

    route_source = route.get("sourcestation")
    route_dest = route.get("destinationstation")
    actual_prev = loc.get("previousstop")
    actual_next = loc.get("nextstop")

    if not route_source or not route_dest:
        return

    now = datetime.now()
    today = now.strftime("%a")

    for alert in travel_alerts:
        if not alert.get("enabled", False):
            continue
        if today not in alert["days"]:
            _travel_alert_fired.pop(alert["name"], None)
            continue

        source_match = _normalize(route_source) == _normalize(alert["source"])
        dest_match = _normalize(route_dest) == _normalize(alert["destination"])
        segment_match = matches_segment(
            actual_prev, actual_next,
            alert["alert_start_location"], alert["alert_end_location"],
        )

        if source_match and dest_match and segment_match:
            if not _travel_alert_fired.get(alert["name"], False):
                _fire_travel_alert(alert)
                _travel_alert_fired[alert["name"]] = True
        else:
            _travel_alert_fired.pop(alert["name"], None)


################################################################################
# Monitoring
################################################################################


def monitor(
    session: requests.Session,
    vehicle_id: int,
    bus_num: str,
    offline_after: timedelta,
    poll_interval: int,
    travel_alerts: Optional[list[dict[str, Any]]] = None,
) -> None:
    """Perform a single poll of vehicle tracking data."""
    log_separator()
    log(datetime.now().strftime("%a %b %d %H:%M:%S"))
    log(f"Checking {bus_num}")
    log_separator()
    log()

    trip_data = fetch_trip_details(session, vehicle_id)
    if trip_data is None:
        log(f"Network/API error. Retrying in {poll_interval} seconds...")
        log()
        return

    check_tracking(trip_data, bus_num, offline_after)

    if travel_alerts:
        check_travel_alerts(trip_data, travel_alerts)


################################################################################
# Startup Banner
################################################################################


def print_startup_banner(
    config: dict[str, Any],
    bus_num: str,
    vehicle_id: int,
    always_track: bool,
) -> None:
    """Print a one-time startup summary."""
    log_separator()
    log(f"BMTC Bus Tracker v{VERSION}")
    log_separator()
    log()
    log(f"Bus Number          : {bus_num}")
    log(f"Vehicle ID          : {vehicle_id}")
    log()
    log(f"Poll Interval       : {config['poll_interval_secs']} sec")
    log(f"Offline Alert       : {config['offline_after_mins']} min")
    log()

    if always_track:
        log("Mode                : Continuous tracking (schedule ignored)")
    else:
        log("Schedules")
        for entry in config["schedule"]:
            if not entry.get("enabled", False):
                continue
            days_str = format_schedule_days(entry["days"])
            label = f"  {entry['name']:<20}"
            log(f"{label}: {days_str} {entry['start']} - {entry['end']}")

    travel_alerts = config.get("travel_alerts", [])
    if travel_alerts:
        log("Travel Alerts")
        for entry in travel_alerts:
            if not entry.get("enabled", False):
                continue
            days_str = format_schedule_days(entry["days"])
            label = f"  {entry['name']:<20}"
            log(f"{label}: {days_str} {entry['source']} \u2192 {entry['destination']}")
        log()

    log_separator()
    log()


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
    validate_config(config)

    bus_num = args.bus_num or config["bus_number"]
    poll_interval = config["poll_interval_secs"]
    offline_after = timedelta(minutes=config["offline_after_mins"])
    always_track = args.always_track
    schedule = config["schedule"]
    travel_alerts = config.get("travel_alerts", [])

    session = requests.Session()
    vehicle_id = resolve_vehicle_id(session, bus_num)

    print_startup_banner(config, bus_num, vehicle_id, always_track)

    _active_schedule: Optional[str] = None

    while True:
        if always_track:
            monitor(session, vehicle_id, bus_num, offline_after, poll_interval, travel_alerts)
            time.sleep(poll_interval)
            continue

        now = datetime.now()
        active = get_active_window_name(now, schedule)

        if active:
            if active != _active_schedule:
                if _active_schedule is not None:
                    log(f"{_active_schedule} Schedule Ended")
                    log()
                log(f"{active} Schedule Started")
                log()
                _active_schedule = active
            monitor(session, vehicle_id, bus_num, offline_after, poll_interval, travel_alerts)
            time.sleep(poll_interval)
        else:
            if _active_schedule is not None:
                log(f"{_active_schedule} Schedule Ended")
                log()
                _active_schedule = None

            next_window = find_next_window(now, schedule)
            if next_window is None:
                log("No upcoming monitoring windows found in the next 14 days.")
                log("Sleeping 1 hour.")
                log()
                time.sleep(3600)
                continue

            sleep_secs = (next_window - datetime.now()).total_seconds()
            if sleep_secs <= 0:
                continue

            wait_str = format_wait_duration(sleep_secs)
            log_separator()
            log()
            log("Outside Monitoring Window")
            log()
            log(f"Current Time")
            log(f"{now.strftime('%a %b %d %H:%M:%S')}")
            log()
            log("Next Monitoring Window")
            log(f"{next_window.strftime('%a %b %d %H:%M:%S')}")
            log()
            log("Sleeping For")
            log(wait_str)
            log()
            log_separator()
            log()
            time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
