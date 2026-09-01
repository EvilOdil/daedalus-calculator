"""Sweep one variable and find where the gains stop."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from common import get_library, page_header, setup_picker
from dronecalc import optimize

st.set_page_config(page_title="Sweeps", page_icon="📈", layout="wide")
page_header(
    "Sweeps",
    "Hold everything else fixed and vary one thing. Battery size is the important one: a "
    "heavier pack carries more energy but also has to lift itself, so the curve bends over "
    "and eventually turns down.",
)

lib = get_library()
setup = setup_picker()

MODES = {
    "scale": "Battery size (scale this pack)",
    "batteries": "Battery (compare saved packs)",
    "props": "Propeller",
    "motors": "Motor",
    "escs": "ESC",
}
mode = st.sidebar.selectbox("Sweep", list(MODES), format_func=MODES.get)
threshold = st.sidebar.slider(
    "Knee threshold (min per 100 g)", 0.1, 8.0, 1.5, 0.1,
    help="Below this marginal return, extra battery has stopped paying for itself.",
)

if mode == "scale":
    st.caption(
        "Scales the setup's own pack up and down at constant specific energy, so watt-hours "
        "per kilogram, C-rating and cell count stay fixed and only the mass/energy trade "
        "moves. This answers what size of *this* chemistry to fly without needing a profile "
        "for every candidate pack."
    )
    df = optimize.battery_scaling_sweep(lib, setup)
    x_col, label = "battery_g", "battery mass (g)"
else:
    df = optimize.sweep(lib, setup, mode)
    x_col = "battery_g" if mode == "batteries" else "auw_g"
    label = "battery mass (g)" if mode == "batteries" else "all-up weight (g)"
    if len(df) < 2:
        st.info(
            f"Only one {MODES[mode].lower()} in the library, so there is nothing to compare. "
            + ("Use *Battery size* above to scale this pack instead, "
               if mode == "batteries" else "Add more on the Components page, ")
            + "or add more profiles."
        )

ok = df.dropna(subset=["endurance_min"])
if ok.empty:
    st.error("Nothing in this sweep produced a hover solution.")
    st.stop()

if "tier" in ok and ok["tier"].nunique() > 1:
    st.warning(
        "This sweep mixes measured (Tier A) and momentum-theory (Tier B) options. Tier B "
        "results depend on the assumed figure of merit, so treat cross-tier comparisons as "
        "indicative until you add a measured thrust table."
    )

ok = ok.sort_values(x_col)
flyable = ok[ok["flyable"].fillna(False).astype(bool)] if "flyable" in ok else ok

fig = go.Figure()
fig.add_trace(go.Scatter(
    x=ok[x_col], y=ok["endurance_min"], mode="lines+markers", name="Endurance (min)",
    text=ok["battery"] if "battery" in ok else ok["setup"],
    hovertemplate="%{text}<br>%{y:.1f} min<extra></extra>",
))
fig.add_trace(go.Scatter(
    x=ok[x_col], y=ok["thrust_to_weight"], mode="lines+markers",
    name="Thrust to weight", yaxis="y2",
))
fig.add_hline(y=2.0, line_dash="dot", yref="y2",
              annotation_text="T/W = 2.0 floor", annotation_position="bottom right")

knee = {}
if mode in ("scale", "batteries"):
    knee = optimize.find_knee(df, threshold_min_per_100g=threshold)
    if knee:
        fig.add_vline(x=knee["knee_row"][x_col], line_dash="dash",
                      annotation_text="diminishing returns")
if len(flyable) < len(ok):
    edge = flyable[x_col].max()
    fig.add_vrect(x0=edge, x1=ok[x_col].max(), fillcolor="#cf222e", opacity=0.07,
                  line_width=0, annotation_text="below T/W floor", annotation_position="top left")

fig.update_layout(
    height=440, xaxis_title=label, yaxis=dict(title="endurance (min)"),
    yaxis2=dict(title="thrust to weight", overlaying="y", side="right"),
    legend=dict(orientation="h", y=1.12), margin=dict(l=0, r=0, t=10, b=0),
)
st.plotly_chart(fig, width="stretch")

if knee:
    k, b = knee["knee_row"], knee["best_row"]
    c = st.columns(3)
    c[0].metric("Diminishing returns from", f"{k['battery_g']:.0f} g",
                f"{k['endurance_min']:.1f} min",
                help=f"Past here each extra 100 g buys under {threshold:.1f} min.")
    c[1].metric("Longest that still flies", f"{b['battery_g']:.0f} g",
                f"{b['endurance_min']:.1f} min",
                help="Best endurance among options meeting the thrust-to-weight floor.")
    # Row closest to the pack actually fitted, for orientation.
    own_mass = lib.resolve(setup).battery.weight_g
    current = ok.loc[(ok[x_col] - own_mass).abs().idxmin()]
    c[2].metric("Your pack", f"{current[x_col]:.0f} g", f"{current['endurance_min']:.1f} min",
                help="The closest point to the battery currently fitted to this setup.")
    st.caption(
        "Marginal return per extra 100 g of battery, lightest first: "
        + ", ".join(f"{v:.2f}" for v in knee["marginal_min_per_100g"]) + " min."
    )

cols = [c for c in ["battery", "scale", "battery_g", "capacity_mah", "energy_wh", "auw_g",
                    "endurance_min", "hover_eff_g_per_w", "thrust_to_weight",
                    "thrust_utilisation", "hover_power_w", "pack_c_rate", "tier",
                    "confidence", "limited_by", "flyable", "failing"] if c in ok]
st.dataframe(ok[cols], hide_index=True, width="stretch")

failed = df[df["endurance_min"].isna()]
if not failed.empty:
    st.subheader("No solution")
    st.dataframe(failed[[c for c in ["battery", "setup", "error"] if c in failed]],
                 hide_index=True, width="stretch")
