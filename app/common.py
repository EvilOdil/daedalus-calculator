"""Shared helpers for the Streamlit pages: loading, overrides and formatting."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from dronecalc.metrics import SetupMetrics, evaluate
from dronecalc.models import PayloadItem, Payload, Provenance, ResolvedSetup, Setup
from dronecalc.solver import PropulsionSystem
from dronecalc.store import Library

CONFIDENCE_STYLE = {
    "measured": ("#1a7f37", "MEASURED", "from a thrust stand on this hardware"),
    "datasheet": ("#0969da", "DATASHEET", "from a manufacturer datasheet"),
    "vendor": ("#9a6700", "VENDOR", "from a vendor listing, not a full datasheet"),
    "estimated": ("#cf222e", "ESTIMATED", "guessed or derived from theory - verify before trusting"),
}

#: Mid-tone hues, chosen to hold contrast against both a white and a dark page.
#: The badges below are filled with white text, so they can be more saturated.
STATUS_COLOUR = {"ok": "#2ea043", "warn": "#d29922", "fail": "#e5484d"}


@st.cache_resource
def get_library() -> Library:
    return Library.load()


def reload_library() -> Library:
    get_library.clear()
    return get_library()


def confidence_badge(confidence: str, prefix: str = "") -> str:
    colour, label, meaning = CONFIDENCE_STYLE.get(confidence, CONFIDENCE_STYLE["estimated"])
    return (
        f"<span style='background:{colour};color:white;padding:2px 8px;border-radius:4px;"
        f"font-size:0.75rem;font-weight:600'>{prefix}{label}</span> "
        f"<span style='opacity:0.7;font-size:0.8rem'>{meaning}</span>"
    )


def apply_overrides(
    resolved: ResolvedSetup,
    *,
    altitude_m: float,
    temperature_c: float,
    dod_limit: float,
    reserve_fraction: float,
    extra_payload_g: float = 0.0,
    extra_payload_w: float = 0.0,
    figure_of_merit: float | None = None,
) -> ResolvedSetup:
    """Apply sidebar tweaks to a resolved setup.

    Extra payload is injected as one more payload item rather than by scaling the
    existing ones, so the sensors already profiled keep their own provenance.
    """
    r = resolved.model_copy(deep=True)
    a = r.setup.assumptions
    a.altitude_m = altitude_m
    a.temperature_c = temperature_c
    a.dod_limit = dod_limit
    a.reserve_fraction = reserve_fraction
    if figure_of_merit is not None:
        a.figure_of_merit = figure_of_merit

    if extra_payload_g or extra_payload_w:
        if r.payload is None:
            r.payload = Payload(id="_adhoc", name="Ad-hoc payload")
        r.payload.items.append(
            PayloadItem(
                name="Sidebar payload (what-if)",
                mass_g=extra_payload_g,
                power_w=extra_payload_w,
                provenance=Provenance(confidence="estimated", notes="Entered in the sidebar"),
            )
        )
    return r


def sidebar_controls(resolved: ResolvedSetup) -> ResolvedSetup:
    """Render the shared operating-condition controls and apply them."""
    a = resolved.setup.assumptions
    with st.sidebar:
        st.subheader("Operating conditions")
        altitude = st.number_input("Altitude (m)", 0.0, 6000.0, float(a.altitude_m), 100.0)
        temperature = st.number_input("Temperature (C)", -20.0, 50.0, float(a.temperature_c), 1.0)

        st.subheader("Battery use")
        # Cycle-life practice is chemistry-specific: the 80-85% convention is a LiPo
        # rule and quoting it at a Li-ion pack costs real flight time for no reason.
        if resolved.battery.chemistry in ("li-ion", "lifepo4"):
            dod_help = (
                f"How far you are willing to run the pack down. {resolved.battery.name} "
                "is Li-ion: these cells are specified to a low cutoff and tolerate deep "
                f"discharge, so 0.9-1.0 down to the profile's "
                f"{resolved.battery.v_cutoff_per_cell:g} V/cell is normal. The 0.8-0.85 "
                "convention is a LiPo cycle-life rule and does not apply here."
            )
        else:
            dod_help = (
                "How far you are willing to run the pack down. 0.8-0.85 is normal "
                "practice for LiPo cycle life."
            )
        dod = st.slider(
            "Usable depth of discharge", 0.5, 1.0, float(a.dod_limit), 0.01, help=dod_help
        )
        reserve = st.slider(
            "Landing reserve", 0.0, 0.4, float(a.reserve_fraction), 0.01,
            help="Held back on top of the depth-of-discharge limit.",
        )

        st.subheader("What-if payload")
        st.caption("Added on top of the setup's own payload profile.")
        extra_g = st.number_input("Extra payload mass (g)", 0.0, 5000.0, 0.0, 10.0)
        extra_w = st.number_input("Extra payload power (W)", 0.0, 500.0, 0.0, 0.5)

        fm = None
        if resolved.motor.table_for(resolved.prop.id) is None:
            st.subheader("Tier B assumption")
            st.caption("No measured table for this motor+prop pair.")
            fm = st.slider("Rotor figure of merit", 0.35, 0.85, float(a.figure_of_merit), 0.01)

    return apply_overrides(
        resolved,
        altitude_m=altitude,
        temperature_c=temperature,
        dod_limit=dod,
        reserve_fraction=reserve,
        extra_payload_g=extra_g,
        extra_payload_w=extra_w,
        figure_of_merit=fm,
    )


def analyse(resolved: ResolvedSetup, with_sensitivity: bool = True) -> SetupMetrics:
    return evaluate(PropulsionSystem(resolved), with_sensitivity=with_sensitivity)


def setup_picker(label: str = "Setup", key: str = "setup") -> Setup:
    lib = get_library()
    ids = lib.list_ids("setups")
    if not ids:
        st.error("No setups found. Run `python scripts/seed_library.py` to populate the library.")
        st.stop()
    chosen = st.sidebar.selectbox(
        label, ids, key=key, format_func=lambda i: lib.setups[i].name
    )
    return lib.setups[chosen]


def page_header(title: str, subtitle: str = "") -> None:
    # The gate lives here rather than in each page so a page added later cannot
    # forget it: nothing renders without calling page_header first.
    from auth import require_login

    require_login()
    st.title(title)
    if subtitle:
        st.caption(subtitle)
    # The library is cached for the life of the server process, so profiles edited
    # on disk from outside the app (a seed script, a hand-edited JSON) stay
    # invisible until the cache is dropped. This is the button that drops it.
    with st.sidebar:
        if st.button("Reload library from disk", width="stretch",
                     help="Re-read everything under data/. Use after editing "
                          "profiles outside the app."):
            reload_library()
            st.rerun()
    for err in get_library().load_errors:
        st.error(
            f"A saved profile could not be loaded and is being ignored — **{err}**  \n"
            "Fix the file under `data/` (or delete it) and reload."
        )
