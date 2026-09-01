"""Compare saved setups side by side against a reference."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from common import analyse, get_library, page_header

st.set_page_config(page_title="Compare", page_icon="⚖️", layout="wide")
page_header("Compare setups", "Side by side, with deltas against whichever one you make the reference.")

lib = get_library()
ids = lib.list_ids("setups")
chosen = st.multiselect("Setups", ids, ids[:2], format_func=lambda i: lib.setups[i].name)
if len(chosen) < 1:
    st.info("Pick at least one setup.")
    st.stop()
reference = st.selectbox("Reference", chosen, format_func=lambda i: lib.setups[i].name)

ROWS = [
    ("All-up weight", "g", lambda m: m.hover.auw_g, 0),
    ("Battery mass", "g", lambda m: m.system.resolved.battery.weight_g, 0),
    ("Payload mass", "g",
     lambda m: m.system.resolved.payload.total_mass_g if m.system.resolved.payload else 0.0, 0),
    ("Payload power", "W", lambda m: m.system.resolved.payload_power_w, 1),
    ("Hover endurance", "min", lambda m: m.endurance.minutes, 1),
    ("Hover efficiency", "g/W", lambda m: m.hover.hover_efficiency_g_per_w, 2),
    ("Hover power", "W", lambda m: m.hover.total_bus_power_w, 0),
    ("Thrust to weight", "", lambda m: m.hover.thrust_to_weight, 2),
    ("Thrust used in hover", "%", lambda m: m.hover.thrust_utilisation * 100, 0),
    ("Rotor speed", "rpm", lambda m: m.hover.rotor.rpm, 0),
    ("Disc loading", "N/m²", lambda m: m.hover.disc_loading_n_per_m2, 1),
    ("Pack C-rate", "C", lambda m: m.hover.c_rate, 2),
    ("Cost of mass", "s/g", lambda m: abs(m.sensitivity.seconds_per_gram), 2),
    ("Payload total cost", "min", lambda m: m.sensitivity.payload_total_cost_minutes, 2),
]

results: dict[str, object] = {}
for sid in chosen:
    try:
        results[sid] = analyse(lib.resolve(sid))
    except Exception as exc:  # noqa: BLE001
        st.error(f"{lib.setups[sid].name}: {exc}")

if not results:
    st.stop()

table = {}
for sid, m in results.items():
    col = {}
    for label, unit, fn, dp in ROWS:
        value = fn(m)
        cell = f"{value:,.{dp}f}{' ' + unit if unit else ''}"
        if sid != reference and reference in results:
            delta = value - fn(results[reference])
            if abs(delta) > 10 ** -(dp + 2):
                cell += f"  ({delta:+,.{dp}f})"
        col[label] = cell
    col["Model tier"] = m.system.tier
    col["Confidence"] = m.confidence
    col["Ratings exceeded"] = ", ".join(x.name for x in m.failing) or "none"
    table[lib.setups[sid].name + (" (ref)" if sid == reference else "")] = col

st.dataframe(pd.DataFrame(table), width="stretch")

for sid, m in results.items():
    if m.warnings:
        with st.expander(f"Warnings — {lib.setups[sid].name}"):
            for w in m.warnings:
                st.warning(w)
