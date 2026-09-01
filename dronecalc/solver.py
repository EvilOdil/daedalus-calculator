"""Hover solver.

The chain runs thrust -> rpm -> torque -> motor current/voltage -> ESC -> bus
-> battery. A useful property falls out of it: **propulsion bus power does not
depend on pack voltage**. The mechanical demand fixes the motor's torque, speed,
current and terminal voltage; the ESC just adjusts duty to deliver that from
whatever bus it has. Pack voltage therefore sets duty, pack current and sag - and
crucially the *ceiling*, since once duty reaches 1.0 the motor cannot spin faster
and available thrust starts falling as the pack drains.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq

from .models import Confidence, ResolvedSetup, weakest_confidence
from .physics import propeller as prop_phys
from .physics.atmosphere import G, air_density, grams_to_newtons, newtons_to_grams
from .physics.battery import BatteryModel, BatteryState, PackOverloadError
from .physics.esc import ESCModel
from .physics.fit import FitResult, fit_thrust_table
from .physics.motor import MotorModel


@dataclass(frozen=True)
class RotorResult:
    """Everything happening at one rotor."""

    thrust_g: float
    thrust_n: float
    rpm: float
    torque_nm: float
    shaft_power_w: float
    motor_current_a: float
    motor_voltage_v: float
    motor_power_w: float
    motor_efficiency: float
    copper_loss_w: float
    iron_loss_w: float
    esc_bus_power_w: float
    esc_loss_w: float
    bus_voltage_v: float
    duty: float
    extrapolated: bool

    @property
    def bus_current_a(self) -> float:
        """Battery-side current for this rotor - the quantity ESC and motor
        amp ratings actually refer to."""
        return self.esc_bus_power_w / self.bus_voltage_v if self.bus_voltage_v > 0 else 0.0

    @property
    def prop_efficiency_g_per_w(self) -> float:
        """Thrust per shaft watt - the propeller's own efficiency."""
        return self.thrust_g / self.shaft_power_w if self.shaft_power_w > 0 else 0.0

    @property
    def overall_efficiency_g_per_w(self) -> float:
        """Thrust per bus watt - what the battery actually pays for."""
        return self.thrust_g / self.esc_bus_power_w if self.esc_bus_power_w > 0 else 0.0


@dataclass(frozen=True)
class HoverPoint:
    """A converged hover solution at one all-up weight and state of charge."""

    auw_g: float
    n_rotors: int
    rho: float
    rotor: RotorResult
    propulsion_bus_power_w: float
    payload_bus_power_w: float
    total_bus_power_w: float
    battery: BatteryState
    #: Largest thrust one rotor can make at the present pack voltage, and why.
    max_thrust_per_rotor_g: float
    thrust_limited_by: str
    confidence: Confidence
    disc_loading_n_per_m2: float
    c_rate: float

    @property
    def thrust_per_rotor_g(self) -> float:
        return self.rotor.thrust_g

    @property
    def thrust_utilisation(self) -> float:
        """Hover thrust as a fraction of what is available.

        Tyto's "at least 2x hover thrust for control authority" rule is the same
        as keeping this at or below 0.5.
        """
        if self.max_thrust_per_rotor_g <= 0:
            return float("inf")
        return self.rotor.thrust_g / self.max_thrust_per_rotor_g

    @property
    def thrust_to_weight(self) -> float:
        return self.n_rotors * self.max_thrust_per_rotor_g / self.auw_g

    @property
    def hover_efficiency_g_per_w(self) -> float:
        """System power loading: grams lifted per bus watt, payload included.

        This is the number that most directly sets flight time.
        """
        if self.total_bus_power_w <= 0:
            return 0.0
        return self.auw_g / self.total_bus_power_w

    @property
    def propulsion_efficiency_g_per_w(self) -> float:
        if self.propulsion_bus_power_w <= 0:
            return 0.0
        return self.auw_g / self.propulsion_bus_power_w

    @property
    def pack_current_a(self) -> float:
        return self.battery.current_a


class SolverError(RuntimeError):
    """Raised when no physical hover solution exists for the configuration."""


