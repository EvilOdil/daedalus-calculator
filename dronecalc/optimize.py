"""Sweeps, search and the endurance/agility trade.

The optimizer never hands back a single "best" answer. Endurance and thrust-to-
weight genuinely trade against each other, so the useful output is the Pareto
front: what a minute of extra flight time costs you in agility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Iterable

import numpy as np
import pandas as pd

from .metrics import SetupMetrics, evaluate
from .models import Setup
from .solver import PropulsionSystem, SolverError
from .store import Library

#: Tyto's control-authority rule, as a hard filter.
DEFAULT_MIN_TWR = 2.0
#: Hover much above this and there is little left for wind or manoeuvre.
DEFAULT_MAX_UTILISATION = 0.6


@dataclass
class Candidate:
    """One evaluated configuration."""

    setup: Setup
    metrics: SetupMetrics | None
    error: str | None = None
    incompatible: list[str] = field(default_factory=list)
    unverifiable: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.metrics is not None

    def row(self) -> dict:
        if self.metrics is None:
            return {
                "setup": self.setup.id,
                "motor": self.setup.motor_id,
                "prop": self.setup.prop_id,
                "esc": self.setup.esc_id,
                "battery": self.setup.battery_id,
                "error": self.error,
                "flyable": False,
                "incompatible": "; ".join(self.incompatible),
            }
        m, hp, e = self.metrics, self.metrics.hover, self.metrics.endurance
        return {
            "setup": self.setup.id,
            "motor": self.setup.motor_id,
            "prop": self.setup.prop_id,
            "esc": self.setup.esc_id,
            "battery": self.setup.battery_id,
            "auw_g": hp.auw_g,
            "battery_g": m.system.resolved.battery.weight_g,
            "endurance_min": e.minutes,
            "hover_eff_g_per_w": hp.hover_efficiency_g_per_w,
            "thrust_to_weight": hp.thrust_to_weight,
            "thrust_utilisation": hp.thrust_utilisation,
            "hover_power_w": hp.total_bus_power_w,
            "pack_c_rate": hp.c_rate,
            "rpm": hp.rotor.rpm,
            "tier": m.system.tier,
            "confidence": m.confidence,
            "flyable": m.flyable and not self.incompatible,
            "incompatible": "; ".join(self.incompatible),
            "unverifiable": "; ".join(self.unverifiable),
            "limited_by": hp.thrust_limited_by,
            "failing": "; ".join(x.name for x in m.failing),
        }


def _variant(setup: Setup, **overrides) -> Setup:
    return setup.model_copy(update=overrides, deep=True)


def evaluate_setup(
    library: Library, setup: Setup, *, with_sensitivity: bool = False
) -> Candidate:
    """Evaluate one setup, capturing rather than raising configuration failures."""
    try:
        resolved = library.resolve(setup)
        incompatible = library.compatibility_errors(resolved)
        unverifiable = library.compatibility_warnings(resolved)
        if incompatible:
            # Do not report performance for something that cannot be built.
            return Candidate(setup, None, error="; ".join(incompatible),
                             incompatible=incompatible, unverifiable=unverifiable)
        system = PropulsionSystem(resolved)
        return Candidate(setup, evaluate(system, with_sensitivity=with_sensitivity),
                         unverifiable=unverifiable)
    except Exception as exc:  # noqa: BLE001 - infeasible combinations are data, not crashes
        return Candidate(setup, None, error=f"{type(exc).__name__}: {exc}")


def sweep(
    library: Library,
    base: Setup,
    kind: str,
    ids: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Swap one component across the library and evaluate each option.

    `kind` is a library directory name: "batteries", "props", "motors", "escs".
    """
    field_name = {
        "batteries": "battery_id",
        "props": "prop_id",
        "motors": "motor_id",
        "escs": "esc_id",
    }[kind]
    ids = list(ids) if ids is not None else library.list_ids(kind)
    rows = []
    for cid in ids:
        cand = evaluate_setup(library, _variant(base, **{field_name: cid, "id": f"{base.id}__{cid}"}))
        rows.append(cand.row())
    return pd.DataFrame(rows)


