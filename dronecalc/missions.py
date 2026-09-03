"""Mission records: what was actually flown, on which setup, and how it went.

A component profile and a `Setup` describe what a build *should* do. A `Mission`
records what happened when it was flown — routine test flights against a setup,
each tagged with the conditions and what was tested that day. Kept separate from
`models.py` because these are operational logs, not design inputs: nothing in the
solver reads a `Mission`.
"""

from __future__ import annotations

import re
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field


class LogPoint(BaseModel):
    """One downsampled battery sample for plotting.

    Downsampled to a few hundred points regardless of the source log's length —
    the plot does not need per-sample resolution, and the summary statistics
    below are computed from the full-resolution data before this is built.
    """

    model_config = ConfigDict(extra="forbid")

    t_s: float = Field(description="Seconds since the first message in the log")
    voltage_v: float | None = None
    current_a: float | None = None
    power_w: float | None = None


class SeriesPoint(BaseModel):
    """One (time, value) sample on a generic plottable series - rangefinder
    distance, optical-flow rate, or anything else that's just a single
    number over time. The field's own name on FlightLogSummary carries the
    unit; this model stays generic rather than growing one variant per sensor."""

    model_config = ConfigDict(extra="forbid")

    t_s: float
    value: float


class RateSample(BaseModel):
    """One point on a message-rate-over-time series, e.g. LANDING_TARGET
    arrivals — Hz computed from the gap to the previous sample."""

    model_config = ConfigDict(extra="forbid")

    t_s: float
    hz: float


class MissionCmdEvent(BaseModel):
    """One mission command (CMD log message) ArduPilot started executing -
    takeoff, land, or a waypoint. `label` is the short marker text plotted on
    the flight's charts ('T', 'L', or the command's sequence number);
    `command` is the full name, for the events table."""

    model_config = ConfigDict(extra="forbid")

    t_s: float
    label: str
    command: str


class LogEvent(BaseModel):
    """One timestamped text/error line pulled from the log's MSG/ERR messages.

    ArduPilot's ERR subsystem/code numbers are surfaced as-is, not translated —
    the mapping is a large, version-drifting enum, and a wrong translation is
    worse than a code the user can look up themselves.
    """

    model_config = ConfigDict(extra="forbid")

    t_s: float = Field(description="Seconds since the first message in the log")
    level: str = Field(description="'error' (from an ERR message) or 'info'/'warning' (from MSG text)")
    subsystem: str | None = None
    message: str


class BatteryLogSummary(BaseModel):
    """Battery behaviour recovered from the log, computed from full-resolution data."""

    model_config = ConfigDict(extra="forbid")

    v_start: float | None = Field(None, description="Voltage at/after arming")
    v_end: float | None = Field(None, description="Voltage at/before disarm — i.e. at landing")
    v_min: float | None = None
    v_min_t_s: float | None = None
    sag_v: float | None = Field(
        None,
        description="Largest resting-vs-loaded gap seen (VoltR - Volt where the log carries "
                    "both), falling back to v_start - v_min when it doesn't",
    )
    i_max: float | None = None
    mah_consumed: float | None = None
    energy_wh: float | None = None
    energy_wh_is_estimated: bool = Field(
        False, description="True when energy was derived from mah_consumed and a mean "
                            "voltage rather than integrated from a synchronous V*I series"
    )


