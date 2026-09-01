"""Brushless motor electrical model.

The classic three-parameter DC-equivalent model, with `Kv` in RPM/V::

    K_t     = 60 / (2 pi Kv)              [N m / A]
    I_motor = Q / K_t + I_0
    V_motor = rpm / Kv + I_motor * R_m
    eta     = P_shaft / (V_motor * I_motor)

`R_m` here is the **line-to-line** resistance, which is what a six-step drive
sees. Vendors publish both conventions and rarely say which, so `Motor` carries
`rm_convention` explicitly and `physics.fit` prefers to recover Rm and Io from a
measured table over trusting either.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..models import Motor


@dataclass(frozen=True)
class MotorState:
    torque_nm: float
    rpm: float
    current_a: float
    voltage_v: float
    shaft_power_w: float
    electrical_power_w: float
    copper_loss_w: float
    iron_loss_w: float

    @property
    def efficiency(self) -> float:
        if self.electrical_power_w <= 0:
            return 0.0
        return self.shaft_power_w / self.electrical_power_w


@dataclass(frozen=True)
class MotorModel:
    """Evaluatable motor: `Kv`, line-to-line `Rm`, no-load current `Io`."""

    kv_rpm_per_v: float
    rm_ohm: float
    io_a: float
    #: How Rm/Io were obtained, for provenance reporting.
    source: str = "datasheet"

    @property
    def kt_nm_per_a(self) -> float:
        """Torque constant. Kt [N m/A] = 60 / (2 pi Kv) for Kv in RPM/V."""
        return 60.0 / (2.0 * np.pi * self.kv_rpm_per_v)

    @classmethod
    def from_profile(cls, motor: Motor, *, rm_default: float = 0.10, io_default: float = 0.5) -> "MotorModel":
        """Build from a saved profile, normalising the resistance convention."""
        rm = motor.rm_ohm if motor.rm_ohm is not None else rm_default
        if motor.rm_convention == "per_phase":
            # Line-to-line is two phases in series for a wye winding.
            rm *= 2.0
        io = motor.io_a if motor.io_a is not None else io_default
        source = "datasheet" if motor.rm_ohm is not None else "estimated"
        return cls(kv_rpm_per_v=motor.kv_rpm_per_v, rm_ohm=rm, io_a=io, source=source)

    def solve(self, torque_nm: float, rpm: float) -> MotorState:
        """Electrical state required to hold `torque_nm` at `rpm`."""
        current = torque_nm / self.kt_nm_per_a + self.io_a
        back_emf = rpm / self.kv_rpm_per_v
        voltage = back_emf + current * self.rm_ohm
        shaft = 2.0 * np.pi * (rpm / 60.0) * torque_nm
        electrical = voltage * current
        copper = current**2 * self.rm_ohm
        # Everything not shaft work or copper loss: iron, windage, bearing drag.
        iron = max(electrical - shaft - copper, 0.0)
        return MotorState(
            torque_nm=torque_nm,
            rpm=rpm,
            current_a=current,
            voltage_v=voltage,
            shaft_power_w=shaft,
            electrical_power_w=electrical,
            copper_loss_w=copper,
            iron_loss_w=iron,
        )

    def max_rpm_at(self, voltage_v: float) -> float:
        """Unloaded top speed at `voltage_v`, allowing for the no-load current."""
        return max((voltage_v - self.io_a * self.rm_ohm) * self.kv_rpm_per_v, 0.0)
