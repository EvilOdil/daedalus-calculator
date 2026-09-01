"""Readable spec sheets with per-field inline editing.

A component profile is a spec sheet, not a blob of JSON. Each field renders as a
labelled row with its unit and a pencil; the pencil turns that one row into an
input without disturbing anything else. Edits are validated through the pydantic
model before they touch disk, so a bad value is rejected rather than saved.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin

import streamlit as st
from pydantic import BaseModel


@dataclass(frozen=True)
class Spec:
    """One displayable/editable field."""

    field: str
    label: str
    unit: str = ""
    help: str | None = None
    #: Shown but never edited (ids are referenced by setups).
    readonly: bool = False


#: Ordered, grouped spec sheets. Anything not listed here still shows up under
#: "Other", so a newly added model field is never silently hidden.
FIELD_GROUPS: dict[str, list[tuple[str, list[Spec]]]] = {
    "motors": [
        ("Identity", [
            Spec("id", "ID", readonly=True),
            Spec("name", "Name"),
            Spec("manufacturer", "Manufacturer"),
        ]),
        ("Electrical", [
            Spec("kv_rpm_per_v", "KV", "rpm/V", "Unloaded speed per volt of back-EMF."),
            Spec("io_a", "No-load current (Io)", "A", "Current drawn spinning free."),
            Spec("io_test_v", "Io measured at", "V"),
            Spec("rm_ohm", "Winding resistance (Rm)", "Ω",
                 "Seeded from the datasheet, then re-fitted from the thrust table if one exists."),
            Spec("rm_convention", "Rm convention", "",
                 "Line-to-line is what a six-step drive sees. Per-phase values are doubled."),
            Spec("max_current_a", "Peak current", "A", "Battery-side, as datasheets quote it."),
            Spec("max_current_duration_s", "Peak current for", "s"),
            Spec("max_power_w", "Max power", "W"),
            Spec("max_cells_s", "Max cells", "S"),
        ]),
        ("Mechanical", [
            Spec("weight_g", "Weight", "g"),
            Spec("stator_diameter_mm", "Stator diameter", "mm"),
            Spec("stator_height_mm", "Stator height", "mm"),
            Spec("diameter_mm", "Body diameter", "mm"),
            Spec("length_mm", "Body length", "mm"),
            Spec("shaft_mm", "Shaft diameter", "mm"),
            Spec("mount_pattern", "Mounting pattern"),
            Spec("wire_length_mm", "Lead length", "mm"),
        ]),
    ],
    "props": [
        ("Identity", [
            Spec("id", "ID", readonly=True),
            Spec("name", "Name"),
            Spec("manufacturer", "Manufacturer"),
        ]),
        ("Geometry", [
            Spec("diameter_in", "Diameter", "in"),
            Spec("pitch_in", "Pitch", "in"),
            Spec("blades", "Blades"),
            Spec("weight_g", "Weight", "g", "Per propeller."),
            Spec("dimensions_mm", "Dimensions", "mm"),
        ]),
        ("Limits and materials", [
            Spec("thrust_limit_g", "Rated thrust limit", "g"),
            Spec("max_rpm", "Maximum speed", "rpm"),
            Spec("optimum_rpm_min", "Optimum speed from", "rpm"),
            Spec("optimum_rpm_max", "Optimum speed to", "rpm"),
            Spec("material", "Material"),
            Spec("propeller_type", "Type"),
            Spec("surface_treatment", "Surface treatment"),
            Spec("working_temp_c_min", "Working temperature from", "°C"),
            Spec("working_temp_c_max", "Working temperature to", "°C"),
        ]),
        ("Fitted coefficients", [
            Spec("ct", "Thrust coefficient Ct", "",
                 "Normally recovered from a thrust table; set here only to override."),
            Spec("cp", "Power coefficient Cp"),
        ]),
    ],
    "escs": [
        ("Identity", [
            Spec("id", "ID", readonly=True),
            Spec("name", "Name"),
            Spec("manufacturer", "Manufacturer"),
            Spec("firmware", "Firmware"),
        ]),
        ("Ratings", [
            Spec("cont_current_a", "Continuous current", "A", "Battery-side."),
            Spec("burst_current_a", "Burst current", "A"),
            Spec("burst_duration_s", "Burst for", "s"),
            Spec("max_cells_s", "Max cells", "S"),
            Spec("bec_current_a", "BEC current", "A", "Blank means no BEC."),
            Spec("efficiency", "Conversion efficiency", "", "0-1. Typically 0.95-0.97."),
            Spec("resistance_mohm", "On-resistance", "mΩ"),
        ]),
        ("Physical", [
            Spec("weight_g", "Weight", "g"),
            Spec("dimensions_mm", "Dimensions", "mm"),
            Spec("connector", "Motor connector"),
            Spec("signal_hz_min", "Signal frequency from", "Hz"),
            Spec("signal_hz_max", "Signal frequency to", "Hz"),
        ]),
    ],
    "batteries": [
        ("Identity", [
            Spec("id", "ID", readonly=True),
            Spec("name", "Name"),
            Spec("manufacturer", "Manufacturer"),
            Spec("cell_model", "Cell"),
            Spec("chemistry", "Chemistry", "",
                 "Selects the open-circuit voltage curve. Lithium-ion is flatter through the "
                 "middle and has a much longer tail than LiPo."),
        ]),
        ("Configuration", [
            Spec("cells_s", "Cells in series", "S"),
            Spec("cells_p", "Strings in parallel", "P",
                 "Affects pack resistance only — capacity below is already the pack figure."),
            Spec("capacity_mah", "Capacity", "mAh", "At the pack terminals, as quoted."),
            Spec("weight_g", "Weight", "g", "Weigh it. Easiest input to get exactly right."),
        ]),
        ("Voltage", [
            Spec("v_max_per_cell", "Maximum", "V/cell"),
            Spec("v_nominal_per_cell", "Nominal", "V/cell",
                 "Capacity times this times cells gives the pack's watt-hours."),
            Spec("v_cutoff_per_cell", "Cutoff", "V/cell", "Loaded voltage that ends the flight."),
        ]),
        ("Current", [
            Spec("c_rating_cont", "Continuous discharge", "C"),
            Spec("c_rating_burst", "Burst discharge", "C"),
            Spec("max_power_w", "Sustained power", "W"),
            Spec("charge_current_a", "Charge current", "A"),
            Spec("internal_resistance_mohm_per_cell", "Internal resistance", "mΩ/cell",
                 "Drives sag and how early the discharge ends. Measure it if you can."),
        ]),
        ("Physical", [
            Spec("dimensions_mm", "Dimensions", "mm"),
            Spec("discharge_connector", "Discharge connector"),
            Spec("balance_connector", "Balance connector"),
        ]),
    ],
    "frames": [
        ("Identity", [
            Spec("id", "ID", readonly=True),
            Spec("name", "Name"),
            Spec("manufacturer", "Manufacturer"),
        ]),
        ("Geometry", [
            Spec("wheelbase_mm", "Wheelbase", "mm"),
            Spec("weight_g", "Weight", "g", "Bare airframe, without motors, ESCs or avionics."),
            Spec("arms", "Arms"),
            Spec("max_prop_in", "Maximum propeller", "in",
                 "Enforced as a hard incompatibility — a propeller that fouls the frame is "
                 "rejected before its performance is reported."),
        ]),
    ],
    "payloads": [
        ("Identity", [
            Spec("id", "ID", readonly=True),
            Spec("name", "Name"),
            Spec("manufacturer", "Manufacturer"),
        ]),
    ],
}

#: Derived values worth showing but never editable.
DERIVED: dict[str, list[tuple[str, str]]] = {
    "batteries": [
        ("energy_wh", "Energy (Wh)"),
        ("specific_energy_wh_per_kg", "Specific energy (Wh/kg)"),
        ("v_nominal", "Nominal pack voltage (V)"),
        ("v_max", "Maximum pack voltage (V)"),
        ("max_cont_current_a", "Continuous current (A)"),
    ],
    "props": [
        ("pitch_ratio", "Pitch / diameter"),
        ("disc_area_m2", "Disc area (m²)"),
    ],
    "payloads": [
        ("total_mass_g", "Total mass (g)"),
        ("total_power_w", "Total power (W)"),
    ],
}


# --------------------------------------------------------------------------- #
# formatting and widgets
# --------------------------------------------------------------------------- #


def fmt(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e6:
            return f"{int(value):,}"
        return f"{value:,.6g}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _base_type(annotation: Any) -> Any:
    """Strip Optional/Union wrappers down to the underlying type."""
    origin = get_origin(annotation)
    if origin is typing.Union or str(origin) == "<class 'types.UnionType'>":
        args = [a for a in get_args(annotation) if a is not type(None)]
        return args[0] if args else str
    return annotation


def _widget(obj: BaseModel, spec: Spec, key: str) -> Any:
    """Render an input matching the field's declared type."""
    annotation = type(obj).model_fields[spec.field].annotation
    base = _base_type(annotation)
    current = getattr(obj, spec.field)

    if get_origin(base) is Literal:
        options = list(get_args(base))
        index = options.index(current) if current in options else 0
        return st.selectbox(spec.label, options, index=index, key=key, label_visibility="collapsed")
    if base is bool:
        return st.checkbox(spec.label, bool(current), key=key, label_visibility="collapsed")
    if base is int:
        return int(st.number_input(
            spec.label, value=int(current) if current is not None else 0, step=1,
            key=key, label_visibility="collapsed",
        ))
    if base is float:
        return st.number_input(
            spec.label, value=float(current) if current is not None else 0.0,
            step=None, format="%g", key=key, label_visibility="collapsed",
        )
    return st.text_input(
        spec.label, value="" if current is None else str(current), key=key,
        label_visibility="collapsed",
    )