def find_knee(
    df: pd.DataFrame,
    mass_col: str = "battery_g",
    time_col: str = "endurance_min",
    threshold_min_per_100g: float = 1.0,
) -> dict:
    """Where extra battery stops paying for itself.

    Tyto make the point with a worked example - past roughly 100-125 Wh their
    marginal gains flatten. The same effect is structural: a heavier pack holds
    more energy but also has to lift itself, so the curve bends over and
    eventually turns down.

    Only configurations that actually fly are considered. A pack so heavy that
    thrust-to-weight collapses is not a longer-endurance option, it is a
    different aircraft that does not take off.
    """
    d = df.dropna(subset=[time_col])
    if "flyable" in d:
        flyable = d[d["flyable"].fillna(False).astype(bool)]
        if not flyable.empty:
            d = flyable
    d = d.sort_values(mass_col)
    if len(d) < 2:
        return {}
    mass = d[mass_col].to_numpy(float)
    time = d[time_col].to_numpy(float)
    marginal = np.gradient(time, mass) * 100.0  # minutes per 100 g

    knee_idx = int(np.argmax(time))
    for i, m in enumerate(marginal):
        if m < threshold_min_per_100g:
            knee_idx = max(i - 1, 0)
            break

    return {
        "knee_row": d.iloc[knee_idx].to_dict(),
        "best_row": d.iloc[int(np.argmax(time))].to_dict(),
        "marginal_min_per_100g": marginal,
        "mass_g": mass,
        "endurance_min": time,
        "threshold": threshold_min_per_100g,
    }


def search(
    library: Library,
    base: Setup,
    *,
    motors: Iterable[str] | None = None,
    props: Iterable[str] | None = None,
    escs: Iterable[str] | None = None,
    batteries: Iterable[str] | None = None,
    min_twr: float = DEFAULT_MIN_TWR,
    max_utilisation: float = DEFAULT_MAX_UTILISATION,
    require_measured: bool = False,
    allow_unverifiable: bool = False,
) -> pd.DataFrame:
    """Evaluate every combination and rank the survivors by endurance.

    Hard filters mirror the design rules rather than taste: `min_twr` is the
    control-authority requirement, `max_utilisation` keeps something in reserve
    for wind, and any exceeded component rating disqualifies outright.
    """
    motors = list(motors) if motors is not None else library.list_ids("motors")
    props = list(props) if props is not None else library.list_ids("props")
    escs = list(escs) if escs is not None else library.list_ids("escs")
    batteries = list(batteries) if batteries is not None else library.list_ids("batteries")

    rows = []
    for mo, pr, es, ba in product(motors, props, escs, batteries):
        setup = _variant(
            base, motor_id=mo, prop_id=pr, esc_id=es, battery_id=ba,
            id=f"{base.id}__{mo}__{pr}__{es}__{ba}",
        )
        rows.append(evaluate_setup(library, setup).row())

    df = pd.DataFrame(rows)
    if df.empty or "endurance_min" not in df:
        return df

    df["passes"] = (
        df["flyable"].fillna(False)
        & (df["thrust_to_weight"] >= min_twr)
        & (df["thrust_utilisation"] <= max_utilisation)
    )
    if require_measured:
        df["passes"] &= df["tier"] == "A"
    if not allow_unverifiable:
        df["passes"] &= df["unverifiable"].fillna("") == ""

    def why_rejected(r: pd.Series) -> str:
        # NaN is truthy, so every optional column has to be tested with notna().
        if pd.notna(r.get("incompatible")) and r.get("incompatible"):
            return str(r["incompatible"])
        if pd.notna(r.get("error")) and r.get("error"):
            return str(r["error"])
        if r.get("passes"):
            return ""
        if not r.get("flyable", False):
            return f"exceeds rating: {r.get('failing', '')}"
        if pd.notna(r.get("thrust_to_weight")) and r["thrust_to_weight"] < min_twr:
            return f"T/W {r['thrust_to_weight']:.2f} is below the {min_twr:g} floor"
        if pd.notna(r.get("thrust_utilisation")) and r["thrust_utilisation"] > max_utilisation:
            return f"hover uses {r['thrust_utilisation'] * 100:.0f}% of available thrust"
        if require_measured and r.get("tier") != "A":
            return "no measured thrust table"
        if not allow_unverifiable and pd.notna(r.get("unverifiable")) and r.get("unverifiable"):
            return str(r["unverifiable"])
        return "did not meet the constraints"

    df["rejected_because"] = df.apply(why_rejected, axis=1)

    return df.sort_values(["passes", "endurance_min"], ascending=[False, False]).reset_index(
        drop=True
    )


