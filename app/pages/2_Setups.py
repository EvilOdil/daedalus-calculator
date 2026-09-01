"""Mix and match components into a saved setup."""

from __future__ import annotations

import streamlit as st

from common import analyse, get_library, page_header, reload_library
from dronecalc.models import Setup

st.set_page_config(page_title="Setups", page_icon="🧩", layout="wide")
page_header(
    "Build a setup",
    "Pick components, see what the combination does, save it under a name. "
    "Setups reference components by id, so correcting a datasheet fixes every setup at once.",
)

lib = get_library()
base_id = st.sidebar.selectbox(
    "Start from", lib.list_ids("setups"), format_func=lambda i: lib.setups[i].name
)
base = lib.setups[base_id]


def pick(label: str, kind: str, current: str | None, allow_none: bool = False):
    ids = lib.list_ids(kind)
    if allow_none:
        ids = ["(none)"] + ids
        current = current or "(none)"
    index = ids.index(current) if current in ids else 0
    choice = st.selectbox(
        label, ids, index=index,
        format_func=lambda i: i if i == "(none)" else getattr(lib, kind)[i].name,
    )
    return None if choice == "(none)" else choice


c1, c2 = st.columns(2)
with c1:
    frame_id = pick("Frame", "frames", base.frame_id)
    motor_id = pick("Motor", "motors", base.motor_id)
    prop_id = pick("Propeller", "props", base.prop_id)
    n_rotors = int(st.number_input("Rotors", 3, 12, base.n_rotors))
with c2:
    esc_id = pick("ESC", "escs", base.esc_id)
    battery_id = pick("Battery", "batteries", base.battery_id)
    payload_id = pick("Payload", "payloads", base.payload_id, allow_none=True)
    misc = st.number_input(
        "Misc mass (g)", 0.0, 5000.0, float(base.misc_mass_g), 5.0,
        help="PDB, wiring, connectors, fasteners — anything not covered by a profile.",
    )

# A motor can hold more than one table for the same prop. When it does, which one
# drives the numbers is a real choice, so it is made here rather than inferred.
tables = lib.motors[motor_id].tables_for(prop_id) if motor_id and prop_id else []
thrust_table = base.thrust_table
if len(tables) > 1:
    AUTO = f"Automatic — highest confidence ({tables[0].display_name})"
    options = [AUTO] + [t.display_name for t in tables]
    current = base.thrust_table if base.thrust_table in options else AUTO
    picked = st.selectbox(
        "Thrust table", options, index=options.index(current),
        help="This motor has several measured tables for this propeller. They can "
             "disagree; pick the one you trust for this build.",
    )
    thrust_table = None if picked == AUTO else picked
    active = next((t for t in tables if t.display_name == picked), tables[0])
    st.caption(
        f"Driving every number below: **{active.display_name}** — "
        f"{active.provenance.confidence}, {len(active.rows)} rows at "
        f"{active.test_voltage_v:g} V."
    )
elif len(tables) == 1:
    st.caption(f"Thrust table: **{tables[0].display_name}** ({tables[0].provenance.confidence}).")

candidate = base.model_copy(
    update=dict(
        id="preview", frame_id=frame_id, motor_id=motor_id, prop_id=prop_id, esc_id=esc_id,
        battery_id=battery_id, payload_id=payload_id, n_rotors=n_rotors, misc_mass_g=misc,
        thrust_table=thrust_table,
    ),
    deep=True,
)

st.divider()
resolved = lib.resolve(candidate)
for e_ in lib.compatibility_errors(resolved):
    st.error(e_)
for w in lib.compatibility_warnings(resolved):
    st.warning(w)

try:
    m = analyse(resolved, with_sensitivity=False)
    hp, e = m.hover, m.endurance
    cols = st.columns(6)
    cols[0].metric("Endurance", f"{e.minutes:.1f} min")
    cols[1].metric("Efficiency", f"{hp.hover_efficiency_g_per_w:.2f} g/W")
    cols[2].metric("T/W", f"{hp.thrust_to_weight:.2f}")
    cols[3].metric("Thrust used", f"{hp.thrust_utilisation * 100:.0f} %")
    cols[4].metric("AUW", f"{hp.auw_g:.0f} g")
    cols[5].metric("Tier", m.system.tier, help="A = measured table, B = momentum theory")
    if m.failing:
        st.error("Exceeds ratings: " + ", ".join(x.name for x in m.failing))
    for w in m.warnings:
        st.warning(w)
except Exception as exc:  # noqa: BLE001
    st.error(f"No hover solution: {exc}")

st.divider()
with st.form("save_setup"):
    st.subheader("Save this combination")
    new_id = st.text_input("id (slug)", f"{base.id}-variant")
    new_name = st.text_input("Name", f"{base.name} (variant)")
    new_desc = st.text_area("Description", "", height=70)
    if st.form_submit_button("Save setup"):
        try:
            saved = candidate.model_copy(
                update=dict(id=new_id, name=new_name, description=new_desc or None), deep=True
            )
            Setup.model_validate(saved.model_dump())
            path = lib.save("setups", saved)
            reload_library()
            st.success(f"Saved {path}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not save: {exc}")
