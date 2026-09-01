"""Component profiles, setups and assumptions.

Every profile carries a `Provenance` block so a number lifted from a datasheet is
never confused with one that was guessed. `confidence` propagates through the
solver and is surfaced in the UI.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Confidence = Literal["measured", "datasheet", "vendor", "estimated"]

#: Ranking used to pick the weakest link when combining several inputs.
CONFIDENCE_ORDER: dict[str, int] = {
    "measured": 3,
    "datasheet": 2,
    "vendor": 1,
    "estimated": 0,
}


def weakest_confidence(*values: Confidence) -> Confidence:
    """Return the least trustworthy confidence level among `values`."""
    if not values:
        return "estimated"
    return min(values, key=lambda c: CONFIDENCE_ORDER.get(c, 0))


class Provenance(BaseModel):
    """Where a component profile's numbers came from."""

    model_config = ConfigDict(extra="forbid")

    source_url: str | None = None
    retrieved: str | None = None
    confidence: Confidence = "estimated"
    notes: str | None = None


class ComponentBase(BaseModel):
    """Fields shared by every saved component profile."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable slug used to reference this profile from a setup")
    name: str
    manufacturer: str | None = None
    provenance: Provenance = Field(default_factory=Provenance)

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not v or any(ch.isspace() for ch in v):
            raise ValueError("id must be a non-empty slug without whitespace")
        return v


# --------------------------------------------------------------------------- #
# Motor
# --------------------------------------------------------------------------- #


class ThrustRow(BaseModel):
    """One operating point from a thrust-stand or datasheet table."""

    model_config = ConfigDict(extra="forbid")

    throttle_pct: float | None = None
    thrust_g: float
    rpm: float | None = None
    current_a: float | None = None
    power_w: float | None = None
    torque_nm: float | None = None
    efficiency_g_per_w: float | None = None
    temperature_c: float | None = None

    @model_validator(mode="after")
    def _fill_power(self) -> "ThrustRow":
        # Datasheets vary in which of power/current they publish; derive the other
        # where the test voltage makes it unambiguous (done by ThrustTable).
        if self.efficiency_g_per_w is None and self.power_w:
            object.__setattr__(self, "efficiency_g_per_w", self.thrust_g / self.power_w)
        return self


class ThrustTable(BaseModel):
    """A measured motor+propeller sweep at a fixed bus voltage.

    This is the Tier A data source: it captures real non-idealities that no
    closed-form model reproduces, and is the basis Tyto Robotics' whole design
    methodology rests on.
    """

    model_config = ConfigDict(extra="forbid")

    prop_id: str
    test_voltage_v: float
    esc_id: str | None = None
    label: str | None = Field(
        None,
        description="Short name for this run, e.g. 'Flight Stand 2026-09-01'. A motor can "
                    "hold several tables for the same prop; a setup selects one by label.",
    )
    rows: list[ThrustRow]
    provenance: Provenance = Field(default_factory=Provenance)

    @property
    def display_name(self) -> str:
        """Label if one was given, otherwise something recognisable from the data."""
        if self.label:
            return self.label
        return f"{self.provenance.confidence} @ {self.test_voltage_v:g} V ({len(self.rows)} rows)"

    @field_validator("rows")
    @classmethod
    def _enough_rows(cls, v: list[ThrustRow]) -> list[ThrustRow]:
        if len(v) < 3:
            raise ValueError("a thrust table needs at least 3 rows to interpolate")
        return v

    @model_validator(mode="after")
    def _derive_missing_columns(self) -> "ThrustTable":
        for row in self.rows:
            if row.power_w is None and row.current_a is not None:
                object.__setattr__(row, "power_w", row.current_a * self.test_voltage_v)
            if row.current_a is None and row.power_w is not None:
                object.__setattr__(row, "current_a", row.power_w / self.test_voltage_v)
            if row.efficiency_g_per_w is None and row.power_w:
                object.__setattr__(row, "efficiency_g_per_w", row.thrust_g / row.power_w)
        return self


class Motor(ComponentBase):
    """Brushless outrunner profile.

    `rm_ohm` conventions differ between vendors (phase-to-phase vs per-phase),
    which is why `rm_convention` is explicit and why the fitter in
    `physics.motor` prefers to recover Rm and Io from a thrust table when one
    exists rather than trusting the published figure.
    """

    kv_rpm_per_v: float = Field(gt=0, description="Unloaded rpm per volt of back-EMF")
    weight_g: float = Field(gt=0)
    stator_diameter_mm: float | None = Field(None, gt=0)
    stator_height_mm: float | None = Field(None, gt=0)
    io_a: float | None = Field(None, ge=0)
    io_test_v: float | None = Field(None, gt=0)
    rm_ohm: float | None = Field(None, gt=0, le=10)
    rm_convention: Literal["phase_to_phase", "per_phase"] = "phase_to_phase"
    max_current_a: float | None = Field(None, gt=0)
    max_current_duration_s: float | None = Field(None, gt=0)
    max_power_w: float | None = Field(None, gt=0)
    max_cells_s: int | None = Field(None, ge=1, le=24)
    shaft_mm: float | None = None
    length_mm: float | None = None
    diameter_mm: float | None = None
    mount_pattern: str | None = None
    wire_length_mm: float | None = None
    thrust_tables: list[ThrustTable] = Field(default_factory=list)

    def tables_for(self, prop_id: str) -> list[ThrustTable]:
        """Every table this motor holds for `prop_id`, best-first."""
        return sorted(
            (t for t in self.thrust_tables if t.prop_id == prop_id),
            key=lambda t: -CONFIDENCE_ORDER.get(t.provenance.confidence, 0),
        )

    def table_for(self, prop_id: str, label: str | None = None) -> ThrustTable | None:
        """Return the table to use for `prop_id`, if this motor has one.

        A motor can carry several tables for the same propeller - a manufacturer's
        nominal figures and a run off your own thrust stand, say. `label` picks one
        explicitly; without it the highest-confidence table wins, so a real
        measurement supersedes a datasheet without anyone deleting the datasheet.
        Ties keep the first one listed.

        An unmatched `label` falls back to the automatic choice rather than to no
        table at all: losing Tier A entirely over a stale name would be a much
        worse failure than quietly using the best available data. `Library`
        surfaces the mismatch as a compatibility warning.
        """
        candidates = self.tables_for(prop_id)
        if not candidates:
            return None
        if label:
            for table in candidates:
                if table.display_name == label:
                    return table
        return candidates[0]


# --------------------------------------------------------------------------- #
# Propeller, ESC, battery, frame, payload
# --------------------------------------------------------------------------- #


class Propeller(ComponentBase):
    diameter_in: float = Field(gt=0, le=120)
    pitch_in: float = Field(gt=0, le=60)
    blades: int = Field(2, ge=1, le=12)
    weight_g: float = Field(gt=0)
    material: str | None = None
    thrust_limit_g: float | None = Field(None, gt=0)
    max_rpm: float | None = Field(None, gt=0)
    optimum_rpm_min: float | None = Field(None, gt=0)
    optimum_rpm_max: float | None = Field(None, gt=0)
    propeller_type: str | None = None
    surface_treatment: str | None = None
    dimensions_mm: str | None = None
    working_temp_c_min: float | None = None
    working_temp_c_max: float | None = None
    #: Optional pre-fitted coefficients; normally recovered from a thrust table.
    ct: float | None = Field(None, gt=0, lt=1)
    cp: float | None = Field(None, gt=0, lt=1)

    @property
    def diameter_m(self) -> float:
        return self.diameter_in * 0.0254

    @property
    def disc_area_m2(self) -> float:
        import math

        return math.pi * self.diameter_m**2 / 4.0

    @property
    def pitch_ratio(self) -> float:
        """Pitch/diameter — the single best predictor of hover efficiency."""
        return self.pitch_in / self.diameter_in


class ESC(ComponentBase):
    cont_current_a: float = Field(gt=0)
    burst_current_a: float | None = Field(None, gt=0)
    burst_duration_s: float | None = Field(None, gt=0)
    weight_g: float = Field(gt=0)
    max_cells_s: int | None = Field(None, ge=1, le=24)
    resistance_mohm: float | None = Field(None, ge=0)
    efficiency: float = Field(0.96, gt=0, le=1, description="Conversion efficiency, 0-1")
    bec_current_a: float | None = Field(None, gt=0)
    firmware: str | None = None
    dimensions_mm: str | None = None
    connector: str | None = None
    signal_hz_min: float | None = Field(None, gt=0)
    signal_hz_max: float | None = Field(None, gt=0)


class Battery(ComponentBase):
    chemistry: Literal["lipo", "li-ion", "lihv", "lifepo4"] = "lipo"
    cells_s: int = Field(ge=1, le=24)
    cells_p: int = Field(
        1, ge=1, le=16, description="Parallel strings. Affects pack resistance only - capacity_mah is "
                       "already the pack figure, as datasheets quote it."
    )
    capacity_mah: float = Field(
        gt=0, description="Capacity at the PACK terminals, exactly as the datasheet quotes it. "
                    "A 4S2P 10000 mAh pack is 10000 here, not 5000 per string."
    )
    c_rating_cont: float = Field(gt=0)
    c_rating_burst: float | None = Field(None, gt=0)
    weight_g: float = Field(gt=0)
    internal_resistance_mohm_per_cell: float | None = Field(None, gt=0)
    v_nominal_per_cell: float = Field(3.7, gt=0, le=5)
    v_max_per_cell: float = Field(4.2, gt=0, le=5)
    v_cutoff_per_cell: float = Field(3.5, gt=0, le=5)
    charge_current_a: float | None = Field(None, gt=0)
    discharge_connector: str | None = None
    balance_connector: str | None = None
    dimensions_mm: str | None = None
    cell_model: str | None = None
    max_power_w: float | None = Field(None, gt=0)
    #: Optional (depth-of-discharge fraction, open-circuit V/cell) points.
    #: Left empty, a curve for `chemistry` is used.
    ocv_curve: list[tuple[float, float]] | None = None

    @model_validator(mode="after")
    def _voltage_order(self) -> "Battery":
        if not (self.v_cutoff_per_cell < self.v_nominal_per_cell < self.v_max_per_cell):
            raise ValueError(
                f"cell voltages must increase: cutoff ({self.v_cutoff_per_cell}) < nominal "
                f"({self.v_nominal_per_cell}) < maximum ({self.v_max_per_cell})"
            )
        return self

    @property
    def capacity_ah(self) -> float:
        """Pack capacity. Parallel strings are already accounted for."""
        return self.capacity_mah / 1000.0

    @property
    def v_nominal(self) -> float:
        return self.cells_s * self.v_nominal_per_cell

    @property
    def v_max(self) -> float:
        return self.cells_s * self.v_max_per_cell

    @property
    def energy_wh(self) -> float:
        """Nameplate energy at nominal voltage."""
        return self.capacity_ah * self.v_nominal

    @property
    def specific_energy_wh_per_kg(self) -> float:
        return self.energy_wh / (self.weight_g / 1000.0)

    @property
    def max_cont_current_a(self) -> float:
        return self.c_rating_cont * self.capacity_ah


class Frame(ComponentBase):
    wheelbase_mm: float = Field(gt=0)
    weight_g: float = Field(gt=0)
    arms: int = Field(4, ge=1, le=16)
    max_prop_in: float | None = Field(None, gt=0, le=120)


class PayloadItem(BaseModel):
    """One thing bolted to the airframe that has mass, and may draw power.

    Sensors, companion computers and 3D-printed mounts all live here. Power is
    drawn from the same pack as the propulsion system.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    mass_g: float = Field(0.0, ge=0)
    power_w: float = Field(0.0, ge=0)
    voltage_v: float | None = Field(None, gt=0)
    provenance: Provenance = Field(default_factory=Provenance)


