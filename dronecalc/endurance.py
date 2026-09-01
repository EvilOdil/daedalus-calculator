"""Endurance: how long the pack holds the aircraft up.

Two numbers are reported side by side.

* **Integrated** (default) - march the pack down in depth-of-discharge steps.
  Bus power is roughly constant, so as open-circuit voltage falls the current
  rises, which deepens the IR sag, which brings the loaded cutoff forward. The
  run ends at the first of: loaded cell cutoff, the depth-of-discharge limit, or
  the point where full duty can no longer hold hover.

* **Naive** - ``usable Wh / bus W``, the formula Tyto Robotics and eCalc quote.
  Kept so results stay comparable to the tools people already trust.

MISSION HOOK
------------
`FlightSegment` is the extension point for the deferred forward-cruise model. A
segment only has to answer "what bus power does this phase need at this weight
and state of charge"; `integrate` handles the discharge march. Adding cruise
means adding a `CruiseSegment` here - the solver does not change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from .physics.battery import PackOverloadError
from .solver import HoverPoint, PropulsionSystem, SolverError


@runtime_checkable
class FlightSegment(Protocol):
    """A phase of flight that draws power from the pack."""

    name: str

    def bus_power_w(self, system: PropulsionSystem, auw_g: float, dod: float) -> float:
        """Total bus power this phase needs, payload included."""
        ...


@dataclass(frozen=True)
class HoverSegment:
    """Steady level hover - the only segment implemented today."""

    name: str = "hover"

    def bus_power_w(self, system: PropulsionSystem, auw_g: float, dod: float) -> float:
        return system.hover(auw_g, dod).total_bus_power_w


@dataclass
class EnduranceResult:
    minutes: float
    minutes_naive: float
    terminated_by: str
    usable_wh: float
    delivered_wh: float
    dod_reached: float
    mean_bus_power_w: float
    #: Per-step trace for plotting: minutes, dod, pack V, cell V, current, power.
    trace: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def seconds(self) -> float:
        return self.minutes * 60.0


def integrate(
    system: PropulsionSystem,
    auw_g: float | None = None,
    segment: FlightSegment | None = None,
    *,
    dod_step: float = 0.002,
) -> EnduranceResult:
    """March the pack down and return the resulting endurance."""
    resolved = system.resolved
    a = resolved.setup.assumptions
    if auw_g is None:
        auw_g = resolved.auw_g
    segment = segment or HoverSegment()

    dod_limit = a.dod_limit * (1.0 - a.reserve_fraction)
    capacity_ah = resolved.battery.capacity_ah
    cutoff_v = system.battery.v_cutoff

    times: list[float] = [0.0]
    dods: list[float] = [0.0]
    volts: list[float] = []
    amps: list[float] = []
    powers: list[float] = []

    t_h = 0.0
    dod = 0.0
    energy_wh = 0.0
    terminated = f"depth-of-discharge limit ({dod_limit * 100:.0f}%)"

    while dod < dod_limit:
        try:
            point: HoverPoint = system.hover(auw_g, dod)
        except (SolverError, PackOverloadError) as exc:
            terminated = f"pack could no longer sustain the load ({exc})"
            break
        if point.thrust_utilisation > 1.0:
            terminated = "ran out of thrust - pack voltage too low to hold hover"
            break
        if point.battery.cell_voltage_v <= cutoff_v / resolved.battery.cells_s:
            terminated = f"loaded cell cutoff ({resolved.battery.v_cutoff_per_cell:.2f} V/cell)"
            break

        power_w = point.total_bus_power_w
        current_a = point.battery.current_a
        step = min(dod_step, dod_limit - dod)
        # Charge drawn over this step sets how long it lasts.
        dt_h = step * capacity_ah / current_a
        t_h += dt_h
        energy_wh += power_w * dt_h
        dod += step

        times.append(t_h * 60.0)
        dods.append(dod)
        volts.append(point.battery.voltage_v)
        amps.append(current_a)
        powers.append(power_w)

    if not powers:
        raise SolverError("configuration cannot hover even on a full pack")

    # Naive comparison figure, on the same usable-energy basis.
    usable_wh = capacity_ah * resolved.battery.v_nominal * dod_limit
    naive_min = usable_wh / float(np.mean(powers)) * 60.0

    return EnduranceResult(
        minutes=t_h * 60.0,
        minutes_naive=naive_min,
        terminated_by=terminated,
        usable_wh=usable_wh,
        delivered_wh=energy_wh,
        dod_reached=dod,
        mean_bus_power_w=float(np.mean(powers)),
        trace={
            "minutes": np.array(times[1:]),
            "dod": np.array(dods[1:]),
            "pack_v": np.array(volts),
            "cell_v": np.array(volts) / resolved.battery.cells_s,
            "current_a": np.array(amps),
            "power_w": np.array(powers),
        },
    )


def payload_tax_minutes(system: PropulsionSystem) -> float:
    """Minutes of endurance lost to the payload's *electrical* draw alone.

    Isolates the cost of powering the payload from the cost of carrying it, by
    re-running at the same weight with the payload's power set to zero. You
    asked for the battery to feed the payload; this is the price of that.
    """
    resolved = system.resolved
    if resolved.payload is None or resolved.payload.total_power_w <= 0:
        return 0.0

    with_payload = integrate(system).minutes

    # Same aircraft, same mass, payload drawing nothing.
    stripped = resolved.model_copy(deep=True)
    for item in stripped.payload.items:
        item.power_w = 0.0
    without = integrate(PropulsionSystem(stripped)).minutes
    return without - with_payload
