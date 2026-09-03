"""ArduPilot log parsing: the summary math, plus the error paths of `parse_log`.

The row-extraction loop in `_read_messages` is thin glue around pymavlink's own
(well-tested) message iterator, so it is exercised indirectly through
`test_missions.py`'s and the app's manual testing against a real dataflash log
rather than reproduced here. What is worth unit-testing in this codebase is the
summary math this module adds on top: sag detection, mAh/energy integration,
event classification and downsampling - all pure functions over fabricated rows.
"""

from __future__ import annotations

import os

import pytest

from dronecalc.ardupilot_log import (
    NAV_SPEED_THRESHOLD_MPS,
    LogParseError, _classify_msg_level, _compute_distances, _compute_rate_series,
    _decode_fence_bitmask, _derive_dates, _downsample, _downsample_series, _err_event,
    _haversine_m, _mission_cmd_label, _summarise_battery, _sustained_speed_window, parse_log,
)


def test_sag_prefers_resting_vs_loaded_voltage():
    """When VoltR (resting) samples exist, sag is the largest resting-loaded gap,
    not just start-minus-minimum - that is the physically meaningful number."""
    rows = [(0.0, 12.6, 5.0), (1.0, 12.4, 20.0), (2.0, 12.6, 5.0)]
    warnings: list[str] = []
    battery = _summarise_battery(rows, sag_candidates=[0.05, 0.35, 0.05], mah_first=100.0,
                                  mah_last=250.0, armed_t_s=0.0, disarmed_t_s=2.0, warnings=warnings)
    assert battery.sag_v == 0.35
    assert not warnings


def test_sag_falls_back_to_start_minus_min_without_voltr():
    rows = [(0.0, 12.6, 5.0), (1.0, 12.1, 20.0), (2.0, 12.5, 5.0)]
    warnings: list[str] = []
    battery = _summarise_battery(rows, sag_candidates=[], mah_first=None, mah_last=None,
                                  armed_t_s=0.0, disarmed_t_s=2.0, warnings=warnings)
    assert battery.sag_v == pytest.approx(12.6 - 12.1)
    assert any("no VoltR" in w for w in warnings)


def test_v_start_and_v_end_use_nearest_sample_to_arm_and_disarm():
    """v_end is 'voltage at landing' - the reading nearest the disarm time, not
    simply the last row in the log (which may run past disarm)."""
    rows = [(0.0, 12.6, 1.0), (5.0, 12.0, 15.0), (10.0, 11.5, 1.0), (12.0, 11.4, 0.1)]
    battery = _summarise_battery(rows, sag_candidates=[], mah_first=None, mah_last=None,
                                  armed_t_s=0.0, disarmed_t_s=10.0, warnings=[])
    assert battery.v_start == 12.6
    assert battery.v_end == 11.5
    assert battery.v_min == 11.4
    assert battery.v_min_t_s == 12.0


def test_mah_consumed_is_a_delta_not_the_raw_total():
    """CurrTot is a running total from pack insertion, not from this flight -
    consumption is last minus first, not the raw field."""
    rows = [(0.0, 12.6, 5.0), (1.0, 12.4, 5.0)]
    battery = _summarise_battery(rows, sag_candidates=[], mah_first=500.0, mah_last=650.0,
                                  armed_t_s=0.0, disarmed_t_s=1.0, warnings=[])
    assert battery.mah_consumed == 150.0


def test_energy_integrates_when_synchronous_voltage_and_current_exist():
    """A steady 12 V @ 10 A for 3600 s is exactly 120 Wh - a direct check on the
    trapezoidal integration, not just that it runs."""
    rows = [(0.0, 12.0, 10.0), (3600.0, 12.0, 10.0)]
    battery = _summarise_battery(rows, sag_candidates=[], mah_first=None, mah_last=None,
                                  armed_t_s=0.0, disarmed_t_s=3600.0, warnings=[])
    assert battery.energy_wh == pytest.approx(120.0)
    assert battery.energy_wh_is_estimated is False


def test_energy_falls_back_to_mah_times_mean_voltage_and_is_flagged():
    rows = [(0.0, 12.0, None), (1.0, 11.8, None)]
    battery = _summarise_battery(rows, sag_candidates=[], mah_first=0.0, mah_last=1000.0,
                                  armed_t_s=0.0, disarmed_t_s=1.0, warnings=[])
    assert battery.energy_wh == pytest.approx(1.0 * 11.9)
    assert battery.energy_wh_is_estimated is True


def test_empty_rows_yield_an_all_none_summary_not_a_crash():
    battery = _summarise_battery([], sag_candidates=[], mah_first=None, mah_last=None,
                                  armed_t_s=None, disarmed_t_s=None, warnings=[])
    assert battery.v_start is None
    assert battery.energy_wh is None


def test_classify_msg_level_flags_failure_keywords():
    assert _classify_msg_level("PreArm: Compass calibration failed") == "warning"
    assert _classify_msg_level("GPS Glitch") == "warning"
    assert _classify_msg_level("New mission") == "info"