def pareto_front(
    df: pd.DataFrame, x: str = "thrust_to_weight", y: str = "endurance_min"
) -> pd.DataFrame:
    """Rows that nothing else beats on both axes at once."""
    d = df.dropna(subset=[x, y])
    if "passes" in d:
        d = d[d["passes"]]
    if d.empty:
        return d
    keep = []
    for i, row in d.iterrows():
        dominated = ((d[x] >= row[x]) & (d[y] >= row[y]) & ((d[x] > row[x]) | (d[y] > row[y]))).any()
        if not dominated:
            keep.append(i)
    return d.loc[keep].sort_values(y, ascending=False)


def battery_scaling_sweep(
    library: Library,
    base: Setup,
    fractions: Iterable[float] = (0.3, 0.5, 0.7, 0.85, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0),
) -> pd.DataFrame:
    """Scale the setup's own pack up and down at constant specific energy.

    More useful than sweeping a library of one: it answers "what size of *this*
    chemistry should I fly" without needing a profile for every candidate pack.
    Capacity and mass scale together, so watt-hours per kilogram, C-rating and
    cell count all stay put, and only the mass/energy trade moves.
    """
    resolved = library.resolve(base)
    pack = resolved.battery
    rows = []
    for f in fractions:
        scaled = pack.model_copy(deep=True)
        scaled.id = f"{pack.id}__x{f:g}"
        scaled.name = f"{pack.name} x{f:g}"
        scaled.capacity_mah = pack.capacity_mah * f
        scaled.weight_g = pack.weight_g * f
        # Parallel-string resistance scales with the number of strings, so a pack
        # of the same chemistry twice the size has roughly half the resistance.
        if pack.internal_resistance_mohm_per_cell:
            scaled.internal_resistance_mohm_per_cell = (
                pack.internal_resistance_mohm_per_cell / f
            )
        variant = resolved.model_copy(deep=True)
        variant.battery = scaled
        try:
            system = PropulsionSystem(variant)
            m = evaluate(system, with_sensitivity=False)
            rows.append({
                "scale": f,
                "battery": scaled.name,
                "battery_g": scaled.weight_g,
                "capacity_mah": scaled.capacity_mah,
                "energy_wh": scaled.energy_wh,
                "auw_g": m.hover.auw_g,
                "endurance_min": m.endurance.minutes,
                "hover_eff_g_per_w": m.hover.hover_efficiency_g_per_w,
                "thrust_to_weight": m.hover.thrust_to_weight,
                "thrust_utilisation": m.hover.thrust_utilisation,
                "hover_power_w": m.hover.total_bus_power_w,
                "pack_c_rate": m.hover.c_rate,
                "tier": system.tier,
                "confidence": m.confidence,
                "flyable": m.flyable,
                "limited_by": m.hover.thrust_limited_by,
                "failing": "; ".join(x.name for x in m.failing),
            })
        except Exception as exc:  # noqa: BLE001 - an oversized pack may simply not fly
            rows.append({"scale": f, "battery": scaled.name, "battery_g": scaled.weight_g,
                         "endurance_min": None, "error": f"{type(exc).__name__}: {exc}"})
    return pd.DataFrame(rows)
