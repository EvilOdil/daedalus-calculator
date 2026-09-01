"""The full metric set for a setup: power budget, margins and sensitivities.

Three things here are worth more than the headline flight time:

* the **power budget**, which says where the watts actually go, and therefore
  what is worth attacking;
* the **margins**, which say which component runs out first;
* **seconds per gram**, which is the number a payload designer can act on
  directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .endurance import EnduranceResult, integrate, payload_tax_minutes
from .models import Confidence
from .physics.atmosphere import grams_to_newtons
from .physics.propeller import ideal_hover_power_w, tip_mach
from .solver import HoverPoint, PropulsionSystem

#: Utilisation above this is a warning; above 1.0 is a failure.
WARN_UTILISATION = 0.80


@dataclass(frozen=True)
class Margin:
    """One rated limit and how close the setup runs to it."""

    name: str
    value: float
    limit: float
    unit: str
    note: str = ""

    @property
    def utilisation(self) -> float:
        return self.value / self.limit if self.limit else 0.0

    @property
    def status(self) -> str:
        u = self.utilisation
        if u > 1.0:
            return "fail"
        if u > WARN_UTILISATION:
            return "warn"
        return "ok"


@dataclass(frozen=True)
class PowerBudget:
    """Where every watt drawn from the cells ends up.

    Accounted from the chemistry outward, so it closes exactly: the pack's
    open-circuit power equals internal-resistance loss plus everything on the bus.
    """

    induced_w: float
    prop_profile_w: float
    motor_copper_w: float
    motor_iron_w: float
    esc_w: float
    payload_useful_w: float
    bec_w: float
    pack_ir_w: float
    total_w: float

    def rows(self) -> list[tuple[str, float, float]]:
        """(label, watts, fraction of total), largest first."""
        items = [
            ("Induced power (irreducible cost of hovering)", self.induced_w),
            ("Propeller profile loss", self.prop_profile_w),
            ("Motor copper loss", self.motor_copper_w),
            ("Motor no-load loss (iron, windage, unmodelled)", self.motor_iron_w),
            ("ESC loss", self.esc_w),
            ("Payload (useful)", self.payload_useful_w),
            ("Payload regulator loss", self.bec_w),
            ("Pack + harness IR loss", self.pack_ir_w),
        ]
        return sorted(
            [(n, w, w / self.total_w if self.total_w else 0.0) for n, w in items if w > 1e-9],
            key=lambda r: -r[1],
        )

    @property
    def closure_error_pct(self) -> float:
        """How far the itemised losses miss the total. Should be ~0."""
        s = (
            self.induced_w + self.prop_profile_w + self.motor_copper_w + self.motor_iron_w
            + self.esc_w + self.payload_useful_w + self.bec_w + self.pack_ir_w
        )
        return abs(s - self.total_w) / self.total_w * 100.0 if self.total_w else 0.0


@dataclass(frozen=True)
class Sensitivity:
    """Local gradients - the actionable part of a design review."""

    seconds_per_gram: float
    minutes_per_100g_battery: float
    payload_tax_minutes: float
    payload_mass_cost_minutes: float

    @property
    def payload_total_cost_minutes(self) -> float:
        return self.payload_tax_minutes + self.payload_mass_cost_minutes


@dataclass
class SetupMetrics:
    system: PropulsionSystem
    hover: HoverPoint
    endurance: EnduranceResult
    power: PowerBudget
    margins: list[Margin]
    sensitivity: Sensitivity
    warnings: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> Confidence:
        return self.hover.confidence

    @property
    def failing(self) -> list[Margin]:
        return [m for m in self.margins if m.status == "fail"]

    @property
    def flyable(self) -> bool:
        return not self.failing


def power_budget(system: PropulsionSystem, hp: HoverPoint) -> PowerBudget:
    n = hp.n_rotors
    prop = system.resolved.prop
    a = system.resolved.setup.assumptions

    induced = n * ideal_hover_power_w(hp.rotor.thrust_n, prop.disc_area_m2, hp.rho)
    profile = n * hp.rotor.shaft_power_w - induced
    payload_useful = system.resolved.payload_power_w
    bec = hp.payload_bus_power_w - payload_useful

    return PowerBudget(
        induced_w=induced,
        prop_profile_w=max(profile, 0.0),
        motor_copper_w=n * hp.rotor.copper_loss_w,
        motor_iron_w=n * hp.rotor.iron_loss_w,
        esc_w=n * hp.rotor.esc_loss_w,
        payload_useful_w=payload_useful,
        bec_w=bec,
        pack_ir_w=hp.battery.ir_loss_w,
        total_w=hp.total_bus_power_w + hp.battery.ir_loss_w,
    )


def margins(system: PropulsionSystem, hp: HoverPoint) -> list[Margin]:
    r = system.resolved
    a = r.setup.assumptions
    out: list[Margin] = []

    # Current ratings are battery-side; see PropulsionSystem.max_thrust_per_rotor_n.
    if r.esc.cont_current_a:
        out.append(Margin("ESC continuous current", hp.rotor.bus_current_a,
                          r.esc.cont_current_a, "A", "battery-side, per rotor"))
    if r.motor.max_current_a:
        out.append(Margin("Motor rated current", hp.rotor.bus_current_a,
                          r.motor.max_current_a, "A", "battery-side, per rotor"))
    if r.motor.max_power_w:
        out.append(Margin("Motor rated power", hp.rotor.esc_bus_power_w,
                          r.motor.max_power_w, "W", "per rotor"))
    if r.battery.c_rating_cont:
        out.append(Margin("Battery continuous discharge", hp.battery.current_a,
                          r.battery.max_cont_current_a, "A",
                          f"{hp.c_rate:.1f}C of a {r.battery.c_rating_cont:g}C pack"))
    if r.prop.thrust_limit_g:
        out.append(Margin("Propeller rated thrust", hp.rotor.thrust_g,
                          r.prop.thrust_limit_g, "g", "per rotor"))
    if r.prop.max_rpm:
        out.append(Margin("Propeller rated speed", hp.rotor.rpm, r.prop.max_rpm, "rpm"))

    out.append(Margin(
        "Thrust used in hover", hp.thrust_utilisation, 0.5, "fraction",
        "Tyto's rule: keep hover at or under half of available thrust (T/W >= 2) "
        "for control authority",
    ))
    out.append(Margin(
        "Propeller tip Mach", tip_mach(hp.rotor.rpm, r.prop.diameter_m, a.temperature_c),
        0.7, "Mach", "above ~0.7 noise and losses climb sharply",
    ))
    return out


def sensitivity(system: PropulsionSystem, baseline_min: float) -> Sensitivity:
    """Local gradients by finite difference around the current design."""
    r = system.resolved
    auw = r.auw_g

    # Seconds of endurance per gram of all-up weight, centred difference.
    d = 25.0
    up = integrate(system, auw + d).minutes
    down = integrate(system, auw - d).minutes
    secs_per_gram = (up - down) / (2 * d) * 60.0

    # What another 100 g of battery buys, at this pack's own specific energy.
    batt = r.battery
    wh_per_g = batt.energy_wh / batt.weight_g
    bigger = r.model_copy(deep=True)
    bigger.battery.weight_g += 100.0
    bigger.battery.capacity_mah *= (batt.weight_g + 100.0) / batt.weight_g
    try:
        gain = integrate(PropulsionSystem(bigger)).minutes - baseline_min
    except Exception:  # noqa: BLE001 - a bigger pack can exceed a rating
        gain = float("nan")

    tax = payload_tax_minutes(system)

    # Cost of *carrying* the payload, separate from powering it.
    mass_cost = 0.0
    if r.payload is not None and r.payload.total_mass_g > 0:
        no_payload_mass = auw - r.payload.total_mass_g
        stripped = r.model_copy(deep=True)
        for item in stripped.payload.items:
            item.power_w = 0.0
        without = integrate(PropulsionSystem(stripped), no_payload_mass).minutes
        with_mass_only = integrate(PropulsionSystem(stripped), auw).minutes
        mass_cost = without - with_mass_only

    return Sensitivity(
        seconds_per_gram=secs_per_gram,
        minutes_per_100g_battery=gain,
        payload_tax_minutes=tax,
        payload_mass_cost_minutes=mass_cost,
    )


def evaluate(system: PropulsionSystem, *, with_sensitivity: bool = True) -> SetupMetrics:
    """Full evaluation of a setup at its own all-up weight."""
    hp = system.hover()
    e = integrate(system)
    budget = power_budget(system, hp)
    marg = margins(system, hp)

    warnings: list[str] = []
    if system.tier == "B":
        warnings.append(
            "No measured thrust table for this motor+propeller pair: running momentum theory "
            f"at FM={system.resolved.setup.assumptions.figure_of_merit:.2f}. "
            "Every number below is ESTIMATED."
        )
    if system.fit is not None:
        warnings.extend(f"Thrust-table fit: {w}" for w in system.fit.warnings)
    if hp.rotor.extrapolated:
        warnings.append(
            "Hover thrust falls outside the measured table, so the operating point is "
            "extrapolated."
        )
    if budget.closure_error_pct > 0.1:
        warnings.append(f"Power budget does not close ({budget.closure_error_pct:.2f}% error).")
    for m in marg:
        if m.status == "fail":
            warnings.append(
                f"{m.name} exceeded: {m.value:.2f} {m.unit} against a limit of "
                f"{m.limit:.2f} {m.unit}."
            )

    sens = (
        sensitivity(system, e.minutes)
        if with_sensitivity
        else Sensitivity(float("nan"), float("nan"), 0.0, 0.0)
    )
    return SetupMetrics(system, hp, e, budget, marg, sens, warnings)