def test_downsample_keeps_first_and_last_point():
    rows = [(float(k), 12.0, 5.0) for k in range(1000)]
    points = _downsample(rows, target=50)
    assert points[0].t_s == 0.0
    assert points[-1].t_s == 999.0
    assert len(points) <= 55  # stride-based, allow a little slop


def test_downsample_computes_power_only_when_both_present():
    rows = [(0.0, 12.0, 5.0), (1.0, 12.0, None)]
    points = _downsample(rows, target=10)
    by_t = {p.t_s: p for p in points}
    assert by_t[0.0].power_w == pytest.approx(60.0)
    assert by_t[1.0].power_w is None


def test_downsample_series_keeps_first_and_last_point():
    rows = [(float(k), float(k) * 2) for k in range(1000)]
    points = _downsample_series(rows, target=50)
    assert points[0].t_s == 0.0 and points[0].value == 0.0
    assert points[-1].t_s == 999.0 and points[-1].value == 1998.0
    assert len(points) <= 55  # stride-based, allow a little slop


def test_downsample_series_empty_input():
    assert _downsample_series([], target=50) == []


def test_downsample_series_shorter_than_target_is_unchanged():
    rows = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    points = _downsample_series(rows, target=50)
    assert [(p.t_s, p.value) for p in points] == rows


def test_derive_dates_uses_arm_time_not_log_start():
    """A log that starts 90s before arming (boot, prearm checks) must date the
    flight from the arm event, not the log's own start - otherwise several
    same-day flights would sort in the wrong order."""
    epoch_2026_01_01_noon = 1767268800.0  # a fixed, known-good UTC instant
    log_date, flown_at = _derive_dates(epoch_2026_01_01_noon - 90, armed_t_s=90.0)
    assert log_date == "2026-01-01"
    assert flown_at == "2026-01-01 12:00"


def test_derive_dates_returns_none_with_no_usable_absolute_time():
    """A log with no GPS fix / unset RTC times out near the Unix epoch - not a
    real recording date, so both fields stay unset rather than showing 1970."""
    log_date, flown_at = _derive_dates(0.0, armed_t_s=5.0)
    assert log_date is None
    assert flown_at is None


def test_haversine_matches_a_known_reference_distance():
    """1 degree of latitude is ~111.19 km at the equator - a standard sanity check."""
    d = _haversine_m(0.0, 0.0, 1.0, 0.0)
    assert d == pytest.approx(111_195, rel=0.001)


def test_haversine_zero_for_identical_points():
    assert _haversine_m(12.34, 56.78, 12.34, 56.78) == pytest.approx(0.0)


def test_sustained_speed_window_ignores_a_single_noisy_sample():
    """One stray fast reading in an otherwise slow (hover) track must not open
    a navigation window - GPS ground speed is noisy at low speed."""
    speeds = [0.5, 0.5, 6.0, 0.5, 0.5, 0.5]  # single spike at index 2
    assert _sustained_speed_window(speeds, threshold=3.0, min_samples=3) is None


def test_sustained_speed_window_spans_first_to_last_sustained_run():
    speeds = [0.5, 5.0, 5.0, 5.0, 0.5, 0.5, 5.0, 5.0, 5.0, 0.5]
    window = _sustained_speed_window(speeds, threshold=3.0, min_samples=3)
    assert window == (1, 8)


def test_sustained_speed_window_none_columns_never_count_as_fast():
    speeds = [None, None, None]
    assert _sustained_speed_window(speeds, threshold=3.0, min_samples=1) is None


def test_compute_distances_needs_at_least_two_fixes():
    total, nav, nav_duration, warnings = _compute_distances([(0.0, 0.0, 0.0, 1.0)])
    assert total is None and nav is None and nav_duration is None
    assert any("fewer than 2 GPS fixes" in w for w in warnings)


def test_compute_distances_excludes_hover_only_legs_from_navigation():
    """A synthetic hover -> cruise -> hover flight: total distance covers the
    whole track, navigation distance/duration only the sustained-speed middle
    section - "after takeoff completed, before landing began"."""
    track = []
    lat = 0.0
    for i in range(10):
        speed = 0.5 if i < 3 or i >= 7 else 5.0
        track.append((float(i), lat, 0.0, speed))
        if speed >= NAV_SPEED_THRESHOLD_MPS:
            lat += 0.0001  # ~11.1 m north per step while "cruising"
    total, nav, nav_duration, warnings = _compute_distances(track)
    assert not warnings
    assert nav is not None and total is not None
    assert 0 < nav < total
    # Cruise legs are indices 3..6 (inclusive), 1 s apart -> a 3 s window.
    assert nav_duration == pytest.approx(3.0)
    assert nav_duration < track[-1][0] - track[0][0]  # shorter than the whole flight


def test_compute_distances_navigation_is_none_for_a_pure_hover_test():
    track = [(float(i), 0.0, 0.0, 0.3) for i in range(5)]  # GPS jitter only
    total, nav, nav_duration, warnings = _compute_distances(track)
    assert total is not None
    assert nav is None
    assert nav_duration is None
    assert any(f"{NAV_SPEED_THRESHOLD_MPS:g} m/s" in w for w in warnings)


