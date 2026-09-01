"""Search every combination, then show the endurance/agility trade honestly."""

from __future__ import annotations

import plotly.express as px
import streamlit as st

from common import get_library, page_header, setup_picker
from dronecalc import optimize

st.set_page_config(page_title="Optimizer", page_icon="🎯", layout="wide")
page_header(
    "Optimizer",
    "Every motor x propeller x ESC x battery in the library, filtered against the design "
    "rules and ranked by endurance. There is no single best answer — endurance and thrust-to-"
    "weight genuinely trade — so the Pareto front matters more than the top row.",
)

lib = get_library()
base = setup_picker()

with st.sidebar:
    st.subheader("Constraints")
    min_twr = st.slider(
        "Minimum thrust to weight", 1.2, 4.0, 2.0, 0.1,
        help="Tyto's rule of thumb: at least double hover thrust for control authority.",
    )
    max_util = st.slider(
        "Maximum thrust used in hover", 0.3, 0.9, 0.6, 0.05,
        help="Leaves headroom for wind and manoeuvre.",
    )
    measured_only = st.checkbox(
        "Measured data only (Tier A)", False,
        help="Excludes anything relying on momentum theory.",
    )
    allow_unverifiable = st.checkbox(
        "Include options the library cannot check", False,
        help="Combinations with no measured thrust table, or a motor that declares no "
             "current or power rating. These tend to look artificially good — nothing in "
             "the model stops an undersized motor being over-worked — so they are excluded "
             "by default.",
    )
    st.subheader("Search space")
    motors = st.multiselect("Motors", lib.list_ids("motors"), lib.list_ids("motors"))
    props = st.multiselect("Propellers", lib.list_ids("props"), lib.list_ids("props"))
    escs = st.multiselect("ESCs", lib.list_ids("escs"), lib.list_ids("escs"))
    batteries = st.multiselect("Batteries", lib.list_ids("batteries"), lib.list_ids("batteries"))

total = len(motors) * len(props) * len(escs) * len(batteries)
st.caption(f"{total} combinations to evaluate.")
if total == 0:
    st.stop()
if not st.button(f"Run search ({total} combinations)", type="primary"):
    st.stop()

with st.spinner("Evaluating..."):
    df = optimize.search(
        lib, base, motors=motors, props=props, escs=escs, batteries=batteries,
        min_twr=min_twr, max_utilisation=max_util, require_measured=measured_only,
        allow_unverifiable=allow_unverifiable,
    )

passing = df[df["passes"]] if "passes" in df else df
st.success(f"{len(passing)} of {len(df)} combinations meet the constraints.")
if allow_unverifiable and (passing.get("unverifiable", "").astype(bool).any() if "unverifiable" in passing else False):
    st.warning(
        "Unverifiable options are included. Results built on momentum theory or on a motor "
        "with no declared ratings are not comparable with measured ones — an undersized motor "
        "on an oversized propeller will top this ranking and overheat in reality."
    )

if passing.empty:
    st.warning("Nothing passed. The rejection reasons below say what to relax.")
else:
    front = optimize.pareto_front(passing)
    fig = px.scatter(
        passing, x="thrust_to_weight", y="endurance_min", color="hover_eff_g_per_w",
        size="auw_g", hover_data=["motor", "prop", "battery", "tier", "confidence"],
        labels={
            "thrust_to_weight": "thrust to weight (agility)",
            "endurance_min": "endurance (min)",
            "hover_eff_g_per_w": "hover efficiency (g/W)",
        },
        color_continuous_scale="Viridis",
    )
    if not front.empty:
        fig.add_scatter(
            x=front["thrust_to_weight"], y=front["endurance_min"], mode="lines+markers",
            name="Pareto front", line=dict(dash="dash"),
        )
    fig.update_layout(height=480, margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, width="stretch")

    st.subheader("Pareto front")
    st.caption("Nothing in the search beats these on both endurance and thrust-to-weight.")
    st.dataframe(
        front[["motor", "prop", "esc", "battery", "endurance_min", "thrust_to_weight",
               "hover_eff_g_per_w", "auw_g", "tier", "confidence"]],
        hide_index=True, width="stretch",
    )

    st.subheader("All passing combinations")
    st.dataframe(
        passing[["motor", "prop", "esc", "battery", "endurance_min", "hover_eff_g_per_w",
                 "thrust_to_weight", "thrust_utilisation", "auw_g", "pack_c_rate",
                 "tier", "confidence", "limited_by"]],
        hide_index=True, width="stretch",
    )

rejected = df[~df["passes"]] if "passes" in df else df.iloc[0:0]
if not rejected.empty:
    with st.expander(f"Rejected ({len(rejected)}) and why"):
        st.dataframe(
            rejected[["motor", "prop", "esc", "battery", "endurance_min",
                      "thrust_to_weight", "rejected_because"]],
            hide_index=True, width="stretch",
        )
