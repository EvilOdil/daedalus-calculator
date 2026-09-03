"""Extract a `FlightLogSummary` from an ArduPilot dataflash log.

Runs once, over a file on disk, and produces a small pydantic object — no raw
log bytes or full-resolution samples survive past this call. The caller is
expected to discard the source file immediately afterwards; see the "Add
flight from log" flow on the Missions page, which parses into a temp file and
deletes it in a `finally` block.

Message layouts are version-dependent (old logs split battery data into a
`CURR` message, newer ones use one `BAT` message with a resting-voltage field
`VoltR` alongside the loaded voltage `Volt`), so every field is read with
`getattr(..., None)` and missing data degrades to a `warnings` entry rather
than an exception. `pymavlink`'s `DFReader` stamps every message, of every
type, with a Unix-epoch `_timestamp` computed from the log's own clock
message — that is used as the one universal time base instead of any
per-message-type time field.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import math
import os
from pathlib import Path
from typing import Any, Callable

from .missions import (
    BatteryLogSummary, FlightLogSummary, LogEvent, LogPoint, MissionCmdEvent, RateSample,
    SeriesPoint,
)

#: Battery data lives in `BAT` on modern logs, `CURR` on old ones. Field names
#: (Volt/Curr/CurrTot/VoltR) have stayed stable across both.
_BATTERY_TYPES = ("BAT", "CURR")
#: A hard ceiling on how many messages this will read before giving up, so a
#: pathological or misidentified file cannot hang the app indefinitely.
_MAX_MESSAGES = 3_000_000
#: Events are capped the same way — some logs spam thousands of repeated
#: failsafe MSG lines, which would bloat the saved summary for no benefit.
_MAX_EVENTS = 500

_WARN_KEYWORDS = ("fail", "error", "crash", "abort", "glitch")

#: ArduPilot's LogErrorSubsystem enum values for two safety-critical checks
#: worth surfacing specially rather than as a bare "Subsys N" code - verified
#: against ArduCopter/crash_check.cpp (source, not the log's own text):
#: THRUST_LOSS_CHECK fires when throttle has been >=90% with attitude error
#: >15 deg for a full second (a failed motor/prop/ESC signature); CRASH_CHECK
#: fires when the vehicle auto-disarms after being detected as crashed.
_ERR_SUBSYS_THRUST_LOSS = 25
_ERR_SUBSYS_CRASH_CHECK = 12
#: FAILSAFE_FENCE - a geofence breach. ECode is a bitmask of which fence(s)
#: (0 = the matching "resolved" entry). Verified against ArduCopter/fence.cpp
#: and the bit values in AC_Fence/AC_Fence.h.
_ERR_SUBSYS_GEOFENCE = 9
_FENCE_TYPE_BITS = {1: "max altitude", 2: "circle", 4: "polygon", 8: "min altitude"}

#: MAV_CMD ids for the two mission commands that get their own short marker
#: ('T'/'L') instead of a bare sequence number - verified against pymavlink's
#: MAV_CMD enum, not guessed.
_CMD_TAKEOFF = 22  # MAV_CMD_NAV_TAKEOFF
_CMD_LAND = 21     # MAV_CMD_NAV_LAND

#: Caps mirroring _MAX_EVENTS - a mission with hundreds of waypoints
#: shouldn't bloat the saved summary or clutter a plot.
_MAX_MISSION_EVENTS = 150
#: Downsample target for the LANDING_TARGET arrival-rate series - the plot
#: doesn't need one point per detection on a long flight.
_MAX_LANDING_TARGET_POINTS = 300
#: Downsample target shared by rangefinder and optical-flow series.
_MAX_SENSOR_SERIES_POINTS = 500

#: Ground speed a multirotor is unambiguously translating at, not hovering or
#: drifting in GPS noise - the boundary "navigation" (cruise) distance is
#: measured against. A heuristic, not a real flight-phase classifier.
NAV_SPEED_THRESHOLD_MPS = 3.0
#: Consecutive fixes at/above the threshold required before a window counts as
#: "sustained", so a single noisy sample can't open or close the window.
NAV_MIN_SUSTAIN_SAMPLES = 3
#: Mean Earth radius, for the haversine ground-track distance.
_EARTH_RADIUS_M = 6_371_000.0

#: Rows as (t_s, voltage_v, current_a) at full resolution, before downsampling.
_BatteryRow = tuple[float, "float | None", "float | None"]
#: Rows as (t_s, lat, lng, ground_speed_m_s) for every valid GPS fix.
_GpsRow = tuple[float, float, float, "float | None"]


class LogParseError(RuntimeError):
    """The file could not be read as an ArduPilot log."""


@contextlib.contextmanager
def _suppress_native_stderr():
    """Redirect the OS-level stderr file descriptor, not just `sys.stderr`.

    pymavlink's optional fast indexer (`dfindexer`, used automatically when
    installed) is a compiled extension that writes "bad header" diagnostics -
    one line per byte it has to skip while resynchronising on a non-log file -
    straight to file descriptor 2. `contextlib.redirect_stderr` only patches
    the Python-level `sys.stderr` object and does not catch that.
    """
    saved_fd = os.dup(2)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull_fd, 2)
        yield
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        os.close(devnull_fd)


def _classify_msg_level(text: str) -> str:
    lowered = text.lower()
    return "warning" if any(k in lowered for k in _WARN_KEYWORDS) else "info"


def _decode_fence_bitmask(ecode: int | None) -> str:
    """ECode 0 on a FAILSAFE_FENCE entry is the matching 'resolved' record;
    otherwise it is a bitmask of breached fence type(s) (AC_Fence.h bit
    values), which can be more than one at once."""
    if not ecode:
        return "cleared"
    names = [name for bit, name in _FENCE_TYPE_BITS.items() if ecode & bit]
    return " + ".join(names) if names else f"code {ecode}"


def _err_event(t_s: float, subsys: int | None, ecode: int | None) -> LogEvent:
    """Build one ERR-message LogEvent. Thrust-loss, crash and geofence checks
    get plain English; every other subsystem stays a raw, untranslated code
    (see the module docstring on why - a wrong translation is worse than a
    code the user can look up themselves)."""
    if subsys == _ERR_SUBSYS_THRUST_LOSS:
        return LogEvent(
            t_s=t_s, level="error", subsystem="Thrust loss check",
            message="Potential thrust loss — sustained high throttle with excess "
                    "attitude error (a failed motor/propeller/ESC signature)",
        )
    if subsys == _ERR_SUBSYS_CRASH_CHECK:
        return LogEvent(t_s=t_s, level="error", subsystem="Crash check",
                         message="Crash detected — motors disarmed")
    if subsys == _ERR_SUBSYS_GEOFENCE:
        detail = _decode_fence_bitmask(ecode)
        return LogEvent(
            t_s=t_s, level="error" if ecode else "info", subsystem="Geofence",
            message=f"Breach ({detail})" if ecode else "Breach cleared",
        )
    return LogEvent(
        t_s=t_s, level="error",
        subsystem=f"Subsys {subsys}" if subsys is not None else None,
        message=f"Error code {ecode}" if ecode is not None else "error logged",
    )


def _mission_cmd_label(cid: int | None, cnum: int | None) -> tuple[str, str]:
    """(short plot label, full command name) for one CMD log entry. 'T' and
    'L' for takeoff/land (verified MAV_CMD ids); every other command shows
    its mission sequence number - what "waypoint N" means to the person who
    planned the mission."""
    if cid == _CMD_TAKEOFF:
        return "T", "Takeoff"
    if cid == _CMD_LAND:
        return "L", "Land"
    return (str(cnum) if cnum is not None else "?"), f"Waypoint {cnum}" if cnum is not None else "Command"


def _compute_rate_series(timestamps: list[float], max_points: int) -> list[RateSample]:
    """Instantaneous rate (Hz) between each consecutive pair of timestamps,
    uniformly downsampled to `max_points`. `timestamps` must already be one
    entry per distinct arrival (e.g. per new PL.LastMeasMS value), not one
    per log row - the caller is responsible for de-duplication."""
    if len(timestamps) < 2:
        return []
    points = [
        RateSample(t_s=t1, hz=1.0 / (t1 - t0))
        for t0, t1 in zip(timestamps, timestamps[1:]) if t1 > t0
    ]
    if len(points) <= max_points:
        return points
    stride = max(1, len(points) // max_points)
    picked = points[::stride]
    if picked[-1] is not points[-1]:
        picked.append(points[-1])
    return picked


def _derive_dates(first_epoch: float, armed_t_s: float) -> tuple[str | None, str | None]:
    """(log_date, flown_at) from the log's own clock, or (None, None) if the log
    had no usable absolute time reference.

    Uses arm time, not log start, as "when the flight happened" - a log
    typically starts a little before arming (power-on, boot, prearm checks),
    which would otherwise put same-day flights in the wrong relative order.
    A log with no absolute time reference (no GPS fix, unset RTC) times out at
    or near the Unix epoch - not a real recording date, so both stay unset.
    """
    try:
        recorded = _dt.datetime.fromtimestamp(first_epoch + armed_t_s, tz=_dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None, None
    if recorded.year < 2000:
        return None, None
    return recorded.date().isoformat(), recorded.strftime("%Y-%m-%d %H:%M")


def _downsample(rows: list[_BatteryRow], target: int) -> list[LogPoint]:
    """Uniform-stride decimation. Summary stats are computed from the full data
    elsewhere — this only shrinks what gets plotted and stored."""
    if not rows:
        return []
    stride = max(1, len(rows) // target)
    picked = rows[::stride]
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    out = []
    for t_s, v, i in picked:
        p = v * i if v is not None and i is not None else None
        out.append(LogPoint(t_s=t_s, voltage_v=v, current_a=i, power_w=p))
    return out


def _downsample_series(rows: list[tuple[float, float]], target: int) -> list[SeriesPoint]:
    """Same uniform-stride decimation as `_downsample`, generalised to a plain
    (t_s, value) series - rangefinder distance, optical-flow rate, or
    anything else that's just one number over time."""
    if not rows:
        return []
    stride = max(1, len(rows) // target)
    picked = rows[::stride]
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return [SeriesPoint(t_s=t, value=v) for t, v in picked]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle ground distance between two lat/lon points, in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _sustained_speed_window(
    speeds: list[float | None], threshold: float, min_samples: int
) -> tuple[int, int] | None:
    """Index range `[start, end]` (inclusive) spanning the first through last
    run of at least `min_samples` consecutive fixes at/above `threshold`.

    Not phase detection - a real takeoff/landing classifier would use climb
    rate and altitude, not ground speed alone. This is a cheap, explainable
    stand-in: "cruise" is wherever the aircraft was clearly translating.
    """
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for i, s in enumerate(speeds):
        fast = s is not None and s >= threshold
        if fast and run_start is None:
            run_start = i
        elif not fast and run_start is not None:
            if i - run_start >= min_samples:
                runs.append((run_start, i - 1))
            run_start = None
    if run_start is not None and len(speeds) - run_start >= min_samples:
        runs.append((run_start, len(speeds) - 1))
    if not runs:
        return None
    return runs[0][0], runs[-1][1]


def _compute_distances(
    track: list[_GpsRow],
) -> tuple[float | None, float | None, float | None, list[str]]:
    """(total_distance_m, navigation_distance_m, navigation_duration_s, warnings)
    from a GPS track.

    `navigation_duration_s` spans the same sustained-speed window as
    `navigation_distance_m` - the time from the first to the last fix in that
    window, i.e. roughly "after takeoff completed, before landing began" under
    the same speed-threshold heuristic (see `_sustained_speed_window`).
    """
    if len(track) < 2:
        return None, None, None, [
            "fewer than 2 GPS fixes with a 3D lock — cannot compute distance travelled"
        ]

    total = sum(
        _haversine_m(a[1], a[2], b[1], b[2]) for a, b in zip(track, track[1:])
    )

    speeds = [row[3] for row in track]
    window = _sustained_speed_window(speeds, NAV_SPEED_THRESHOLD_MPS, NAV_MIN_SUSTAIN_SAMPLES)
    if window is None:
        return total, None, None, [
            f"ground speed never sustained {NAV_SPEED_THRESHOLD_MPS:g} m/s for "
            f"{NAV_MIN_SUSTAIN_SAMPLES} consecutive GPS fixes — no navigation-phase "
            "window found (e.g. a hover-only test)"
        ]
    start, end = window
    nav = sum(
        _haversine_m(track[i][1], track[i][2], track[i + 1][1], track[i + 1][2])
        for i in range(start, end)
    )
    nav_duration = track[end][0] - track[start][0]
    return total, nav, nav_duration, []


def parse_log(
    path: str | Path,
    *,
    max_series_points: int = 500,
    progress_callback: Callable[[int], None] | None = None,
) -> FlightLogSummary:
    """Parse an ArduPilot `.bin`/`.log` dataflash log into a `FlightLogSummary`.

    `progress_callback`, if given, is called with 0-100 while pymavlink builds
    its byte-offset index of the file (opening the connection below) - the
    dominant cost for a large log, since the actual message extraction that
    follows only visits the offsets for the types this module wants. It is
    called synchronously, from whatever thread calls `parse_log` - callers
    driving a UI progress bar from a background thread are expected to have
    the callback just record the value for a separate thread to read, not
    touch UI state directly.
    """
    try:
        from pymavlink import mavutil
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise LogParseError(
            "the pymavlink package is not installed. `pip install pymavlink`."
        ) from exc

    path = Path(path)

    # Covers both opening the file and reading through it, since resync can also
    # happen mid-stream on a corrupt frame.
    with _suppress_native_stderr():
        try:
            mlog = mavutil.mavlink_connection(str(path), progress_callback=progress_callback)
        except Exception as exc:  # noqa: BLE001 - any parse failure becomes a LogParseError
            raise LogParseError(f"could not open '{path.name}' as an ArduPilot log: {exc}") from exc
        rows = _read_messages(mlog)

    if rows["first_epoch"] is None:
        raise LogParseError(
            f"'{path.name}' did not contain any recognisable BAT/CURR/MSG/STAT/GPS messages"
        )

    warnings: list[str] = rows["warnings"]
    first_epoch, last_epoch = rows["first_epoch"], rows["last_epoch"]
    duration_s = (last_epoch - first_epoch) if last_epoch is not None else None

    # STAT.Armed transitions are absent from a lot of real logs (older firmware,
    # arming at the very start of the recording), so falling back to the log's
    # own start/end is the common case, not an anomaly - not worth a warning.
    armed_epoch, disarmed_epoch = rows["armed_epoch"], rows["disarmed_epoch"]
    armed_t_s = (armed_epoch - first_epoch) if armed_epoch is not None else 0.0
    disarmed_t_s = (disarmed_epoch - first_epoch) if disarmed_epoch is not None else duration_s

    battery_rows: list[_BatteryRow] = rows["battery_rows"]
    battery = _summarise_battery(
        battery_rows, rows["sag_candidates"], rows["mah_first"], rows["mah_last"],
        armed_t_s, disarmed_t_s, warnings,
    )
    if not battery_rows:
        warnings.append("no battery voltage/current data (BAT/CURR messages) found in this log")

    log_date, flown_at = _derive_dates(first_epoch, armed_t_s)

    total_distance_m, navigation_distance_m, navigation_duration_s, distance_warnings = (
        _compute_distances(rows["gps_track"])
    )
    warnings.extend(distance_warnings)

    landing_target_rate = _compute_rate_series(
        rows["landing_target_timestamps"], _MAX_LANDING_TARGET_POINTS
    )
    rangefinder_distance_m = _downsample_series(rows["rangefinder_rows"], _MAX_SENSOR_SERIES_POINTS)
    optical_flow_rate_x = _downsample_series(rows["flow_x_rows"], _MAX_SENSOR_SERIES_POINTS)
    optical_flow_rate_y = _downsample_series(rows["flow_y_rows"], _MAX_SENSOR_SERIES_POINTS)
    throttle_pct = _downsample_series(rows["throttle_rows"], _MAX_SENSOR_SERIES_POINTS)

    return FlightLogSummary(
        source_filename=path.name,
        parsed_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        log_date=log_date,
        flown_at=flown_at,
        duration_s=duration_s,
        armed_t_s=armed_t_s,
        disarmed_t_s=disarmed_t_s,
        battery=battery,
        series=_downsample(battery_rows, max_series_points),
        events=rows["events"],
        takeoff_latlon=rows["takeoff_latlon"],
        landing_latlon=rows["landing_latlon"],
        total_distance_m=total_distance_m,
        navigation_distance_m=navigation_distance_m,
        navigation_duration_s=navigation_duration_s,
        thrust_loss_events=rows["thrust_loss_events"],
        crash_events=rows["crash_events"],
        geofence_breach_events=rows["geofence_breach_events"],
        mission_events=rows["mission_events"],
        landing_target_rate=landing_target_rate,
        landing_target_samples=rows["landing_target_samples"],
        rangefinder_distance_m=rangefinder_distance_m,
        optical_flow_rate_x=optical_flow_rate_x,
        optical_flow_rate_y=optical_flow_rate_y,
        throttle_pct=throttle_pct,
        warnings=warnings,
    )


def _read_messages(mlog: Any) -> dict[str, Any]:
    """One pass over the log, pulling out everything `parse_log` needs.

    Kept separate from `parse_log` so the stderr-suppression context in the
    caller wraps exactly the pymavlink calls and nothing else.
    """
    warnings: list[str] = []
    battery_rows: list[_BatteryRow] = []
    sag_candidates: list[float] = []
    mah_first: float | None = None
    mah_last: float | None = None
    events: list[LogEvent] = []
    truncated_events = False
    thrust_loss_events = 0
    crash_events = 0
    geofence_breach_events = 0
    mission_events: list[MissionCmdEvent] = []
    truncated_mission_events = False
    landing_target_timestamps: list[float] = []
    landing_target_samples = 0
    last_meas_ms: int | None = None
    rangefinder_rows: list[tuple[float, float]] = []
    flow_x_rows: list[tuple[float, float]] = []
    flow_y_rows: list[tuple[float, float]] = []
    throttle_rows: list[tuple[float, float]] = []

    first_epoch: float | None = None
    last_epoch: float | None = None
    armed_epoch: float | None = None
    disarmed_epoch: float | None = None
    last_armed_state: int | None = None
    takeoff_latlon: tuple[float, float] | None = None
    landing_latlon: tuple[float, float] | None = None
    gps_track: list[_GpsRow] = []

    want_types = list(_BATTERY_TYPES) + [
        "MSG", "ERR", "STAT", "GPS", "CMD", "PL", "RFND", "OF", "CTUN",
    ]

    n_messages = 0
    while True:
        if n_messages >= _MAX_MESSAGES:
            warnings.append(f"stopped after {_MAX_MESSAGES:,} messages — this log is unusually large")
            break
        try:
            msg = mlog.recv_match(type=want_types)
        except Exception as exc:  # noqa: BLE001 - a corrupt frame mid-file should not be fatal
            warnings.append(f"stopped early: the log became unreadable ({exc})")
            break
        if msg is None:
            break
        n_messages += 1

        t_epoch: float | None = getattr(msg, "_timestamp", None)
        if t_epoch is None:
            continue
        if first_epoch is None:
            first_epoch = t_epoch
        last_epoch = t_epoch
        t_s = t_epoch - first_epoch

        mtype = msg.get_type()

        if mtype in _BATTERY_TYPES:
            instance = getattr(msg, "Instance", 0) or 0
            if instance != 0:
                continue  # only the primary battery is summarised
            volt = getattr(msg, "Volt", None)
            curr = getattr(msg, "Curr", None)
            volt_r = getattr(msg, "VoltR", None)
            curr_tot = getattr(msg, "CurrTot", None)
            if volt is not None or curr is not None:
                battery_rows.append((t_s, volt, curr))
            if volt is not None and volt_r is not None:
                sag_candidates.append(volt_r - volt)
            if curr_tot is not None:
                if mah_first is None:
                    mah_first = curr_tot
                mah_last = curr_tot

        elif mtype == "MSG":
            if len(events) < _MAX_EVENTS:
                text = getattr(msg, "Message", "") or ""
                events.append(LogEvent(t_s=t_s, level=_classify_msg_level(text), message=text))
            else:
                truncated_events = True

        elif mtype == "ERR":
            subsys = getattr(msg, "Subsys", None)
            ecode = getattr(msg, "ECode", None)
            # Counted unconditionally, even past the event-list cap below - a
            # safety-relevant count must stay accurate regardless of how many
            # MSG/ERR lines got truncated from the displayed list.
            if subsys == _ERR_SUBSYS_THRUST_LOSS:
                thrust_loss_events += 1
            elif subsys == _ERR_SUBSYS_CRASH_CHECK:
                crash_events += 1
            elif subsys == _ERR_SUBSYS_GEOFENCE and ecode:
                geofence_breach_events += 1  # ecode==0 is the matching "resolved" entry
            if len(events) < _MAX_EVENTS:
                events.append(_err_event(t_s, subsys, ecode))
            else:
                truncated_events = True

        elif mtype == "STAT":
            armed = getattr(msg, "Armed", None)
            if armed is not None:
                if last_armed_state == 0 and armed == 1 and armed_epoch is None:
                    armed_epoch = t_epoch
                elif last_armed_state == 1 and armed == 0:
                    disarmed_epoch = t_epoch
                last_armed_state = armed

        elif mtype == "GPS":
            status = getattr(msg, "Status", 0) or 0
            lat = getattr(msg, "Lat", None)
            lng = getattr(msg, "Lng", None)
            if status >= 3 and lat and lng:
                if takeoff_latlon is None:
                    takeoff_latlon = (lat, lng)
                landing_latlon = (lat, lng)
                gps_track.append((t_s, lat, lng, getattr(msg, "Spd", None)))

        elif mtype == "CMD":
            if len(mission_events) < _MAX_MISSION_EVENTS:
                label, command = _mission_cmd_label(getattr(msg, "CId", None), getattr(msg, "CNum", None))
                mission_events.append(MissionCmdEvent(t_s=t_s, label=label, command=command))
            else:
                truncated_mission_events = True

        elif mtype == "PL":
            # LastMeasMS is a boot-relative millisecond timestamp of the last
            # LANDING_TARGET received by AC_PrecLand - a new value means a new
            # message arrived since the previous "PL" row. ArduPilot does not
            # log raw MAVLink messages, so this is the closest honest proxy
            # for LANDING_TARGET arrival rate; precision is bounded by how
            # often "PL" itself is logged (main loop rate), not millisecond-exact.
            meas = getattr(msg, "LastMeasMS", None)
            if meas is not None:
                if last_meas_ms is not None and meas != last_meas_ms:
                    landing_target_timestamps.append(t_s)
                    landing_target_samples += 1
                last_meas_ms = meas

        elif mtype == "RFND":
            instance = getattr(msg, "Instance", 0) or 0
            dist = getattr(msg, "Dist", None)
            if instance == 0 and dist is not None:  # only the primary rangefinder
                rangefinder_rows.append((t_s, dist))

        elif mtype == "OF":
            flow_x = getattr(msg, "flowX", None)
            flow_y = getattr(msg, "flowY", None)
            if flow_x is not None:
                flow_x_rows.append((t_s, flow_x))
            if flow_y is not None:
                flow_y_rows.append((t_s, flow_y))

        elif mtype == "CTUN":
            # ThO is the mixer's commanded throttle output, 0-1 - the same
            # "stick position" convention a manufacturer's thrust table uses,
            # not a measurement of actual thrust or current.
            tho = getattr(msg, "ThO", None)
            if tho is not None:
                throttle_rows.append((t_s, tho * 100.0))

    if truncated_events:
        warnings.append(f"more than {_MAX_EVENTS} MSG/ERR lines — only the first {_MAX_EVENTS} were kept")
    if truncated_mission_events:
        warnings.append(f"more than {_MAX_MISSION_EVENTS} mission commands — only the first "
                         f"{_MAX_MISSION_EVENTS} were kept")

    return dict(
        warnings=warnings, battery_rows=battery_rows, sag_candidates=sag_candidates,
        mah_first=mah_first, mah_last=mah_last, events=events,
        first_epoch=first_epoch, last_epoch=last_epoch,
        armed_epoch=armed_epoch, disarmed_epoch=disarmed_epoch,
        takeoff_latlon=takeoff_latlon, landing_latlon=landing_latlon, gps_track=gps_track,
        geofence_breach_events=geofence_breach_events, mission_events=mission_events,
        landing_target_timestamps=landing_target_timestamps,
        landing_target_samples=landing_target_samples,
        thrust_loss_events=thrust_loss_events, crash_events=crash_events,
        rangefinder_rows=rangefinder_rows, flow_x_rows=flow_x_rows, flow_y_rows=flow_y_rows,
        throttle_rows=throttle_rows,
    )


def _nearest_reading(rows: list[_BatteryRow], t_s: float | None, *, volt: bool) -> float | None:
    """The voltage (or current) reading closest to `t_s`, e.g. "at disarm"."""
    if t_s is None:
        return None
    candidates = [(abs(t - t_s), v if volt else i) for t, v, i in rows if (v if volt else i) is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


def _summarise_battery(
    rows: list[_BatteryRow],
    sag_candidates: list[float],
    mah_first: float | None,
    mah_last: float | None,
    armed_t_s: float | None,
    disarmed_t_s: float | None,
    warnings: list[str],
) -> BatteryLogSummary:
    if not rows:
        return BatteryLogSummary()

    volts = [(t, v) for t, v, _ in rows if v is not None]
    currs = [i for _, _, i in rows if i is not None]

    v_start = _nearest_reading(rows, armed_t_s, volt=True)
    v_end = _nearest_reading(rows, disarmed_t_s, volt=True)

    v_min = v_min_t_s = None
    if volts:
        v_min_t_s, v_min = min(volts, key=lambda tv: tv[1])

    if sag_candidates:
        sag_v = max(sag_candidates)
    elif v_start is not None and v_min is not None:
        sag_v = v_start - v_min
        warnings.append("battery has no VoltR (resting-voltage) samples — sag is v_start - v_min instead")
    else:
        sag_v = None

    i_max = max(currs) if currs else None

    mah_consumed = None
    if mah_first is not None and mah_last is not None:
        mah_consumed = mah_last - mah_first if mah_last >= mah_first else mah_last

    energy_wh = None
    is_estimated = False
    paired = [(v, i) for _, v, i in rows if v is not None and i is not None]
    both_times = [t for t, v, i in rows if v is not None and i is not None]
    if len(paired) >= 2:
        energy_wh = 0.0
        for k in range(1, len(paired)):
            dt = both_times[k] - both_times[k - 1]
            if dt <= 0:
                continue
            p0 = paired[k - 1][0] * paired[k - 1][1]
            p1 = paired[k][0] * paired[k][1]
            energy_wh += (p0 + p1) / 2 * dt / 3600.0
    elif mah_consumed is not None and volts:
        mean_v = sum(v for _, v in volts) / len(volts)
        energy_wh = mah_consumed / 1000.0 * mean_v
        is_estimated = True

    return BatteryLogSummary(
        v_start=v_start, v_end=v_end, v_min=v_min, v_min_t_s=v_min_t_s,
        sag_v=sag_v, i_max=i_max, mah_consumed=mah_consumed,
        energy_wh=energy_wh, energy_wh_is_estimated=is_estimated,
    )
