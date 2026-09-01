"""Propeller aerodynamics.

Two tiers, per the design:

* **Tier A (measured)** - a datasheet or thrust-stand table is reduced to
  non-dimensional coefficients so it answers questions at any mass and any bus
  voltage, which a raw throttle lookup cannot::

      C_T = T / (rho n^2 D^4)        C_P = P_shaft / (rho n^3 D^5)

  with `n` in rev/s. Inside the measured range a monotone PCHIP interpolation on
  ``T <-> n`` is used because it reproduces the data exactly; outside it, the
  fitted constant `C_T` extrapolates.

* **Tier B (parametric)** - momentum theory with a figure of merit, for
  components that have no measured data. Always badged ESTIMATED.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.interpolate import PchipInterpolator

from ..models import Propeller
from .atmosphere import RHO_SL, speed_of_sound


@dataclass(frozen=True)
class RotorPoint:
    """A propeller operating point."""

    thrust_n: float
    rpm: float
    torque_nm: float
    shaft_power_w: float
    #: True when `thrust_n` sat outside the measured table and was extrapolated.
    extrapolated: bool = False

    @property
    def rev_per_s(self) -> float:
        return self.rpm / 60.0


def ct_from(thrust_n: float, rpm: float, diameter_m: float, rho: float) -> float:
    """Non-dimensional thrust coefficient (per-rev convention)."""
    n = rpm / 60.0
    if n <= 0:
        return 0.0
    return thrust_n / (rho * n**2 * diameter_m**4)


def cq_from(torque_nm: float, rpm: float, diameter_m: float, rho: float) -> float:
    """Non-dimensional torque coefficient (per-rev convention)."""
    n = rpm / 60.0
    if n <= 0:
        return 0.0
    return torque_nm / (rho * n**2 * diameter_m**5)


def cp_from_cq(cq: float) -> float:
    """Power coefficient from torque coefficient: C_P = 2 pi C_Q."""
    return 2.0 * np.pi * cq


def ideal_hover_power_w(thrust_n: float, disc_area_m2: float, rho: float) -> float:
    """Momentum-theory induced power for a rotor in hover.

    ``P_ideal = T^1.5 / sqrt(2 rho A)``
    """
    if thrust_n <= 0:
        return 0.0
    return thrust_n**1.5 / np.sqrt(2.0 * rho * disc_area_m2)


def figure_of_merit(thrust_n: float, shaft_power_w: float, disc_area_m2: float, rho: float) -> float:
    """FM = ideal power / actual shaft power. Sanity range for small props: 0.4-0.85."""
    if shaft_power_w <= 0:
        return 0.0
    return ideal_hover_power_w(thrust_n, disc_area_m2, rho) / shaft_power_w


def estimate_ct(prop: Propeller) -> float:
    """Empirical static C_T from pitch ratio, for props with no measured data.

    Static thrust coefficient for small multirotor props sits around 0.10-0.14
    and climbs mildly with pitch ratio. Deliberately crude: any result built on
    this is reported as ESTIMATED.
    """
    return float(np.clip(0.09 + 0.10 * prop.pitch_ratio, 0.08, 0.16))


# --------------------------------------------------------------------------- #
# Tier A
# --------------------------------------------------------------------------- #


@dataclass
class MeasuredProp:
    """Tier A rotor model built from a measured thrust table.

    Only the *aerodynamic* relationship ``thrust <-> rpm`` is stored here; it is
    the part of a thrust table that is genuinely voltage-independent and so the
    part that ports to other operating conditions. Torque comes from the fitted
    `cq`, which `physics.fit` recovers jointly with the motor's Rm and Io from
    the electrical column.
    """

    diameter_m: float
    rho_ref: float
    thrust_n: np.ndarray
    rpm: np.ndarray
    cq: float
    ct_mean: float
    ct_scatter_pct: float
    #: Highest thrust present in the table, at `rho_ref`.
    max_thrust_n: float = field(init=False)
    _t_of_n: PchipInterpolator = field(init=False, repr=False)
    _n_of_t: PchipInterpolator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        order = np.argsort(self.rpm)
        self.rpm = np.asarray(self.rpm, float)[order]
        self.thrust_n = np.asarray(self.thrust_n, float)[order]
        if not np.all(np.diff(self.thrust_n) > 0):
            raise ValueError("thrust must increase monotonically with rpm")
        self._t_of_n = PchipInterpolator(self.rpm, self.thrust_n, extrapolate=True)
        self._n_of_t = PchipInterpolator(self.thrust_n, self.rpm, extrapolate=True)
        self.max_thrust_n = float(self.thrust_n[-1])

    @classmethod
    def from_columns(
        cls,
        thrust_g: np.ndarray,
        rpm: np.ndarray,
        diameter_m: float,
        cq: float,
        rho_ref: float = RHO_SL,
    ) -> "MeasuredProp":
        from .atmosphere import grams_to_newtons

        thrust_n = np.array([grams_to_newtons(t) for t in thrust_g], float)
        rpm = np.asarray(rpm, float)
        cts = np.array(
            [ct_from(t, r, diameter_m, rho_ref) for t, r in zip(thrust_n, rpm) if r > 0]
        )
        ct_mean = float(cts.mean())
        scatter = float(cts.std() / ct_mean * 100.0) if ct_mean else 0.0
        return cls(
            diameter_m=diameter_m,
            rho_ref=rho_ref,
            thrust_n=thrust_n,
            rpm=rpm,
            cq=cq,
            ct_mean=ct_mean,
            ct_scatter_pct=scatter,
        )

    def rpm_for_thrust(self, thrust_n: float, rho: float) -> tuple[float, bool]:
        """RPM needed for `thrust_n` at density `rho`.

        Density is handled by scaling into the reference-density frame: at fixed
        RPM thrust is proportional to rho, so a query at low density is the same
        as asking the reference curve for a proportionally larger thrust.
        """
        equivalent = thrust_n * (self.rho_ref / rho)
        extrapolated = equivalent > self.max_thrust_n or equivalent < self.thrust_n[0]
        if equivalent > self.max_thrust_n:
            # Beyond the table, fall back on the fitted constant-Ct law.
            n = np.sqrt(equivalent / (self.rho_ref * self.ct_mean * self.diameter_m**4))
            return float(n * 60.0), True
        rpm = float(self._n_of_t(equivalent))
        return max(rpm, 0.0), extrapolated

    def torque_for_rpm(self, rpm: float, rho: float) -> float:
        n = rpm / 60.0
        return self.cq * rho * n**2 * self.diameter_m**5

    def operating_point(self, thrust_n: float, rho: float) -> RotorPoint:
        rpm, extrapolated = self.rpm_for_thrust(thrust_n, rho)
        torque = self.torque_for_rpm(rpm, rho)
        power = 2.0 * np.pi * (rpm / 60.0) * torque
        return RotorPoint(thrust_n, rpm, torque, power, extrapolated)

    def max_thrust_n_at(self, rho: float) -> float:
        """Highest thrust the table covers, corrected to density `rho`."""
        return self.max_thrust_n * (rho / self.rho_ref)


# --------------------------------------------------------------------------- #
# Tier B
# --------------------------------------------------------------------------- #


@dataclass
class ParametricProp:
    """Tier B rotor model: momentum theory plus a figure of merit."""

    diameter_m: float
    disc_area_m2: float
    ct: float
    fm: float

    @classmethod
    def from_profile(cls, prop: Propeller, fm: float) -> "ParametricProp":
        return cls(
            diameter_m=prop.diameter_m,
            disc_area_m2=prop.disc_area_m2,
            ct=prop.ct if prop.ct else estimate_ct(prop),
            fm=fm,
        )

    def operating_point(self, thrust_n: float, rho: float) -> RotorPoint:
        power = ideal_hover_power_w(thrust_n, self.disc_area_m2, rho) / self.fm
        n = np.sqrt(thrust_n / (rho * self.ct * self.diameter_m**4)) if thrust_n > 0 else 0.0
        rpm = float(n * 60.0)
        torque = power / (2.0 * np.pi * n) if n > 0 else 0.0
        return RotorPoint(thrust_n, rpm, torque, power, extrapolated=True)


def tip_mach(rpm: float, diameter_m: float, temperature_c: float) -> float:
    """Propeller tip Mach number. Above ~0.7 noise and losses rise sharply."""
    tip_speed = np.pi * diameter_m * rpm / 60.0
    return tip_speed / speed_of_sound(temperature_c)
