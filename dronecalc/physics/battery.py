"""Battery model: open-circuit voltage, sag, and depth-of-discharge marching.

Endurance is integrated rather than divided. Under a roughly constant power
draw, falling OCV forces rising current, which deepens the IR sag, which brings
the loaded cutoff forward. That feedback is the dominant real-world mechanism
behind the Peukert-style capacity penalty, and it falls straight out of stepping
the discharge rather than dividing energy by power.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..models import Battery

#: Typical LiPo open-circuit voltage per cell against depth of discharge.
#: Used when a profile does not carry a measured curve.
DEFAULT_LIPO_OCV: list[tuple[float, float]] = [
    (0.00, 4.20),
    (0.05, 4.10),
    (0.10, 4.03),
    (0.20, 3.92),
    (0.30, 3.85),
    (0.40, 3.79),
    (0.50, 3.74),
    (0.60, 3.70),
    (0.70, 3.66),
    (0.80, 3.60),
    (0.90, 3.50),
    (0.95, 3.40),
    (1.00, 3.20),
]

#: Typical high-energy lithium-ion (silicon or graphite anode) open-circuit
#: voltage per cell against depth of discharge. Flatter through the middle than
#: LiPo and with a much longer tail, which is why running one on the LiPo curve
#: badly misjudges the end of the discharge.
DEFAULT_LIION_OCV: list[tuple[float, float]] = [
    (0.00, 4.20),
    (0.05, 4.05),
    (0.10, 3.96),
    (0.20, 3.84),
    (0.30, 3.75),
    (0.40, 3.68),
    (0.50, 3.61),
    (0.60, 3.55),
    (0.70, 3.48),
    (0.80, 3.39),
    (0.90, 3.24),
    (0.95, 3.05),
    (1.00, 2.55),
]

#: Fallback pack resistance when a profile does not publish one, in milliohms
#: per cell. Sane for a healthy 20C-class LiPo of a few thousand mAh.
DEFAULT_IR_MOHM_PER_CELL = 4.0


def default_ocv_curve(chemistry: str) -> list[tuple[float, float]]:
    """Open-circuit curve to use when a profile carries no measured one."""
    if chemistry in ("li-ion", "lifepo4"):
        return DEFAULT_LIION_OCV
    return DEFAULT_LIPO_OCV


@dataclass(frozen=True)
class BatteryState:
    voltage_v: float
    current_a: float
    dod: float
    ocv_v: float
    sag_v: float
    ir_loss_w: float
    cells_s: int

    @property
    def cell_voltage_v(self) -> float:
        """Loaded voltage per cell - the number a low-voltage alarm watches."""
        return self.voltage_v / self.cells_s


@dataclass
class BatteryModel:
    cells_s: int
    capacity_ah: float
    resistance_ohm: float
    v_cutoff_per_cell: float
    _dod: np.ndarray = field(repr=False, default=None)
    _ocv_cell: np.ndarray = field(repr=False, default=None)

    @classmethod
    def from_profile(cls, battery: Battery, extra_resistance_ohm: float = 0.0) -> "BatteryModel":
        ir_cell = battery.internal_resistance_mohm_per_cell
        if ir_cell is None:
            ir_cell = DEFAULT_IR_MOHM_PER_CELL
        # Series cells add resistance; parallel strings divide it.
        pack_r = battery.cells_s * (ir_cell / 1000.0) / max(battery.cells_p, 1)
        curve = battery.ocv_curve or default_ocv_curve(battery.chemistry)
        arr = np.array(curve, float)
        model = cls(
            cells_s=battery.cells_s,
            capacity_ah=battery.capacity_ah,
            resistance_ohm=pack_r + extra_resistance_ohm,
            v_cutoff_per_cell=battery.v_cutoff_per_cell,
        )
        # Rescale a default curve so its endpoints honour the profile's limits.
        cell_v = arr[:, 1]
        if battery.ocv_curve is None:
            cell_v = cell_v * (battery.v_max_per_cell / cell_v[0])
        model._dod = arr[:, 0]
        model._ocv_cell = cell_v
        return model

    def ocv(self, dod: float) -> float:
        """Pack open-circuit voltage at depth of discharge `dod` (0..1)."""
        return float(np.interp(np.clip(dod, 0.0, 1.0), self._dod, self._ocv_cell)) * self.cells_s

    @property
    def v_cutoff(self) -> float:
        return self.cells_s * self.v_cutoff_per_cell

    def solve_constant_power(self, power_w: float, dod: float) -> tuple[float, float]:
        """Loaded pack voltage and current for a constant-power draw.

        Solves ``V = OCV - (P/V) R`` exactly: ``V^2 - OCV*V + P*R = 0``, taking
        the high-voltage (stable) root. Returns ``(voltage, current)``.
        """
        ocv = self.ocv(dod)
        disc = ocv**2 - 4.0 * power_w * self.resistance_ohm
        if disc <= 0:
            # Demanded power exceeds what the pack can deliver at any voltage.
            raise PackOverloadError(
                f"pack cannot supply {power_w:.0f} W at {ocv:.2f} V OCV "
                f"with {self.resistance_ohm * 1000:.1f} mOhm internal resistance"
            )
        voltage = 0.5 * (ocv + np.sqrt(disc))
        return float(voltage), float(power_w / voltage)

    def state(self, power_w: float, dod: float) -> BatteryState:
        voltage, current = self.solve_constant_power(power_w, dod)
        ocv = self.ocv(dod)
        return BatteryState(
            voltage_v=voltage,
            current_a=current,
            dod=dod,
            ocv_v=ocv,
            sag_v=ocv - voltage,
            ir_loss_w=current**2 * self.resistance_ohm,
            cells_s=self.cells_s,
        )


class PackOverloadError(RuntimeError):
    """Raised when the demanded bus power exceeds what the pack can deliver."""
