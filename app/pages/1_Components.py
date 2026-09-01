"""Component library: readable spec sheets, editable field by field."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from common import confidence_badge, get_library, page_header, reload_library
from spec_ui import fmt, provenance_sheet, spec_sheet

st.set_page_config(page_title="Components", page_icon="🔧", layout="wide")
page_header(
    "Component library",
    "Every saved component, laid out as a spec sheet. Click the pencil beside any value to "
    "change it — edits are validated and written straight back to data/.",
)

lib = get_library()

KINDS = {
    "motors": "Motors", "props": "Propellers", "escs": "ESCs",
    "batteries": "Batteries", "frames": "Frames", "payloads": "Payloads",
}
kind = st.sidebar.radio("Component type", list(KINDS), format_func=KINDS.get)
ids = lib.list_ids(kind)

st.sidebar.caption(
    "  \n".join(f"**{KINDS[k]}** — {len(lib.list_ids(k))}" for k in KINDS)
)


def save(obj) -> None:
    lib.save(kind, obj)
    reload_library()


browse, add = st.tabs([f"Browse ({len(ids)})", "Add new"])

# --------------------------------------------------------------------- browse
with browse:
    if not ids:
        st.info(f"No {KINDS[kind].lower()} saved yet. Add one on the next tab.")
    else:
        chosen = st.selectbox(
            "Component", ids, format_func=lambda i: getattr(lib, kind)[i].name,
            label_visibility="collapsed",
        )
        obj = getattr(lib, kind)[chosen]

        head, badge = st.columns([3, 2], vertical_alignment="center")
        head.subheader(obj.name)
        if obj.manufacturer:
            head.caption(obj.manufacturer)
        with badge:
            st.markdown(confidence_badge(obj.provenance.confidence), unsafe_allow_html=True)

        if obj.provenance.notes:
            st.info(obj.provenance.notes)

        left, right = st.columns([3, 2])

        with left:
            spec_sheet(obj, kind, save)

        with right:
            st.markdown("##### Provenance")
            st.caption("Where these numbers came from. Editable like any other field.")
            provenance_sheet(obj, kind, save)

            st.divider()
            with st.expander("Delete this component"):
                setup_field = {
                    "motors": "motor_id", "props": "prop_id", "escs": "esc_id",
                    "batteries": "battery_id", "frames": "frame_id", "payloads": "payload_id",
                }[kind]
                used_by = [
                    s.name for s in lib.all_setups() if getattr(s, setup_field) == obj.id
                ]
                if used_by:
                    st.warning("In use by: " + ", ".join(sorted(set(used_by))))
                if st.button("Delete", type="secondary", key=f"del::{kind}::{obj.id}"):
                    lib.delete(kind, obj.id)
                    reload_library()
                    st.rerun()

        # -------------------------------------------------------- payload items
        if kind == "payloads":
            st.divider()
            st.markdown("##### Items")
            st.caption(
                "Sensors, compute and printed parts. Mass drives thrust; power comes off the "
                "same pack that flies the aircraft. Edit cells directly, add rows at the bottom."
            )
            edited = st.data_editor(
                pd.DataFrame([
                    {"name": i.name, "mass_g": i.mass_g, "power_w": i.power_w,
                     "confidence": i.provenance.confidence, "notes": i.provenance.notes or ""}
                    for i in obj.items
                ]) if obj.items else pd.DataFrame(
                    columns=["name", "mass_g", "power_w", "confidence", "notes"]
                ),
                num_rows="dynamic", width="stretch", key=f"items::{obj.id}",
                column_config={
                    "name": st.column_config.TextColumn("Item", required=True),
                    "mass_g": st.column_config.NumberColumn("Mass (g)", min_value=0.0),
                    "power_w": st.column_config.NumberColumn("Power (W)", min_value=0.0),
                    "confidence": st.column_config.SelectboxColumn(
                        "Confidence", options=["measured", "datasheet", "vendor", "estimated"]),
                    "notes": st.column_config.TextColumn("Notes"),
                },
            )
            c = st.columns([1, 3])
            if c[0].button("Save items", type="primary", key=f"saveitems::{obj.id}"):
                from dronecalc.models import PayloadItem, Provenance
                try:
                    obj.items = [
                        PayloadItem(
                            name=str(r["name"]), mass_g=float(r["mass_g"] or 0),
                            power_w=float(r["power_w"] or 0),
                            provenance=Provenance(
                                confidence=r.get("confidence") or "estimated",
                                notes=(r.get("notes") or None),
                            ),
                        )
                        for r in edited.to_dict("records") if str(r.get("name") or "").strip()
                    ]
                    save(obj)
                    st.success("Saved.")
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not save: {exc}")
            c[1].caption(
                f"Currently {obj.total_mass_g:.0f} g and {obj.total_power_w:.1f} W in total."
            )

        # -------------------------------------------------------- thrust tables
        if kind == "motors":
            st.divider()
            st.markdown("##### Measured thrust tables")
            if not obj.thrust_tables:
                st.info(
                    "No measured table. This motor runs on momentum theory, and everything "
                    "built on it is reported as ESTIMATED. Adding a table is the single "
                    "highest-value thing you can do here — try database.tytorobotics.com, a "
                    "manufacturer datasheet, or your own thrust stand."
                )
            # Which table a setup actually runs on depends on the setup, so say so
            # per setup rather than stamping one table as globally "the" table.
            users: dict[int, list[str]] = {}
            for setup in lib.all_setups():
                if setup.motor_id != obj.id:
                    continue
                chosen = obj.table_for(setup.prop_id, setup.thrust_table)
                if chosen is not None:
                    users.setdefault(id(chosen), []).append(setup.name)

            for idx, table in enumerate(obj.thrust_tables):
                prop = lib.props.get(table.prop_id)
                st.markdown(
                    f"**{table.display_name}** — {prop.name if prop else table.prop_id} "
                    f"at {fmt(table.test_voltage_v)} V"
                )
                used_by = users.get(id(table), [])
                badges = confidence_badge(table.provenance.confidence)
                if used_by:
                    badges += (
                        " <span style='background:#2ea043;color:white;padding:2px 8px;"
                        "border-radius:4px;font-size:0.75rem;font-weight:600;margin-left:6px'>"
                        "IN USE</span> <span style='opacity:0.7;font-size:0.8rem'>by "
                        + ", ".join(used_by) + "</span>"
                    )
                st.markdown(badges, unsafe_allow_html=True)
                if table.provenance.notes:
                    st.caption(table.provenance.notes)

                if prop is not None:
                    from dronecalc.physics.fit import fit_thrust_table
                    try:
                        _, fit = fit_thrust_table(obj, prop, table)
                        m = st.columns(6)
                        m[0].metric("Cq", f"{fit.cq:.5f}", help="Fitted torque coefficient")
                        m[1].metric("Rm", f"{fit.rm_ohm * 1000:.1f} mΩ",
                                    help="Recovered from this table, not the datasheet figure")
                        m[2].metric("Io", f"{fit.io_a:.2f} A")
                        m[3].metric("Ct", f"{fit.ct_mean:.4f}", f"{fit.ct_scatter_pct:.1f}% scatter")
                        m[4].metric("Figure of merit", f"{fit.figure_of_merit:.2f}")
                        m[5].metric("Fit residual", f"{fit.power_residual_pct:.2f}%",
                                    help="RMS error reproducing this table's own power column")
                        for w in fit.warnings:
                            st.warning(w)
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not fit this table: {exc}")

                edited = st.data_editor(
                    pd.DataFrame([r.model_dump(exclude_none=False) for r in table.rows]),
                    num_rows="dynamic", width="stretch", key=f"tbl::{obj.id}::{idx}",
                )
                tc = st.columns([1, 1, 3])
                if tc[0].button("Save table", key=f"savetbl::{obj.id}::{idx}", type="primary"):
                    from dronecalc.models import ThrustRow
                    try:
                        table.rows = [
                            ThrustRow(**{k: v for k, v in r.items() if pd.notna(v)})
                            for r in edited.to_dict("records") if pd.notna(r.get("thrust_g"))
                        ]
                        save(obj)
                        st.success("Saved.")
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not save: {exc}")
                if tc[1].button("Delete table", key=f"deltbl::{obj.id}::{idx}"):
                    obj.thrust_tables.pop(idx)
                    save(obj)
                    st.rerun()

            with st.expander("Add a thrust table"):
                st.caption(
                    "Paste rows as CSV. Columns: throttle_pct, thrust_g, torque_nm, current_a, "
                    "rpm, power_w. Current and power are battery-side. rpm and one of "
                    "current/power are required; torque is optional and used only as a "
                    "cross-check."
                )
                with st.form(f"addtbl::{obj.id}"):
                    tprop = st.selectbox("Propeller it was measured with", lib.list_ids("props"),
                                         format_func=lambda i: lib.props[i].name)
                    tvolt = st.number_input("Test bus voltage (V)", 1.0, 120.0, 16.0)
                    tconf = st.selectbox("Confidence",
                                         ["measured", "datasheet", "vendor", "estimated"])
                    tsrc = st.text_input("Source URL", "")
                    tnotes = st.text_area("Notes", "", height=68)
                    tcsv = st.text_area(
                        "CSV rows", "", height=150,
                        placeholder="throttle_pct,thrust_g,torque_nm,current_a,rpm,power_w\n"
                                    "30,210,0.03,1.44,4042,23",
                    )
                    if st.form_submit_button("Add table"):
                        import io
                        from dronecalc.models import Provenance, ThrustRow, ThrustTable
                        try:
                            df = pd.read_csv(io.StringIO(tcsv.strip()))
                            obj.thrust_tables.append(ThrustTable(
                                prop_id=tprop, test_voltage_v=tvolt,
                                rows=[ThrustRow(**{k: v for k, v in r.items() if pd.notna(v)})
                                      for r in df.to_dict("records")],
                                provenance=Provenance(
                                    confidence=tconf, source_url=tsrc or None,
                                    notes=tnotes or None,
                                    retrieved=str(pd.Timestamp.today().date()),
                                ),
                            ))
                            save(obj)
                            st.success("Added.")
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Could not add: {exc}")

# ------------------------------------------------------------------------ add
with add:
    st.caption(
        "Create the profile with the essentials, then fill in the rest field by field on the "
        "Browse tab. Anything left blank stays blank rather than being guessed."
    )
    from dronecalc.models import Battery, ESC, Frame, Motor, Payload, Propeller, Provenance

    with st.form("add_component"):
        c = st.columns(3)
        cid = c[0].text_input("ID (slug)", placeholder="tmotor-mn3110-780kv")
        name = c[1].text_input("Name")
        manufacturer = c[2].text_input("Manufacturer", "")
        c = st.columns([1, 2])
        confidence = c[0].selectbox(
            "Confidence", ["datasheet", "measured", "vendor", "estimated"],
            help="'measured' means you tested this exact hardware yourself.",
        )
        source_url = c[1].text_input("Source URL", "")
        notes = st.text_area("Notes", "", height=68)

        fields: dict = {}
        if kind == "motors":
            c = st.columns(4)
            fields["kv_rpm_per_v"] = c[0].number_input("KV (rpm/V)", 1.0, 20000.0, 920.0)
            fields["weight_g"] = c[1].number_input("Weight (g)", 0.1, 5000.0, 64.0)
            fields["max_cells_s"] = int(c[2].number_input("Max cells (S)", 1, 24, 4))
            fields["max_current_a"] = c[3].number_input(
                "Peak current (A, battery-side)", 0.0, 500.0, 17.0)
        elif kind == "props":
            c = st.columns(4)
            fields["diameter_in"] = c[0].number_input("Diameter (in)", 1.0, 60.0, 10.0)
            fields["pitch_in"] = c[1].number_input("Pitch (in)", 0.5, 40.0, 4.5)
            fields["blades"] = int(c[2].number_input("Blades", 2, 8, 2))
            fields["weight_g"] = c[3].number_input("Weight (g)", 0.1, 2000.0, 12.5)
        elif kind == "escs":
            c = st.columns(4)
            fields["cont_current_a"] = c[0].number_input("Continuous current (A)", 1.0, 500.0, 20.0)
            fields["burst_current_a"] = c[1].number_input("Burst current (A)", 0.0, 800.0, 30.0)
            fields["weight_g"] = c[2].number_input("Weight (g)", 0.1, 2000.0, 21.0)
            fields["max_cells_s"] = int(c[3].number_input("Max cells (S)", 1, 24, 4))
        elif kind == "batteries":
            c = st.columns(4)
            fields["chemistry"] = c[0].selectbox("Chemistry", ["lipo", "li-ion", "lihv", "lifepo4"])
            fields["cells_s"] = int(c[1].number_input("Cells in series (S)", 1, 24, 4))
            fields["cells_p"] = int(c[2].number_input("Strings in parallel (P)", 1, 10, 1))
            fields["capacity_mah"] = c[3].number_input(
                "Pack capacity (mAh)", 100.0, 200000.0, 10000.0,
                help="At the pack terminals — a 4S2P 10 Ah pack is 10000 here.")
            c = st.columns(3)
            fields["weight_g"] = c[0].number_input("Weight (g)", 1.0, 50000.0, 567.0)
            fields["c_rating_cont"] = c[1].number_input("Continuous C rating", 0.1, 200.0, 4.5)
            fields["v_nominal_per_cell"] = c[2].number_input(
                "Nominal V per cell", 2.0, 4.2, 3.6,
                help="3.7 for LiPo, about 3.6 for lithium-ion.")
        elif kind == "frames":
            c = st.columns(3)
            fields["wheelbase_mm"] = c[0].number_input("Wheelbase (mm)", 50.0, 5000.0, 500.0)
            fields["weight_g"] = c[1].number_input("Weight (g)", 1.0, 50000.0, 610.0)
            fields["max_prop_in"] = c[2].number_input("Max propeller (in)", 1.0, 60.0, 11.0)

        if st.form_submit_button("Create", type="primary"):
            try:
                if not cid or not name:
                    raise ValueError("ID and name are required")
                model = {"motors": Motor, "props": Propeller, "escs": ESC,
                         "batteries": Battery, "frames": Frame, "payloads": Payload}[kind]
                obj = model(
                    id=cid, name=name, manufacturer=manufacturer or None,
                    provenance=Provenance(
                        source_url=source_url or None, confidence=confidence,
                        notes=notes or None, retrieved=str(pd.Timestamp.today().date()),
                    ),
                    **fields,
                )
                path = lib.save(kind, obj)
                reload_library()
                st.success(f"Created {path}. Fill in the rest on the Browse tab.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not create: {exc}")