class PropulsionSystem:
    """A resolved setup turned into evaluatable physics.

    Building one runs the Tier A fit if the motor carries a thrust table for the
    chosen propeller, and falls back to Tier B momentum theory otherwise.
    """

    def __init__(self, resolved: ResolvedSetup) -> None:
        self.resolved = resolved
        s = resolved.setup
        a = s.assumptions

        self.rho = air_density(a.altitude_m, a.temperature_c)
        self.n_rotors = s.n_rotors
        self.esc = ESCModel.from_profile(resolved.esc)
        self.battery = BatteryModel.from_profile(
            resolved.battery, extra_resistance_ohm=a.wiring_resistance_mohm / 1000.0
        )

        table = resolved.motor.table_for(resolved.prop.id, s.thrust_table)
        self.fit: FitResult | None = None
        if table is not None:
            prop_model, fit = fit_thrust_table(
                resolved.motor, resolved.prop, table, esc_efficiency=self.esc.efficiency
            )
            self.prop_model: object = prop_model
            self.fit = fit
            self.table_test_voltage_v: float | None = table.test_voltage_v
            self.table = table
            self.motor = MotorModel(
                kv_rpm_per_v=resolved.motor.kv_rpm_per_v,
                rm_ohm=fit.rm_ohm,
                io_a=fit.io_a,
                source="fitted from measured table",
            )
            self.tier = "A"
            self.tier_confidence: Confidence = table.provenance.confidence
        else:
            self.prop_model = prop_phys.ParametricProp.from_profile(
                resolved.prop, a.figure_of_merit
            )
            self.motor = MotorModel.from_profile(resolved.motor)
            self.tier = "B"
            self.tier_confidence = "estimated"
            self.table_test_voltage_v = None
            self.table = None

    # ------------------------------------------------------------------ #

    @property
    def confidence(self) -> Confidence:
        """Weakest link across everything feeding the answer."""
        r = self.resolved
        parts = [
            self.tier_confidence,
            r.motor.provenance.confidence,
            r.prop.provenance.confidence,
            r.esc.provenance.confidence,
            r.battery.provenance.confidence,
            r.frame.provenance.confidence,
        ]
        if r.payload is not None and r.payload.total_power_w > 0:
            parts.append(r.payload.power_confidence)
        return weakest_confidence(*parts)

    def rotor_at_thrust(self, thrust_n: float, bus_voltage_v: float) -> RotorResult:
        """Solve one rotor for a required thrust on a given bus."""
        point = self.prop_model.operating_point(thrust_n, self.rho)
        m = self.motor.solve(point.torque_nm, point.rpm)
        e = self.esc.solve(m.electrical_power_w, m.voltage_v, bus_voltage_v)
        return RotorResult(
            thrust_g=newtons_to_grams(thrust_n),
            thrust_n=thrust_n,
            rpm=point.rpm,
            torque_nm=point.torque_nm,
            shaft_power_w=point.shaft_power_w,
            motor_current_a=m.current_a,
            motor_voltage_v=m.voltage_v,
            motor_power_w=m.electrical_power_w,
            motor_efficiency=m.efficiency,
            copper_loss_w=m.copper_loss_w,
            iron_loss_w=m.iron_loss_w,
            esc_bus_power_w=e.bus_power_w,
            esc_loss_w=e.loss_w,
            bus_voltage_v=bus_voltage_v,
            duty=e.duty,
            extrapolated=point.extrapolated,
        )

    def max_thrust_per_rotor_n(self, bus_voltage_v: float) -> tuple[float, str]:
        """Greatest thrust one rotor can hold, and which limit binds first.

        For a Tier A setup the ceiling is anchored to the **measured** end of the
        thrust table rather than to the model's duty estimate. A motor at full
        duty is back-EMF limited, so its top speed tracks bus voltage and thrust
        goes as speed squared::

            T_max(V) = T_max(V_test) * (V / V_test)^2

        That is a measurement scaled by physics, which is worth more than a
        model extrapolated past the data. It is also why available thrust decays
        as the pack drains, and why a marginal design runs out of lift before it
        runs out of energy.

        Current ratings are compared against **battery-side** current per rotor.
        On the seed datasheet the 17 A "peak current" spec and the 16.37 A top
        row of the current column are plainly the same quantity, and multirotor
        ESC amp ratings are DC input ratings by convention.
        """
        r = self.resolved
        limits: list[tuple[float, str]] = []

        if self.tier == "A" and self.table_test_voltage_v:
            ceiling = self.prop_model.max_thrust_n_at(self.rho) * (
                bus_voltage_v / self.table_test_voltage_v
            ) ** 2
            limits.append((ceiling, "measured table ceiling (scaled to pack voltage)"))
        else:
            limits.append((self._thrust_at_full_duty(bus_voltage_v), "bus voltage (100% duty)"))

        if r.motor.max_current_a:
            limits.append(
                (self._thrust_at_bus_current(r.motor.max_current_a, bus_voltage_v),
                 "motor rated current")
            )
        if r.esc.cont_current_a:
            limits.append(
                (self._thrust_at_bus_current(r.esc.cont_current_a, bus_voltage_v),
                 "ESC continuous current")
            )
        if r.prop.thrust_limit_g:
            limits.append((grams_to_newtons(r.prop.thrust_limit_g), "propeller rated thrust"))

        limits = [(t, why) for t, why in limits if t > 0]
        if not limits:
            raise SolverError("no thrust limit could be established")
        return min(limits, key=lambda x: x[0])

    def _bracket(self, fn, lo: float, hi: float) -> float:
        """Expand `hi` until `fn` changes sign, then root-find. Returns thrust in N."""
        try:
            if fn(lo) > 0:
                return lo
            while fn(hi) < 0 and hi < grams_to_newtons(100_000.0):
                hi *= 1.6
            if fn(hi) < 0:
                return hi
            return brentq(fn, lo, hi, xtol=1e-4)
        except (ValueError, RuntimeError):
            return hi

    def _thrust_at_full_duty(self, bus_voltage_v: float) -> float:
        """Tier B ceiling: the thrust at which the ESC runs out of duty cycle."""
        return self._bracket(
            lambda t: self.rotor_at_thrust(t, bus_voltage_v).duty - 1.0,
            grams_to_newtons(1.0),
            grams_to_newtons(50.0),
        )

    def _thrust_at_bus_current(self, current_a: float, bus_voltage_v: float) -> float:
        """Thrust at which one rotor's battery-side current reaches `current_a`."""

        def excess(thrust_n: float) -> float:
            rotor = self.rotor_at_thrust(thrust_n, bus_voltage_v)
            return rotor.esc_bus_power_w / bus_voltage_v - current_a

        return self._bracket(excess, grams_to_newtons(1.0), grams_to_newtons(50.0))

    # ------------------------------------------------------------------ #

    def hover(self, auw_g: float | None = None, dod: float = 0.0) -> HoverPoint:
        """Solve steady level hover at `auw_g` with the pack at depth `dod`."""
        if auw_g is None:
            auw_g = self.resolved.auw_g
        if auw_g <= 0:
            raise SolverError("all-up weight must be positive")

        thrust_per_rotor_n = grams_to_newtons(auw_g) / self.n_rotors
        a = self.resolved.setup.assumptions

        # Propulsion power is independent of bus voltage, so it is solved first
        # with a nominal bus and only the duty/current figures are refreshed once
        # the true loaded pack voltage is known.
        nominal_v = self.battery.ocv(dod)
        rotor = self.rotor_at_thrust(thrust_per_rotor_n, nominal_v)

        propulsion_bus_w = self.n_rotors * rotor.esc_bus_power_w
        payload_bus_w = self.resolved.payload_power_w / a.bec_efficiency
        total_bus_w = propulsion_bus_w + payload_bus_w

        try:
            batt = self.battery.state(total_bus_w, dod)
        except PackOverloadError as exc:
            raise SolverError(str(exc)) from exc

        # Refresh duty against the sagged bus; power figures are unchanged.
        rotor = self.rotor_at_thrust(thrust_per_rotor_n, batt.voltage_v)

        max_thrust_n, limited_by = self.max_thrust_per_rotor_n(batt.voltage_v)

        disc_loading = thrust_per_rotor_n / self.resolved.prop.disc_area_m2
        c_rate = batt.current_a / self.resolved.battery.capacity_ah

        return HoverPoint(
            auw_g=auw_g,
            n_rotors=self.n_rotors,
            rho=self.rho,
            rotor=rotor,
            propulsion_bus_power_w=propulsion_bus_w,
            payload_bus_power_w=payload_bus_w,
            total_bus_power_w=total_bus_w,
            battery=batt,
            max_thrust_per_rotor_g=newtons_to_grams(max_thrust_n),
            thrust_limited_by=limited_by,
            confidence=self.confidence,
            disc_loading_n_per_m2=disc_loading,
            c_rate=c_rate,
        )

    def can_hover(self, auw_g: float, dod: float) -> bool:
        """Whether the aircraft can still hold this weight at this state of charge.

        Judged against the measured thrust ceiling, not the model's duty
        estimate - see `max_thrust_per_rotor_n` for why duty is the weaker signal.
        """
        try:
            hp = self.hover(auw_g, dod)
        except (SolverError, PackOverloadError):
            return False
        return hp.thrust_utilisation <= 1.0

    def thrust_at_throttle_g(self, throttle_pct: float) -> float | None:
        """Per-rotor thrust at a throttle *command*, read from the measured table.

        Manufacturers quote payload "at 70% throttle", meaning the stick position,
        not 70% of maximum thrust - the two differ because thrust is roughly
        quadratic in speed. Valid at the table's test voltage; returns None for a
        Tier B setup, which has no throttle column to read.
        """
        if self.table is None:
            return None
        pairs = sorted(
            (r.throttle_pct, r.thrust_g) for r in self.table.rows if r.throttle_pct is not None
        )
        if not pairs:
            return None
        xs = np.array([p[0] for p in pairs])
        ys = np.array([p[1] for p in pairs])
        return float(np.interp(throttle_pct, xs, ys))

    def max_payload_at_throttle_g(self, throttle_pct: float = 70.0) -> float | None:
        """Payload liftable while holding `throttle_pct` on the measured table.

        This is the convention behind Holybro's "1500 g maximum payload at 70%
        throttle" figure for the X500 V2.
        """
        per_rotor = self.thrust_at_throttle_g(throttle_pct)
        if per_rotor is None:
            return None
        return self.n_rotors * per_rotor - self.resolved.auw_g

    def max_payload_g(self, dod: float = 0.0, thrust_utilisation: float = 0.7) -> float:
        """Extra mass liftable while keeping thrust use at `thrust_utilisation`.

        Holybro quote the X500 V2's payload at 70% throttle, so 0.7 reproduces
        their published figure.
        """
        base = self.resolved.auw_g

        def excess(extra_g: float) -> float:
            hp = self.hover(base + extra_g, dod)
            return hp.thrust_utilisation - thrust_utilisation

        if excess(0.0) > 0:
            return 0.0
        hi = 100.0
        while excess(hi) < 0 and hi < 100_000.0:
            hi *= 1.6
        return float(brentq(excess, 0.0, hi, xtol=0.5))