class FlightLogSummary(BaseModel):
    """What was extracted from an uploaded ArduPilot log.

    Deliberately holds no raw log bytes and nothing at full sample rate — see
    `dronecalc.ardupilot_log.parse_log`, which produces this and discards the
    source file. This is the only trace of the log that gets saved.
    """

    model_config = ConfigDict(extra="forbid")

    source_filename: str
    parsed_at: str = Field(description="ISO timestamp of when the log was parsed")
    log_date: str | None = Field(
        None, description="Calendar date the log itself was recorded (from its own clock, "
                           "not upload time), YYYY-MM-DD — None if the log had no usable "
                           "absolute time reference (no GPS fix, unset RTC, etc.)"
    )
    flown_at: str | None = Field(
        None, description="Date and time of arming, from the log's own clock (UTC), "
                           "'YYYY-MM-DD HH:MM' — distinguishes same-day flights, which "
                           "log_date alone cannot. None under the same conditions as log_date."
    )
    duration_s: float | None = None
    armed_t_s: float | None = None
    disarmed_t_s: float | None = None
    battery: BatteryLogSummary = Field(default_factory=BatteryLogSummary)
    series: list[LogPoint] = Field(default_factory=list)
    events: list[LogEvent] = Field(default_factory=list)
    takeoff_latlon: tuple[float, float] | None = None
    landing_latlon: tuple[float, float] | None = None
    total_distance_m: float | None = Field(
        None, description="Cumulative GPS ground-track distance, arm to disarm"
    )
    navigation_distance_m: float | None = Field(
        None, description="Ground-track distance while ground speed sustained at least "
                           "NAV_SPEED_THRESHOLD_MPS - a heuristic stand-in for 'cruise "
                           "flight' that excludes takeoff climb-out and landing approach. "
                           "None if the log never sustained that speed (e.g. a pure hover "
                           "test) or had too few GPS fixes."
    )
    navigation_duration_s: float | None = Field(
        None, description="Time from the first to the last fix in that same sustained-speed "
                           "window - roughly 'after takeoff completed, before landing began', "
                           "under the same heuristic as navigation_distance_m. None under the "
                           "same conditions navigation_distance_m is."
    )
    thrust_loss_events: int = Field(
        0, description="Count of ArduPilot's THRUST_LOSS_CHECK error: throttle >=90% with "
                       "attitude error >15 deg sustained for 1s - a failed motor, propeller "
                       "or ESC signature. Source: ArduCopter/crash_check.cpp."
    )
    crash_events: int = Field(
        0, description="Count of ArduPilot's CRASH_CHECK error - the vehicle auto-disarmed "
                       "after being detected as crashed. Source: ArduCopter/crash_check.cpp."
    )
    geofence_breach_events: int = Field(
        0, description="Count of new geofence breaches (ArduPilot's FAILSAFE_FENCE error, "
                       "excluding the matching 'resolved' entries). Source: ArduCopter/fence.cpp."
    )
    mission_events: list[MissionCmdEvent] = Field(
        default_factory=list,
        description="Takeoff/land/waypoint commands (CMD log messages) - the short markers "
                    "plotted on the flight's charts.",
    )
    landing_target_rate: list[RateSample] = Field(
        default_factory=list,
        description="LANDING_TARGET arrival rate (Hz) over time, from the precision-landing "
                    "companion computer (MAV_COMP_ID_ONBOARD_COMPUTER, 191) - reconstructed "
                    "from the PL log's LastMeasMS field, since ArduPilot does not log raw "
                    "MAVLink messages directly. Empty if precision landing was never active.",
    )
    landing_target_samples: int = Field(
        0, description="Total LANDING_TARGET messages counted while building landing_target_rate"
    )
    rangefinder_distance_m: list[SeriesPoint] = Field(
        default_factory=list,
        description="Primary rangefinder distance to ground (RFND, instance 0), metres. "
                    "Empty if no rangefinder was logged.",
    )
    optical_flow_rate_x: list[SeriesPoint] = Field(
        default_factory=list,
        description="Raw optical flow rate, X axis (OF.flowX), rad/s - the sensor's own "
                    "output, not ground velocity in m/s (ArduPilot does not log a converted "
                    "velocity, and the conversion needs a synchronised height and additional "
                    "compensation this app does not attempt). Empty if no flow sensor was logged.",
    )
    optical_flow_rate_y: list[SeriesPoint] = Field(
        default_factory=list,
        description="Raw optical flow rate, Y axis (OF.flowY), rad/s - see optical_flow_rate_x.",
    )
    throttle_pct: list[SeriesPoint] = Field(
        default_factory=list,
        description="Commanded throttle output (CTUN.ThO * 100), percent - the duty-cycle "
                    "command ArduPilot sent the motor mixer, i.e. stick position, not thrust. "
                    "Empty if CTUN was not logged.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Things this parse could not determine confidently, e.g. no ARM/DISARM "
                    "transition found",
    )


class MissionFlight(BaseModel):
    """One test flight within a mission.

    Wind speed is never sourced from a log — ArduPilot does not measure it — so
    it stays a manually entered field, per flight, since it can change between
    runs on the same day at the same site.
    """

    model_config = ConfigDict(extra="forbid")

    date: str | None = Field(
        None, description="When this flight happened - 'YYYY-MM-DD', or 'YYYY-MM-DD HH:MM' "
                           "when known precisely enough to tell same-day flights apart. "
                           "Prefilled from the log's own clock (flown_at) when parsed from one."
    )
    ardupilot_log_url: str | None = Field(
        None, description="Optional link to a copy of the full log hosted elsewhere — "
                           "the log itself is never stored by this app, only log_summary below"
    )
    log_summary: FlightLogSummary | None = Field(
        None, description="Extracted from an uploaded .bin/.log at add-time; the source file "
                           "is discarded immediately after parsing"
    )
    wind_speed_mps: float | None = Field(
        None, ge=0, description="Wind speed at flight time, manually recorded (m/s)"
    )
    video_stream: bool = False
    threat_detection: bool = False
    precision_landing: bool = False
    notes: str | None = None


class Mission(BaseModel):
    """A named test campaign against one setup: a place, not a point in time.

    Deliberately carries no date — the same named mission (e.g. "Scouts V2
    Test Horana") legitimately runs again next week at the same site with the
    same setup, and each of its flights carries its own date and time.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    setup_id: str
    location: str | None = None
    description: str | None = None
    flights: list[MissionFlight] = Field(default_factory=list)

    @property
    def flight_count(self) -> int:
        return len(self.flights)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "mission"


def generate_mission_id(name: str, existing_ids: Iterable[str]) -> str:
    """A stable, readable id derived from `name`, auto-generated so nobody has to
    invent a slug by hand. Mission names are not unique — the same recurring
    test ("Scouts V2 field test") legitimately runs again on a different day —
    so a name collision is resolved with a numeric suffix rather than refused.
    """
    existing = set(existing_ids)
    base = _slugify(name)
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"