def _commit(obj: BaseModel, field: str, value: Any) -> None:
    """Assign and validate. Empty text on an optional field means None."""
    annotation = type(obj).model_fields[field].annotation
    optional = type(None) in get_args(annotation)
    if isinstance(value, str) and value.strip() == "" and optional:
        value = None
    setattr(obj, field, value)
    # Round-trip through the model so a bad value raises here, not on load.
    type(obj).model_validate(obj.model_dump())


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def spec_row(obj: BaseModel, spec: Spec, scope: str, on_save) -> None:
    """One row: label on the left, value on the right beside its pencil.

    The value keeps the same position whether it is being displayed or edited -
    clicking the pencil turns that value into an input in place, rather than
    moving it somewhere else on the row.

    `scope` makes the widget keys unique; it cannot be derived from the object
    because nested blocks such as `Provenance` carry no id of their own.
    """
    state_key = f"editing::{scope}::{spec.field}"
    editing = st.session_state.get(state_key, False)
    value = getattr(obj, spec.field, None)

    # The action column widens while editing to fit save/cancel and the old value.
    widths = [5, 3.4, 2.4] if editing else [5, 3.4, 1.2]
    label_col, value_col, action_col = st.columns(widths, vertical_alignment="center")

    label_col.markdown(
        # No explicit colour anywhere in this module: text inherits the active
        # Streamlit theme and muted variants are done with opacity, so the sheet
        # stays legible in both light and dark.
        f"<span style='font-size:0.88rem;opacity:0.75'>{spec.label}"
        + (f" <span style='opacity:0.7'>({spec.unit})</span>" if spec.unit else "")
        + "</span>",
        unsafe_allow_html=True,
        help=spec.help,
    )

    if not editing:
        # Unset values are dimmed rather than recoloured.
        dim = "opacity:0.4;" if value in (None, "") else ""
        value_col.markdown(
            f"<div style='text-align:right;font-size:0.9rem;font-weight:600;{dim}"
            f"overflow:hidden;text-overflow:ellipsis'>{fmt(value)}</div>",
            unsafe_allow_html=True,
        )
        if spec.readonly:
            action_col.markdown(
                "<span style='opacity:0.45;font-size:0.8rem'>🔒</span>",
                unsafe_allow_html=True,
                help="Locked — setups reference components by id.",
            )
        elif action_col.button("✏️", key=f"pencil::{scope}::{spec.field}",
                               help=f"Edit {spec.label}"):
            st.session_state[state_key] = True
            st.rerun()
        return

    with value_col:
        new_value = _widget(obj, spec, f"input::{scope}::{spec.field}")

    with action_col:
        # Keep the value being replaced in view, so it is obvious what changed.
        st.markdown(
            f"<div style='font-size:0.72rem;opacity:0.65;line-height:1.1;margin-bottom:2px;"
            f"white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>was "
            f"<b>{fmt(value)}</b>"
            + (f" {spec.unit}" if spec.unit and value not in (None, "") else "")
            + "</div>",
            unsafe_allow_html=True,
        )
        save_col, cancel_col = st.columns(2)
    if save_col.button("✔", key=f"save::{scope}::{spec.field}", help="Save"):
        try:
            _commit(obj, spec.field, new_value)
            on_save(obj)
            st.session_state[state_key] = False
            st.rerun()
        except Exception as exc:  # noqa: BLE001 - user input
            st.error(f"{spec.label}: {exc}")
    if cancel_col.button("✕", key=f"cancel::{scope}::{spec.field}", help="Cancel"):
        st.session_state[state_key] = False
        st.rerun()


