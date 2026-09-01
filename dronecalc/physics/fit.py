"""Recover physical parameters from a measured thrust table.

A datasheet table is a set of operating points at one fixed voltage. To use it
at a different mass or a different pack voltage, it has to be turned back into
physics. The aerodynamic half (``thrust <-> rpm``) is directly measured and
voltage-independent. The electrical half is recovered here by a joint
least-squares fit of three parameters against the measured current column::

    Q          = cq * rho * n^2 * D^5
    I_motor    = Q / Kt + Io
    V_motor    = rpm / Kv + I_motor * Rm
    P_bus_pred = V_motor * I_motor / eta_esc

Fitting the electrical column rather than reading the torque column matters in
practice: manufacturer torque columns are often rounded to two decimals, which
on a 2216-class motor is a 15-30% quantisation at low throttle. The torque
column is kept as an independent cross-check instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

from ..models import Motor, Propeller, ThrustTable
from .atmosphere import RHO_SL, grams_to_newtons
from .propeller import MeasuredProp, cq_from, figure_of_merit


@dataclass
class FitResult:
    """Fitted parameters plus everything needed to judge whether to trust them."""

    cq: float
    rm_ohm: float
    io_a: float
    ct_mean: float
    ct_scatter_pct: float
    #: RMS error of predicted vs measured bus power, in percent.
    power_residual_pct: float
    max_power_residual_pct: float
    #: Fitted cq vs the datasheet torque column, in percent. None if no torque data.
    torque_column_delta_pct: float | None
    #: Figure of merit implied at the mid-table point; sanity range 0.4-0.85.
    figure_of_merit: float
    #: Duty cycle implied at the highest measured row. Bus power is pinned tightly
    #: by the data but the voltage/current *split* behind it is not - only their
    #: product is measured - so duty is indicative, not a hard number.
    max_duty: float
    warnings: list[str]

    @property
    def trustworthy(self) -> bool:
        return not self.warnings


def fit_thrust_table(
    motor: Motor,
    prop: Propeller,
    table: ThrustTable,
    esc_efficiency: float = 0.96,
    rho: float = RHO_SL,
) -> tuple[MeasuredProp, FitResult]:
    """Fit `(cq, Rm, Io)` to a measured table and build the Tier A rotor model."""
    rows = [r for r in table.rows if r.rpm and r.power_w and r.thrust_g > 0]
    if len(rows) < 3:
        raise ValueError(
            f"table for {motor.id}+{prop.id} needs at least 3 rows with rpm and power"
        )

    rpm = np.array([r.rpm for r in rows], float)
    thrust_g = np.array([r.thrust_g for r in rows], float)
    p_bus_meas = np.array([r.power_w for r in rows], float)
    thrust_n = np.array([grams_to_newtons(t) for t in thrust_g])
    n_rps = rpm / 60.0

    d = prop.diameter_m
    kv = motor.kv_rpm_per_v
    kt = 60.0 / (2.0 * np.pi * kv)

    def predict(params: np.ndarray) -> np.ndarray:
        cq, rm, io = params
        torque = cq * rho * n_rps**2 * d**5
        i_motor = torque / kt + io
        v_motor = rpm / kv + i_motor * rm
        return v_motor * i_motor / esc_efficiency

    def residual(params: np.ndarray) -> np.ndarray:
        # Relative residual so low-throttle rows are not drowned out by high ones.
        return (predict(params) - p_bus_meas) / p_bus_meas

    # Initial guesses: torque column if present, else a generic small-prop Cq.
    torque_rows = [(r.torque_nm, r.rpm) for r in rows if r.torque_nm]
    if torque_rows:
        cq0 = float(np.mean([cq_from(q, n, d, rho) for q, n in torque_rows]))
    else:
        cq0 = 0.010
    rm0 = motor.rm_ohm if motor.rm_ohm else 0.10
    if motor.rm_convention == "per_phase":
        rm0 *= 2.0
    io0 = motor.io_a if motor.io_a else 0.5

    fit = least_squares(
        residual,
        x0=[cq0, rm0, io0],
        bounds=([1e-4, 1e-4, 0.0], [1.0, 2.0, 10.0]),
        method="trf",
        xtol=1e-12,
        ftol=1e-12,
    )
    cq, rm, io = (float(v) for v in fit.x)

    resid = residual(fit.x)
    residual_pct = float(np.sqrt(np.mean(resid**2)) * 100.0)
    max_residual_pct = float(np.max(np.abs(resid)) * 100.0)

    torque_delta = None
    if torque_rows:
        torque_delta = float((cq / cq0 - 1.0) * 100.0)

    prop_model = MeasuredProp.from_columns(thrust_g, rpm, d, cq, rho_ref=rho)

    mid = len(rows) // 2
    q_mid = cq * rho * n_rps[mid] ** 2 * d**5
    p_shaft_mid = 2.0 * np.pi * n_rps[mid] * q_mid
    fm = figure_of_merit(thrust_n[mid], p_shaft_mid, prop.disc_area_m2, rho)

    top = int(np.argmax(rpm))
    q_top = cq * rho * n_rps[top] ** 2 * d**5
    i_top = q_top / kt + io
    v_top = rpm[top] / kv + i_top * rm
    max_duty = float(v_top / table.test_voltage_v)

    warnings: list[str] = []
    if residual_pct > 10.0:
        warnings.append(
            f"electrical fit residual {residual_pct:.1f}% (>10%): the table's columns "
            "may be mutually inconsistent"
        )
    if prop_model.ct_scatter_pct > 10.0:
        warnings.append(
            f"Ct scatter {prop_model.ct_scatter_pct:.1f}% across the table (>10%): "
            "thrust and rpm columns disagree with a constant-Ct law"
        )
    if not 0.35 <= fm <= 0.90:
        warnings.append(f"implied figure of merit {fm:.2f} is outside the plausible 0.35-0.90 range")
    if max_duty > 1.02:
        warnings.append(
            f"fit implies {max_duty * 100:.0f}% duty at the top row, which exceeds the "
            f"{table.test_voltage_v:.1f} V test bus - the electrical model cannot reproduce "
            "this table"
        )
    if torque_delta is not None and abs(torque_delta) > 25.0:
        warnings.append(
            f"fitted Cq differs from the datasheet torque column by {torque_delta:+.0f}%"
        )

    return prop_model, FitResult(
        cq=cq,
        rm_ohm=rm,
        io_a=io,
        ct_mean=prop_model.ct_mean,
        ct_scatter_pct=prop_model.ct_scatter_pct,
        power_residual_pct=residual_pct,
        max_power_residual_pct=max_residual_pct,
        torque_column_delta_pct=torque_delta,
        figure_of_merit=fm,
        max_duty=max_duty,
        warnings=warnings,
    )
