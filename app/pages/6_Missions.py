"""Mission log: routine test flights recorded against a saved setup."""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import get_library, page_header, reload_library
from dronecalc.ardupilot_log import LogParseError, parse_log
from dronecalc.missions import FlightLogSummary, Mission, MissionFlight, generate_mission_id

st.set_page_config(page_title="Missions", page_icon="📋", layout="wide")
page_header("Mission log", "Test flights per setup, with wind and ArduPilot log data.")

lib = get_library()
if not lib.list_ids("setups"):
    st.error("No setups found. Build one on the Setups page first.")
    st.stop()

mission_ids = lib.list_ids("missions")
browse, add = st.tabs([f"Browse ({len(mission_ids)})", "Log new mission"])

#: Grouped by kind rather than insertion order: identity first (pinned date +
#: whether a log is attached), then what was manually recorded for the test,
#: then what the log derived, then free text and the rarely-used external
#: link last - so the columns you actually look for when scanning are up front.
FLIGHT_COLUMNS = [
    "date", "has_log",
    "wind_speed_mps", "video_stream", "threat_detection", "precision_landing",
    "duration", "nav_duration", "navigation_distance_m", "total_distance_m",
    "notes", "ardupilot_log_url",
]


def _fmt_t(t_s: float) -> str:
    """Seconds since log start as MM:SS — flights are minutes long, not hours."""
    m, s = divmod(max(0.0, t_s), 60.0)
    return f"{int(m):02d}:{s:05.2f}"