class Payload(ComponentBase):
    items: list[PayloadItem] = Field(default_factory=list)

    @property
    def total_mass_g(self) -> float:
        return sum(i.mass_g for i in self.items)

    @property
    def total_power_w(self) -> float:
        return sum(i.power_w for i in self.items)

    @property
    def power_confidence(self) -> Confidence:
        """Weakest confidence among items that actually draw power."""
        drawing = [i for i in self.items if i.power_w > 0]
        if not drawing:
            return "datasheet"
        return weakest_confidence(*(i.provenance.confidence for i in drawing))


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


class Assumptions(BaseModel):
    """Everything the model needs that is not a component property."""

    model_config = ConfigDict(extra="forbid")

    bec_efficiency: float = Field(0.90, gt=0, le=1, description="Payload regulator efficiency")
    figure_of_merit: float = Field(
        0.65, gt=0, le=1, description="Tier B rotor figure of merit; ignored when a thrust table exists"
    )
    dod_limit: float = Field(
        0.85, gt=0, le=1,
        description="Usable depth of discharge. The 0.85 default is the LiPo cycle-life "
                    "convention; Li-ion packs specified to a low cutoff routinely run "
                    "deeper, so raise it for those.",
    )
    altitude_m: float = 0.0
    temperature_c: float = 15.0
    wiring_resistance_mohm: float = Field(
        3.0, ge=0, description="Pack-to-ESC harness + connector + PDB resistance"
    )
    #: Reserve held back from the endurance figure, on top of the DoD limit.
    reserve_fraction: float = Field(0.0, ge=0, lt=1)