def spec_sheet(obj: BaseModel, kind: str, on_save) -> None:
    """Render the whole grouped spec sheet, plus anything not explicitly grouped."""
    groups = FIELD_GROUPS.get(kind, [])
    shown = {s.field for _, specs in groups for s in specs}

    for title, specs in groups:
        st.markdown(f"##### {title}")
        for spec in specs:
            if spec.field in type(obj).model_fields:
                spec_row(obj, spec, f"{kind}::{obj.id}", on_save)
        st.write("")

    skip = shown | {"provenance", "thrust_tables", "items", "ocv_curve"}
    leftover = [f for f in type(obj).model_fields if f not in skip]
    if leftover:
        st.markdown("##### Other")
        for field in leftover:
            spec_row(obj, Spec(field, field.replace("_", " ").capitalize()),
                     f"{kind}::{obj.id}", on_save)
        st.write("")

    derived = DERIVED.get(kind)
    if derived:
        st.markdown("##### Derived")
        st.caption("Computed from the fields above; not editable.")
        cols = st.columns(len(derived))
        for col, (attr, label) in zip(cols, derived):
            try:
                col.metric(label, fmt(getattr(obj, attr)))
            except Exception:  # noqa: BLE001
                col.metric(label, "—")


def provenance_sheet(obj: BaseModel, kind: str, on_save) -> None:
    """The provenance block, edited the same way as any other field."""
    prov = obj.provenance
    specs = [
        Spec("confidence", "Confidence", "",
             "measured = you tested this hardware; datasheet = manufacturer document; "
             "vendor = a listing; estimated = a guess. The weakest input sets the "
             "confidence of every result built on it."),
        Spec("source_url", "Source"),
        Spec("retrieved", "Retrieved"),
        Spec("notes", "Notes"),
    ]
    for spec in specs:
        spec_row(prov, spec, f"{kind}::{obj.id}::provenance", lambda _p, o=obj: on_save(o))
