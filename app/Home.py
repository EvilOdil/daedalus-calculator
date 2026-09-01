"""Dashboard: every metric for one setup, live."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import (
    STATUS_COLOUR,
    analyse,
    confidence_badge,
    get_library,
    page_header,
    setup_picker,
    sidebar_controls,
)

st.set_page_config(page_title="Daedalus Calculator", page_icon="🚁", layout="wide")

page_header("Daedalus Calculator")

lib = get_library()
setup = setup_picker()
resolved = sidebar_controls(lib.resolve(setup))

try:
    m = analyse(resolved)
except Exception as exc:  # noqa: BLE001 - infeasible setups are a normal outcome
    st.error(f"This configuration has no hover solution: {exc}")
    st.stop()

hp, e, sens = m.hover, m.endurance, m.sensitivity

if setup.description:
    st.info(setup.description)

for e_ in lib.compatibility_errors(resolved):
    st.error(e_)
for w in lib.compatibility_warnings(resolved):
    st.warning(w)
for w in m.warnings:
    st.warning(w)

# ------------------------------------------------------------------ headline
c = st.columns(5)
c[0].metric("Hover endurance", f"{e.minutes:.1f} min", help=f"Ends at: {e.terminated_by}")
c[1].metric(
    "Hover efficiency", f"{hp.hover_efficiency_g_per_w:.2f} g/W",
    help="All-up weight divided by total bus power, payload draw included. "
         "This is the number that most directly sets flight time.",
)
c[2].metric(
    "Thrust to weight", f"{hp.thrust_to_weight:.2f}",
    delta="target >= 2.0", delta_color="off",
    help=f"Available thrust is limited by: {hp.thrust_limited_by}",
)
c[3].metric(
    "Thrust used in hover", f"{hp.thrust_utilisation * 100:.0f} %",
    help="Keep at or below 50% for control authority.",
)
c[4].metric("All-up weight", f"{hp.auw_g:.0f} g")

st.divider()
left, right = st.columns([3, 2])

# ------------------------------------------------------------------- rotor
with left:
    st.subheader("Per rotor at hover")
    r = hp.rotor
    rotor_rows = [
        ("Thrust", f"{r.thrust_g:.0f} g", "one of four"),
        ("Speed", f"{r.rpm:.0f} rpm", ""),
        ("Shaft power", f"{r.shaft_power_w:.1f} W", ""),
        ("Battery-side current", f"{r.bus_current_a:.2f} A", "what ESC ratings refer to"),
        ("Bus power", f"{r.esc_bus_power_w:.1f} W", ""),
        ("Motor efficiency", f"{r.motor_efficiency * 100:.1f} %", ""),
        ("Propeller efficiency", f"{r.prop_efficiency_g_per_w:.2f} g/W", "thrust per shaft watt"),
        ("Overall efficiency", f"{r.overall_efficiency_g_per_w:.2f} g/W", "thrust per bus watt"),
        ("Disc loading", f"{hp.disc_loading_n_per_m2:.1f} N/m²", "lower is more efficient"),
        ("Throttle (estimated)", f"{r.duty * 100:.0f} %",
         "duty cycle from the electrical model; indicative only, see README"),
    ]
    st.dataframe(
        pd.DataFrame(rotor_rows, columns=["Metric", "Value", "Note"]),
        hide_index=True, width="stretch",
    )

    st.subheader("Pack")
    b = hp.battery
    pack_rows = [
        ("Loaded voltage", f"{b.voltage_v:.2f} V", f"{b.cell_voltage_v:.2f} V per cell"),
        ("Sag under hover load", f"{b.sag_v:.2f} V", ""),
        ("Pack current", f"{b.current_a:.1f} A", f"{hp.c_rate:.2f}C"),
        ("Usable energy", f"{e.usable_wh:.1f} Wh", f"at {resolved.setup.assumptions.dod_limit:.0%} DoD"),
        ("Delivered before cutoff", f"{e.delivered_wh:.1f} Wh", f"reached {e.dod_reached:.0%} DoD"),
        ("Pack specific energy", f"{resolved.battery.specific_energy_wh_per_kg:.0f} Wh/kg", ""),
        ("Battery mass fraction", f"{resolved.battery.weight_g / hp.auw_g * 100:.0f} %", ""),
    ]
    st.dataframe(
        pd.DataFrame(pack_rows, columns=["Metric", "Value", "Note"]),
        hide_index=True, width="stretch",
    )

# ------------------------------------------------------------------ margins
with right:
    st.subheader("Margins")
    st.caption("How close each component runs to its rating in hover.")
    for mg in m.margins:
        u = min(mg.utilisation, 1.0)
        colour = STATUS_COLOUR[mg.status]
        st.markdown(
            f"<div style='margin-bottom:2px'><span style='font-size:0.85rem'>{mg.name}</span>"
            f"<span style='float:right;font-size:0.85rem;color:{colour};font-weight:600'>"
            f"{mg.utilisation * 100:.0f}%</span></div>"
            f"<div style='background:rgba(128,128,128,0.25);border-radius:3px;height:7px'>"
            f"<div style='background:{colour};width:{u * 100:.1f}%;height:7px;border-radius:3px'>"
            f"</div></div>"
            f"<div style='opacity:0.65;font-size:0.72rem;margin-bottom:9px'>"
            f"{mg.value:.2f} / {mg.limit:.2f} {mg.unit}{' - ' + mg.note if mg.note else ''}</div>",
            unsafe_allow_html=True,
        )

    st.subheader("Mass budget")
    mass = resolved.mass_breakdown_g()
    fig = go.Figure(go.Bar(
        x=list(mass.values()), y=list(mass.keys()), orientation="h",
        text=[f"{v:.0f} g" for v in mass.values()], textposition="auto",
    ))
    fig.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0), xaxis_title="grams")
    st.plotly_chart(fig, width="stretch")

st.divider()

# ------------------------------------------------------------- power budget
pcol, scol = st.columns([3, 2])
with pcol:
    st.subheader("Where the watts go")
    st.caption(
        "Accounted from the cells outward, so it closes exactly "
        f"(error {m.power.closure_error_pct:.3f}%). Induced power is the irreducible cost of "
        "holding the aircraft up; everything else is, in principle, attackable."
    )
    rows = m.power.rows()
    labels = [n for n, _, _ in rows]
    values = [w for _, w, _ in rows]
    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h",
        text=[f"{w:.1f} W ({f:.0%})" for _, w, f in rows], textposition="auto",
    ))
    fig.update_layout(
        height=300, margin=dict(l=0, r=0, t=10, b=0), xaxis_title="watts",
        yaxis=dict(autorange="reversed"),
    )
    st.plotly_chart(fig, width="stretch")

with scol:
    st.subheader("Sensitivity")
    st.metric(
        "Cost of mass", f"{abs(sens.seconds_per_gram):.2f} s/g",
        help="Seconds of endurance lost per gram added anywhere on the aircraft. "
             "The most actionable number here if you are designing a payload.",
    )
    st.metric(
        "Next 100 g of battery", f"{sens.minutes_per_100g_battery:+.2f} min",
        help="At this pack's own specific energy. When this approaches zero, a bigger "
             "battery has stopped paying for itself.",
    )
    if resolved.payload and resolved.payload.total_mass_g:
        st.markdown("**What the payload costs**")
        st.markdown(
            f"- carrying its {resolved.payload.total_mass_g:.0f} g: "
            f"**{sens.payload_mass_cost_minutes:.2f} min**\n"
            f"- powering its {resolved.payload.total_power_w:.1f} W: "
            f"**{sens.payload_tax_minutes:.2f} min**\n"
            f"- total: **{sens.payload_total_cost_minutes:.2f} min**"
        )
        st.caption(
            "Powering the payload from the flight pack is a real, separable cost - "
            "shown apart from the cost of carrying it."
        )

# ----------------------------------------------------------------- discharge
st.subheader("Discharge")
st.caption(
    "Marched down in depth-of-discharge steps. Falling open-circuit voltage forces rising "
    "current, which deepens sag and brings the cutoff forward - the mechanism behind the "
    f"Peukert-style penalty. Naive Wh/W would predict {e.minutes_naive:.1f} min."
)
t = e.trace
fig = go.Figure()
fig.add_trace(go.Scatter(x=t["minutes"], y=t["cell_v"], name="Loaded cell voltage", yaxis="y"))
fig.add_trace(go.Scatter(x=t["minutes"], y=t["current_a"], name="Pack current", yaxis="y2"))
fig.add_hline(
    y=resolved.battery.v_cutoff_per_cell, line_dash="dot",
    annotation_text=f"cutoff {resolved.battery.v_cutoff_per_cell:.2f} V/cell",
)
fig.update_layout(
    height=300, margin=dict(l=0, r=0, t=10, b=0), xaxis_title="minutes",
    yaxis=dict(title="V per cell"),
    yaxis2=dict(title="A", overlaying="y", side="right"),
    legend=dict(orientation="h", y=1.12),
)
st.plotly_chart(fig, width="stretch")

with st.expander("Model provenance and how this was computed"):
    st.markdown(
        f"**Tier {m.system.tier}** - "
        + (
            "a measured thrust table was found for this motor and propeller, reduced to "
            "propeller coefficients so it holds at any weight and any pack voltage."
            if m.system.tier == "A"
            else "no measured table for this pair, so momentum theory with a figure of merit "
                 "was used. Results are estimates."
        )
    )
    st.markdown(f"Library loaded from **{lib.backend.label}**.")
    if m.system.table is not None:
        t = m.system.table
        others = [
            x.display_name
            for x in m.system.resolved.motor.tables_for(m.system.resolved.prop.id)
            if x is not t
        ]
        st.markdown(
            f"Running on **{t.display_name}** — {t.provenance.confidence}, "
            f"{len(t.rows)} rows at {t.test_voltage_v:g} V."
            + (
                f" Not used: {', '.join(others)}. Change the choice on the Setups page."
                if others
                else ""
            )
        )
    if m.system.fit:
        f = m.system.fit
        st.markdown(
            f"Fitted jointly against the datasheet's electrical column: "
            f"`Cq={f.cq:.5f}`, `Rm={f.rm_ohm * 1000:.1f} mΩ`, `Io={f.io_a:.2f} A`. "
            f"Bus power reproduced to **{f.power_residual_pct:.2f}% RMS** "
            f"(worst row {f.max_power_residual_pct:.2f}%). "
            f"`Ct={f.ct_mean:.4f}` with {f.ct_scatter_pct:.1f}% scatter, implied figure of "
            f"merit {f.figure_of_merit:.2f}"
            + (
                f", and the fitted Cq sits {f.torque_column_delta_pct:+.1f}% from the "
                "datasheet's torque column."
                if f.torque_column_delta_pct is not None
                else "."
            )
        )
    for kind, obj in [
        ("Frame", resolved.frame), ("Motor", resolved.motor), ("Propeller", resolved.prop),
        ("ESC", resolved.esc), ("Battery", resolved.battery),
    ]:
        p = obj.provenance
        st.markdown(f"**{kind} — {obj.name}**", help=None)
        st.markdown(confidence_badge(p.confidence), unsafe_allow_html=True)
        if p.notes:
            st.caption(p.notes)
        if p.source_url:
            st.caption(p.source_url)