class Setup(BaseModel):
    """A full drone configuration: component ids plus mass and assumptions.

    Mixing and matching is just changing an id — profiles are never duplicated.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str | None = None
    n_rotors: int = Field(4, ge=1, le=16)
    frame_id: str
    motor_id: str
    prop_id: str
    esc_id: str
    battery_id: str
    payload_id: str | None = None
    misc_mass_g: float = Field(
        0.0, ge=0, description="Wiring, PDB, fasteners, prop nuts — anything not in a profile"
    )
    thrust_table: str | None = Field(
        None,
        description="Which of the motor's thrust tables to use, by label. Leave empty to "
                    "take the highest-confidence one automatically.",
    )
    assumptions: Assumptions = Field(default_factory=Assumptions)


class ResolvedSetup(BaseModel):
    """A `Setup` with its component profiles loaded."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    setup: Setup
    frame: Frame
    motor: Motor
    prop: Propeller
    esc: ESC
    battery: Battery
    payload: Payload | None = None

    @property
    def n_rotors(self) -> int:
        return self.setup.n_rotors

    @property
    def dry_mass_g(self) -> float:
        """Everything except the battery."""
        n = self.n_rotors
        mass = (
            self.frame.weight_g
            + n * self.motor.weight_g
            + n * self.prop.weight_g
            + n * self.esc.weight_g
            + self.setup.misc_mass_g
        )
        if self.payload is not None:
            mass += self.payload.total_mass_g
        return mass

    @property
    def auw_g(self) -> float:
        """All-up weight: dry mass plus the battery."""
        return self.dry_mass_g + self.battery.weight_g

    @property
    def payload_power_w(self) -> float:
        return self.payload.total_power_w if self.payload else 0.0

    def mass_breakdown_g(self) -> dict[str, float]:
        n = self.n_rotors
        out = {
            "frame": self.frame.weight_g,
            f"motors x{n}": n * self.motor.weight_g,
            f"props x{n}": n * self.prop.weight_g,
            f"ESCs x{n}": n * self.esc.weight_g,
            "misc/wiring": self.setup.misc_mass_g,
            "payload": self.payload.total_mass_g if self.payload else 0.0,
            "battery": self.battery.weight_g,
        }
        return {k: v for k, v in out.items() if v > 0}