def test_err_event_translates_thrust_loss_subsystem():
    """Subsys 25 is ArduCopter's THRUST_LOSS_CHECK - verified against
    ArduCopter/crash_check.cpp, not guessed - so it gets plain English."""
    e = _err_event(12.5, subsys=25, ecode=1)
    assert e.subsystem == "Thrust loss check"
    assert "thrust loss" in e.message.lower()
    assert e.level == "error"


def test_err_event_translates_crash_check_subsystem():
    """Subsys 12 is ArduCopter's CRASH_CHECK."""
    e = _err_event(12.5, subsys=12, ecode=1)
    assert e.subsystem == "Crash check"
    assert "crash" in e.message.lower()


def test_err_event_leaves_other_subsystems_as_raw_codes():
    """Every other subsystem stays untranslated - see the module's own
    'raw codes only' design note."""
    e = _err_event(12.5, subsys=7, ecode=2)
    assert e.subsystem == "Subsys 7"
    assert e.message == "Error code 2"


def test_err_event_handles_missing_subsys_or_ecode():
    e = _err_event(12.5, subsys=None, ecode=None)
    assert e.subsystem is None
    assert e.message == "error logged"


def test_err_event_translates_geofence_breach_and_clear():
    """Subsys 9 is FAILSAFE_FENCE - verified against ArduCopter/fence.cpp.
    ECode 0 is the matching 'resolved' entry, not an error."""
    breach = _err_event(12.5, subsys=9, ecode=1)  # bit 1 = max altitude
    assert breach.subsystem == "Geofence"
    assert breach.level == "error"
    assert "max altitude" in breach.message

    cleared = _err_event(13.0, subsys=9, ecode=0)
    assert cleared.level == "info"
    assert "cleared" in cleared.message.lower()


def test_decode_fence_bitmask_combines_multiple_breached_fences():
    """AC_Fence.h bit values: 1=max altitude, 2=circle, 4=polygon, 8=min
    altitude - more than one can breach at once."""
    assert _decode_fence_bitmask(1) == "max altitude"
    assert _decode_fence_bitmask(2) == "circle"
    assert _decode_fence_bitmask(4) == "polygon"
    assert _decode_fence_bitmask(8) == "min altitude"
    assert _decode_fence_bitmask(1 | 2) == "max altitude + circle"
    assert _decode_fence_bitmask(0) == "cleared"
    assert _decode_fence_bitmask(None) == "cleared"


def test_mission_cmd_label_marks_takeoff_and_land():
    """MAV_CMD ids 22/21 - verified against pymavlink's MAV_CMD enum."""
    assert _mission_cmd_label(22, 5) == ("T", "Takeoff")
    assert _mission_cmd_label(21, 9) == ("L", "Land")


def test_mission_cmd_label_uses_sequence_number_for_other_commands():
    assert _mission_cmd_label(16, 3) == ("3", "Waypoint 3")  # NAV_WAYPOINT
    assert _mission_cmd_label(None, None) == ("?", "Command")


def test_compute_rate_series_from_evenly_spaced_arrivals():
    points = _compute_rate_series([0.0, 1.0, 2.0, 3.0], max_points=100)
    assert [p.hz for p in points] == [1.0, 1.0, 1.0]
    assert [p.t_s for p in points] == [1.0, 2.0, 3.0]


def test_compute_rate_series_reflects_faster_and_slower_arrivals():
    # 10 Hz burst, then a 1-second gap
    points = _compute_rate_series([0.0, 0.1, 0.2, 1.2], max_points=100)
    hz = [round(p.hz, 2) for p in points]
    assert hz == [10.0, 10.0, 1.0]


def test_compute_rate_series_needs_at_least_two_timestamps():
    assert _compute_rate_series([], max_points=100) == []
    assert _compute_rate_series([5.0], max_points=100) == []


def test_compute_rate_series_downsamples_long_runs():
    timestamps = [float(i) for i in range(2000)]  # 1 Hz for 2000 samples
    points = _compute_rate_series(timestamps, max_points=100)
    assert len(points) <= 115  # stride-based, small slop allowed
    assert points[0].t_s < points[-1].t_s


def test_parse_log_rejects_a_non_log_file_without_crashing(tmp_path):
    garbage = tmp_path / "not_a_log.bin"
    garbage.write_bytes(b"this is not an ArduPilot dataflash log" * 500)
    with pytest.raises(LogParseError):
        parse_log(garbage)


def test_parse_log_rejects_a_missing_file(tmp_path):
    with pytest.raises(LogParseError):
        parse_log(tmp_path / "does_not_exist.bin")


def test_parse_log_does_not_leak_bad_header_spam_to_stderr(tmp_path, capfd):
    """pymavlink's fast indexer writes resync diagnostics straight to the OS
    stderr file descriptor; parse_log must suppress that regardless (see
    _suppress_native_stderr), not just quiet Python-level sys.stderr."""
    garbage = tmp_path / "not_a_log.bin"
    garbage.write_bytes(os.urandom(4096))
    with pytest.raises(LogParseError):
        parse_log(garbage)
    captured = capfd.readouterr()
    assert "bad header" not in captured.err