def _parse_date_time(value: str | None) -> tuple[_dt.date, _dt.time]:
    """A flight's stored date string -> (date, time), for prefilling the date
    and time picker widgets. Not a strict format - older entries may be
    date-only or hand-typed. A date-only value's time is honestly unknown, so
    it defaults to midnight rather than the current time; a missing or
    unparseable value defaults to the current system date and time, matching
    what a brand new flight starts at.
    """
    if value:
        value = value.strip()
        for fmt, has_time in (("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False)):
            try:
                parsed = _dt.datetime.strptime(value, fmt)
                return parsed.date(), (parsed.time() if has_time else _dt.time(0, 0))
            except ValueError:
                continue
    now = _dt.datetime.now()
    return now.date(), now.time().replace(second=0, microsecond=0)


def _combine_date_time(d: _dt.date, t: _dt.time) -> str:
    return f"{d.isoformat()} {t.strftime('%H:%M')}"


def _flight_df(mission: Mission) -> pd.DataFrame:
    """The sheet: summary columns only, read-only, one row per flight in
    `mission.flights` order. Duration and both distances are read straight
    from each flight's log_summary, not typed in. It is deliberately a plain
    `st.dataframe`, not a `st.data_editor` — clicking a row is what opens that
    flight's edit form and log details below, and row *selection* is a
    dataframe-only feature the editor variant doesn't have. Since nothing here
    adds/removes/reorders rows, a selected row's position is always that
    flight's index in `mission.flights` - no separate id column needed."""
    if not mission.flights:
        return pd.DataFrame(columns=FLIGHT_COLUMNS)
    rows = []
    for f in mission.flights:
        row = f.model_dump()
        ls = f.log_summary
        row["has_log"] = ls is not None
        row["duration"] = _fmt_t(ls.duration_s) if ls and ls.duration_s is not None else None
        row["nav_duration"] = (
            _fmt_t(ls.navigation_duration_s) if ls and ls.navigation_duration_s is not None else None
        )
        row["navigation_distance_m"] = ls.navigation_distance_m if ls else None
        row["total_distance_m"] = ls.total_distance_m if ls else None
        rows.append(row)
    return pd.DataFrame(rows)[FLIGHT_COLUMNS]


_LEVEL_ICON = {"error": "🔴 error", "warning": "🟡 warning", "info": "⚪ info"}
#: New-name / new-location sentinel for the "pick or type" pattern below.
_NEW = "➕ New…"


def _pick_or_new(label: str, existing: list[str], *, key: str, placeholder: str = "") -> str | None:
    """A dropdown of previously used values plus a free-text option.

    Names and locations both repeat across missions (the same recurring test
    site, the same campaign name run again next week), so typing them fresh
    every time just invites typos that fracture what should be one group.
    """
    options = existing + [_NEW]
    choice = st.selectbox(label, options, index=len(options) - 1, key=f"{key}::pick")
    if choice != _NEW:
        return choice
    return st.text_input(f"New {label.lower()}", placeholder=placeholder, key=f"{key}::new")


def _mission_label(m: Mission, dupe_names: set[str]) -> str:
    return f"{m.name}  [{m.id}]" if m.name in dupe_names else m.name


def _parse_uploaded_log(uploaded_file) -> FlightLogSummary | None:
    """Parse an `st.file_uploader` result and discard the bytes immediately after.

    The file is written to a temp path only because pymavlink's DFReader reads
    from a filesystem path (it mmaps the file), not a bytes buffer. The temp
    file is deleted in `finally` regardless of outcome — nothing here is ever
    written under `data/`, and nothing raw is returned to the caller.
    """
    suffix = Path(uploaded_file.name).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name
    try:
        return parse_log(tmp_path)
    except LogParseError as exc:
        st.error(f"Could not read '{uploaded_file.name}': {exc}")
        return None
    finally:
        os.unlink(tmp_path)


def _add_event_markers(fig: go.Figure, summary: FlightLogSummary) -> None:
    """Minimal vertical markers shared by every time-series plot for a
    flight: armed/landing, takeoff/land/waypoint number, and geofence
    breaches. Single-token labels only — this is meant to be glanced at, not
    read, so it must not compete with the data for attention."""
    if summary.armed_t_s is not None:
        fig.add_vline(x=summary.armed_t_s, line_dash="dot", line_width=1, line_color="#2ea043")
    if summary.disarmed_t_s is not None:
        fig.add_vline(x=summary.disarmed_t_s, line_dash="dot", line_width=1, line_color="#e5484d")
    for ev in summary.mission_events:
        fig.add_vline(
            x=ev.t_s, line_dash="dot", line_width=1, line_color="#8b949e",
            annotation_text=ev.label, annotation_font_size=9,
            annotation_font_color="#8b949e", annotation_position="top",
        )
    for ev in summary.events:
        if ev.subsystem == "Geofence" and ev.level == "error":
            fig.add_vline(
                x=ev.t_s, line_dash="dash", line_width=1, line_color="#cf9d4f",
                annotation_text="GF", annotation_font_size=9,
                annotation_font_color="#cf9d4f", annotation_position="bottom",
            )


def _render_log_summary(summary: FlightLogSummary, *, key: str) -> None:
    """Metrics, a Plotly voltage/current/power chart, and a timestamped event
    table — shared between the "add flight" preview and viewing a saved flight.

    `key` must be unique per call site *and* per flight - two calls that would
    otherwise build an identical chart (e.g. the same summary shown in both the
    upload preview and a saved flight's expander) collide on Streamlit's
    auto-generated element id without it.
    """
    for w in summary.warnings:
        st.warning(w)
    if summary.crash_events > 0:
        st.error(f"🚨 {summary.crash_events} crash event(s) — vehicle auto-disarmed.")
    if summary.thrust_loss_events > 0:
        st.warning(f"⚠️ {summary.thrust_loss_events} potential thrust-loss event(s).")
    if summary.geofence_breach_events > 0:
        st.warning(f"🚧 {summary.geofence_breach_events} geofence breach event(s).")

    b = summary.battery
    m = st.columns(6)
    m[0].metric("Duration", f"{_fmt_t(summary.duration_s)}" if summary.duration_s else "—")
    m[1].metric("V start", f"{b.v_start:.2f} V" if b.v_start is not None else "—")
    m[2].metric("V min", f"{b.v_min:.2f} V" if b.v_min is not None else "—")
    m[3].metric("V at landing", f"{b.v_end:.2f} V" if b.v_end is not None else "—")
    m[4].metric("Sag", f"{b.sag_v:.2f} V" if b.sag_v is not None else "—")
    m[5].metric("Peak current", f"{b.i_max:.1f} A" if b.i_max is not None else "—")

    energy_label = "Energy used" + (" (est.)" if b.energy_wh_is_estimated else "")
    m2 = st.columns(5)
    m2[0].metric("mAh consumed", f"{b.mah_consumed:.0f}" if b.mah_consumed is not None else "—")
    m2[1].metric(energy_label, f"{b.energy_wh:.1f} Wh" if b.energy_wh is not None else "—")
    m2[2].metric("Thrust loss events", summary.thrust_loss_events)
    m2[3].metric("Crash events", summary.crash_events)
    m2[4].metric("Geofence breaches", summary.geofence_breach_events)

    dist_bits = []
    if summary.total_distance_m is not None:
        dist_bits.append(f"{summary.total_distance_m:.0f} m total")
    if summary.navigation_distance_m is not None:
        nav_bit = f"{summary.navigation_distance_m:.0f} m navigation"
        if summary.navigation_duration_s is not None:
            nav_bit += f" ({_fmt_t(summary.navigation_duration_s)})"
        dist_bits.append(nav_bit)
    if dist_bits:
        st.caption(", ".join(dist_bits))

    # One compact chart, tick boxes choose what's plotted. Voltage gets its
    # own axis (left); everything else shares a second axis (right) — each
    # trace only exists on the figure when its box is ticked, so that axis
    # auto-scales to whatever is actually shown rather than a fixed range
    # that would swamp e.g. a 0-2 m/s flow rate next to a 300 W power trace.
    series_defs: list[dict] = []
    if summary.series:
        t = [p.t_s for p in summary.series]
        series_defs += [
            dict(label="Voltage (V)", default=True, trace=go.Scatter(
                x=t, y=[p.voltage_v for p in summary.series],
                name="Voltage (V)", line=dict(color="#0969da"), yaxis="y1")),
            dict(label="Current (A)", default=True, trace=go.Scatter(
                x=t, y=[p.current_a for p in summary.series],
                name="Current (A)", line=dict(color="#cf222e"), yaxis="y2")),
            dict(label="Power (W)", default=True, trace=go.Scatter(
                x=t, y=[p.power_w for p in summary.series],
                name="Power (W)", line=dict(color="#9a6700", dash="dot"), yaxis="y2")),
        ]
    if summary.landing_target_rate:
        series_defs.append(dict(label="Landing-target rate (Hz)", default=False, trace=go.Scatter(
            x=[p.t_s for p in summary.landing_target_rate],
            y=[p.hz for p in summary.landing_target_rate],
            name="Landing-target rate (Hz)", line=dict(color="#8250df"), yaxis="y2")))
    if summary.rangefinder_distance_m:
        series_defs.append(dict(label="Rangefinder distance (m)", default=False, trace=go.Scatter(
            x=[p.t_s for p in summary.rangefinder_distance_m],
            y=[p.value for p in summary.rangefinder_distance_m],
            name="Rangefinder distance (m)", line=dict(color="#1a7f37"), yaxis="y2")))
    if summary.optical_flow_rate_x:
        series_defs.append(dict(label="Flow rate X (rad/s)", default=False, trace=go.Scatter(
            x=[p.t_s for p in summary.optical_flow_rate_x],
            y=[p.value for p in summary.optical_flow_rate_x],
            name="Flow rate X (rad/s)", line=dict(color="#bf3989"), yaxis="y2")))
    if summary.optical_flow_rate_y:
        series_defs.append(dict(label="Flow rate Y (rad/s)", default=False, trace=go.Scatter(
            x=[p.t_s for p in summary.optical_flow_rate_y],
            y=[p.value for p in summary.optical_flow_rate_y],
            name="Flow rate Y (rad/s)", line=dict(color="#e16f24", dash="dot"), yaxis="y2")))

    if not series_defs:
        st.caption("No time-series data.")
    else:
        shown = []
        for row_start in range(0, len(series_defs), 4):
            row = series_defs[row_start:row_start + 4]
            for c, d in zip(st.columns(4), row):
                if c.checkbox(d["label"], value=d["default"], key=f"{key}::toggle::{d['label']}"):
                    shown.append(d["trace"])

        if not shown:
            st.caption("Nothing selected.")
        else:
            fig = go.Figure()
            for trace in shown:
                fig.add_trace(trace)
            _add_event_markers(fig, summary)
            fig.update_layout(
                height=380, margin=dict(l=10, r=10, t=30, b=10),
                xaxis=dict(title="Time (s)"),
                yaxis=dict(title="Voltage (V)"),
                yaxis2=dict(overlaying="y", side="right"),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
            )
            st.plotly_chart(fig, width="stretch", key=f"{key}::chart")

    if summary.events:
        st.markdown("###### Log messages")
        events_df = pd.DataFrame([
            {
                "Time": _fmt_t(e.t_s), "Level": _LEVEL_ICON.get(e.level, e.level),
                "Subsystem": e.subsystem or "", "Message": e.message,
            }
            for e in sorted(summary.events, key=lambda e: e.t_s)
        ])
        st.dataframe(
            events_df, width="stretch", hide_index=True,
            height=min(300, 40 + 35 * len(events_df)), key=f"{key}::events",
        )


def _render_flight_panel(mission: Mission, idx: int, flight: MissionFlight) -> None:
    """The editable form for one flight, plus its log details if it has one.
    This is what a row click in the sheet opens - editing and viewing are one
    panel, not split across a spreadsheet cell and a separate expander."""
    key_prefix = f"panel::{mission.id}::{idx}"
    default_date, default_time = _parse_date_time(flight.date)
    with st.form(f"{key_prefix}::form"):
        fc = st.columns(4)
        date_val = fc[0].date_input("Date", value=default_date, format="YYYY-MM-DD")
        time_val = fc[1].time_input("Time", value=default_time, step=60)
        wind = fc[2].number_input(
            "Wind speed (m/s)", 0.0, 60.0, flight.wind_speed_mps or 0.0, 0.5
        )
        ext_url = fc[3].text_input("External log link", flight.ardupilot_log_url or "")
        cc = st.columns(3)
        video_stream = cc[0].checkbox("Video stream", flight.video_stream)
        threat_detection = cc[1].checkbox("Threat detection", flight.threat_detection)
        precision_landing = cc[2].checkbox("Precision landing", flight.precision_landing)
        notes = st.text_area("Notes", flight.notes or "", height=68)

        bc = st.columns([1, 1, 4])
        save_clicked = bc[0].form_submit_button("Save changes", type="primary", icon="💾")
        delete_clicked = bc[1].form_submit_button("Delete this flight", icon="🗑️")

    if delete_clicked:
        mission.flights.pop(idx)
        lib.save("missions", mission)
        reload_library()
        st.session_state.pop(f"flights_select::{mission.id}", None)
        st.success("Flight deleted.")
        st.rerun()
    elif save_clicked:
        flight.date = _combine_date_time(date_val, time_val)
        flight.wind_speed_mps = wind
        flight.ardupilot_log_url = ext_url or None
        flight.video_stream = video_stream
        flight.threat_detection = threat_detection
        flight.precision_landing = precision_landing
        flight.notes = notes or None
        lib.save("missions", mission)
        reload_library()
        # Closes the dialog on save, same as delete - "submit and close" is the
        # expected pattern for a modal form. Attaching a log deliberately does
        # NOT clear this (see below): reopening on the same flight afterwards
        # is what lets the user see the log they just attached.
        st.session_state.pop(f"flights_select::{mission.id}", None)
        st.success("Saved.")
        st.rerun()

    st.divider()
    if flight.log_summary is not None:
        _render_log_summary(flight.log_summary, key=f"{key_prefix}::log")
    else:
        st.markdown("###### Attach an ArduPilot log")
        attach_uploaded = st.file_uploader(
            "ArduPilot log", type=["bin", "log"],
            key=f"{key_prefix}::attach_upload", label_visibility="collapsed",
        )
        # A name-keyed guard, not a confirm button: attaching happens the
        # moment a *new* file parses successfully. Once attached, this branch
        # stops rendering at all (log_summary is no longer None), so there's
        # no risk of re-attaching on an unrelated rerun.
        tried_key = f"{key_prefix}::attach_tried"
        if attach_uploaded is not None and st.session_state.get(tried_key) != attach_uploaded.name:
            st.session_state[tried_key] = attach_uploaded.name
            with st.spinner(f"Parsing {attach_uploaded.name}…"):
                attach_summary = _parse_uploaded_log(attach_uploaded)
            if attach_summary is not None:
                flight.log_summary = attach_summary
                # The log's own clock is authoritative - it overrides whatever
                # was typed in by hand, since a manually entered date/time can
                # be wrong in a way the log itself cannot.
                if attach_summary.flown_at:
                    flight.date = attach_summary.flown_at
                if not mission.location and attach_summary.takeoff_latlon:
                    lat, lon = attach_summary.takeoff_latlon
                    mission.location = f"{lat:.5f}, {lon:.5f}"
                lib.save("missions", mission)
                reload_library()
                # tried_key stays set to this filename - the uploader still holds
                # the same file across the rerun below, and popping the guard here
                # would let the next rerun see it as "new" again and re-attach it
                # in a loop (this exact bug, fixed after shipping).
                st.success(
                    "Log attached — date/time corrected."
                    if attach_summary.flown_at else "Log attached."
                )
                st.rerun()
        elif attach_uploaded is None:
            st.session_state.pop(tried_key, None)


def _clear_flight_dialog_selection() -> None:
    """`on_dismiss` callback for `_flight_dialog`.

    Dismissing (the dialog's own X, clicking outside, or Escape) has to also
    drop the sheet's row selection - otherwise the selection is unchanged, and
    the very next rerun for any unrelated reason on the page sees the same row
    still "selected" and reopens the dialog the user just closed.
    """
    key = st.session_state.pop("_flight_dialog_selection_key", None)
    if key:
        st.session_state.pop(key, None)


@st.dialog("Flight details", width="large", on_dismiss=_clear_flight_dialog_selection)
def _flight_dialog(mission: Mission, idx: int, flight: MissionFlight) -> None:
    """A row click opens this instead of scrolling to an inline panel - with
    many flights logged, the panel could be a long way below the row that
    opened it. Save/Delete close it (see `_render_flight_panel`); attaching a
    log deliberately leaves the row selected so the dialog reopens showing
    the newly attached log."""
    log_bit = flight.log_summary.source_filename if flight.log_summary else "no log"
    st.caption(f"{flight.date or 'Flight'} — {log_bit}")
    _render_flight_panel(mission, idx, flight)


# --------------------------------------------------------------------- browse
with browse:
    if not mission_ids:
        st.info("No missions logged yet. Add one on the next tab.")
    else:
        all_names = [lib.missions[i].name for i in mission_ids]
        dupe_names = {n for n in all_names if all_names.count(n) > 1}
        chosen = st.selectbox(
            "Mission", mission_ids,
            format_func=lambda i: _mission_label(lib.missions[i], dupe_names),
        )
        mission = lib.missions[chosen]
        setup = lib.setups.get(mission.setup_id)

        with st.container(border=True):
            head, meta = st.columns([3, 2])
            head.subheader(mission.name)
            if mission.description:
                head.caption(mission.description)
            with meta:
                st.markdown(f"🚁 **Setup:** {setup.name if setup else mission.setup_id + ' (missing)'}")
                st.markdown(f"📍 **Location:** {mission.location or '—'}")
            if setup is None:
                st.warning(f"Setup '{mission.setup_id}' not found.")

        st.write("")
        st.markdown("##### Add a flight")
        with st.container(border=True):
            uploaded = st.file_uploader(
                "ArduPilot log", type=["bin", "log"],
                key=f"upload::{mission.id}", label_visibility="collapsed",
            )
            # A name-keyed guard, not a confirm button: adding happens the moment a
            # *new* file parses successfully. Once added, this whole block stops
            # rendering (the uploader's own state still holds the file, but nothing
            # here reacts to it again), so there's no risk of re-adding on an
            # unrelated rerun.
            tried_key = f"tried_upload::{mission.id}"
            if uploaded is not None and st.session_state.get(tried_key) != uploaded.name:
                st.session_state[tried_key] = uploaded.name
                with st.spinner(f"Parsing {uploaded.name}…"):
                    summary = _parse_uploaded_log(uploaded)
                if summary is not None:
                    mission.flights.append(MissionFlight(
                        date=summary.flown_at or summary.log_date, log_summary=summary,
                    ))
                    if not mission.location and summary.takeoff_latlon:
                        lat, lon = summary.takeoff_latlon
                        mission.location = f"{lat:.5f}, {lon:.5f}"
                    lib.save("missions", mission)
                    reload_library()
                    # tried_key stays set to this filename - see the matching
                    # comment in _render_flight_panel's attach handler.
                    st.success("Flight added.")
                    st.rerun()
            elif uploaded is None:
                st.session_state.pop(tried_key, None)

            with st.expander("Add manually", icon="✏️"):
                with st.form(f"add_manual_flight::{mission.id}"):
                    fc = st.columns(4)
                    m_date_val = fc[0].date_input("Date", value="today", format="YYYY-MM-DD")
                    m_time_val = fc[1].time_input("Time", value="now", step=60)
                    m_wind = fc[2].number_input("Wind speed (m/s)", 0.0, 60.0, 0.0, 0.5)
                    m_url = fc[3].text_input("External log link (optional)", "")
                    cc = st.columns(3)
                    m_video = cc[0].checkbox("Video stream")
                    m_threat = cc[1].checkbox("Threat detection")
                    m_precision = cc[2].checkbox("Precision landing")
                    m_notes = st.text_area("Notes", "", height=68)
                    if st.form_submit_button("Add flight", type="primary", icon="➕"):
                        mission.flights.append(MissionFlight(
                            date=_combine_date_time(m_date_val, m_time_val),
                            wind_speed_mps=m_wind, ardupilot_log_url=m_url or None,
                            video_stream=m_video, threat_detection=m_threat, precision_landing=m_precision,
                            notes=m_notes or None,
                        ))
                        lib.save("missions", mission)
                        reload_library()
                        st.success("Flight added.")
                        st.rerun()

        st.write("")
        st.markdown("##### Flights")
        with st.container(border=True):
            flight_select = st.dataframe(
                _flight_df(mission),
                width="stretch", hide_index=True, key=f"flights_select::{mission.id}",
                on_select="rerun", selection_mode="single-row",
                column_order=FLIGHT_COLUMNS,
                column_config={
                    "date": st.column_config.TextColumn("Date", pinned=True, width="medium"),
                    "has_log": st.column_config.CheckboxColumn(
                        "Log", width="small", help="ArduPilot log attached"
                    ),
                    "wind_speed_mps": st.column_config.NumberColumn("Wind (m/s)", width="small"),
                    "video_stream": st.column_config.CheckboxColumn("Video", width="small"),
                    "threat_detection": st.column_config.CheckboxColumn("Threat", width="small"),
                    "precision_landing": st.column_config.CheckboxColumn("Prec. landing", width="small"),
                    "duration": st.column_config.TextColumn(
                        "Duration", width="small", help="From the log, arm to disarm"
                    ),
                    "nav_duration": st.column_config.TextColumn(
                        "Nav. duration", width="small", help="Cruise-speed duration"
                    ),
                    "navigation_distance_m": st.column_config.NumberColumn(
                        "Nav. distance", format="%.0f m", width="small",
                        help="Distance at cruise speed",
                    ),
                    "total_distance_m": st.column_config.NumberColumn(
                        "Distance travelled", format="%.0f m", width="small",
                        help="Total distance, arm to disarm",
                    ),
                    "notes": st.column_config.TextColumn("Notes", width="large"),
                    "ardupilot_log_url": st.column_config.LinkColumn(
                        "External log link", width="small", help="Optional hosted copy"
                    ),
                },
            )
            st.caption(f"{mission.flight_count} flight(s) logged.")

        selected_rows = flight_select.selection.rows if flight_select.selection else []
        if selected_rows:
            idx = selected_rows[0]
            # Read by the dialog's on_dismiss callback, so closing it (X,
            # click-outside, Escape) also clears this row's selection.
            st.session_state["_flight_dialog_selection_key"] = f"flights_select::{mission.id}"
            _flight_dialog(mission, idx, mission.flights[idx])

        st.write("")
        st.markdown("##### Mission settings")
        with st.container(border=True):
            with st.expander("Edit name, setup, location or description"):
                with st.form(f"edit_mission::{mission.id}"):
                    e_name = st.text_input("Name", mission.name)
                    e_setup = st.selectbox(
                        "Setup", lib.list_ids("setups"),
                        index=lib.list_ids("setups").index(mission.setup_id)
                        if mission.setup_id in lib.list_ids("setups") else 0,
                        format_func=lambda i: lib.setups[i].name,
                    )
                    e_location = st.text_input("Location", mission.location or "")
                    e_desc = st.text_area("Description", mission.description or "", height=70)
                    if st.form_submit_button("Save overview", type="primary"):
                        try:
                            mission.name = e_name
                            mission.setup_id = e_setup
                            mission.location = e_location or None
                            mission.description = e_desc or None
                            lib.save("missions", mission)
                            reload_library()
                            st.success("Saved.")
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Could not save: {exc}")

            with st.expander("🗑️ Delete this mission"):
                st.warning(f"Removes '{mission.name}' and its {mission.flight_count} flight(s).")
                if st.button("Delete mission", icon="🗑️", key=f"del::{mission.id}"):
                    lib.delete("missions", mission.id)
                    reload_library()
                    st.rerun()

# ------------------------------------------------------------------------ add
with add:
    existing_names = sorted({m.name for m in lib.all_missions()})
    existing_locations = sorted({m.location for m in lib.all_missions() if m.location})

    center, _ = st.columns([2, 1])
    with center, st.container(border=True), st.form("add_mission"):
        name = _pick_or_new(
            "Mission name", existing_names, key="new_mission_name",
            placeholder="Scouts V2 field test",
        )
        location = _pick_or_new(
            "Location", existing_locations, key="new_mission_location",
            placeholder="Field site, GPS coordinates, etc.",
        )
        setup_id = st.selectbox(
            "Setup", lib.list_ids("setups"), format_func=lambda i: lib.setups[i].name
        )

        if st.form_submit_button("Create mission", type="primary", icon="➕"):
            try:
                if not name or not name.strip():
                    raise ValueError("Mission name is required")
                mission_id = generate_mission_id(name, lib.list_ids("missions"))
                mission = Mission(
                    id=mission_id, name=name.strip(), setup_id=setup_id,
                    location=(location or "").strip() or None,
                )
                lib.save("missions", mission)
                reload_library()
                st.success(f"Created '{mission.name}'.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not create: {exc}")
