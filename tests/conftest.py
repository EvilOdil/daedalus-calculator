"""Fixtures.

Test-only components are built in memory rather than saved into `data/`. The
shipped library is the user's own database and should contain only components
they actually intend to build with.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dronecalc.models import (
    Battery, Motor, Payload, PayloadItem, Propeller, Provenance, ResolvedSetup,
)
from dronecalc.solver import PropulsionSystem
from dronecalc.store import Library

DEFAULT_SETUP = "x500v2-2216-default"


@pytest.fixture(scope="session")
def library() -> Library:
    return Library.load()


@pytest.fixture()
def default_system(library) -> PropulsionSystem:
    return PropulsionSystem(library.resolve(DEFAULT_SETUP))


def system_with(library: Library, **swaps) -> PropulsionSystem:
    """The default setup with components swapped for in-memory ones."""
    resolved: ResolvedSetup = library.resolve(DEFAULT_SETUP)
    for name, obj in swaps.items():
        setattr(resolved, name, obj)
    return PropulsionSystem(resolved)


def reference_lipo_5000() -> Battery:
    """The 4S 5000 mAh LiPo Holybro quote their X500 V2 flight time against.

    Kept in the test suite rather than the library so the published-figure
    validation survives whatever battery the user actually flies.
    """
    return Battery(
        id="_ref-lipo-4s-5000-20c", name="Reference 4S 5000 mAh 20C LiPo",
        chemistry="lipo", cells_s=4, cells_p=1, capacity_mah=5000, c_rating_cont=20,
        weight_g=490, internal_resistance_mohm_per_cell=3.5, v_cutoff_per_cell=3.5,
        provenance=Provenance(
            confidence="estimated",
            notes="Class-typical 20C 4S pack, the size Holybro recommend for the X500 V2."),
    )


def generic_prop(diameter_in: float, pitch_in: float, weight_g: float) -> Propeller:
    """A propeller with no measured data, for Tier B and guard tests."""
    return Propeller(
        id=f"_generic-{diameter_in:g}x{pitch_in:g}", name=f"Generic {diameter_in:g}x{pitch_in:g}",
        diameter_in=diameter_in, pitch_in=pitch_in, weight_g=weight_g,
        provenance=Provenance(confidence="estimated"),
    )


def unrated_motor() -> Motor:
    """A motor declaring no current or power rating - nothing bounds it."""
    return Motor(
        id="_unrated-2213-920kv", name="Unrated 2213 920KV", kv_rpm_per_v=920, weight_g=54,
        max_cells_s=4, provenance=Provenance(confidence="estimated"),
    )


def datasheet_only(motor: Motor) -> Motor:
    """A copy of `motor` carrying only its datasheet tables.

    Holybro's published flight-time and payload claims are derived from nominal
    manufacturer curves, so validating the model against them has to be done on
    the nominal curve. Once a thrust-stand run is added it outranks the datasheet
    everywhere else - which is correct for design work, and exactly wrong here:
    it would turn "does the model reproduce the published number" into "does the
    real hardware match the marketing", which is a different question and not one
    a regression test should silently start answering.
    """
    copy = motor.model_copy(deep=True)
    copy.thrust_tables = [
        t for t in copy.thrust_tables if t.provenance.confidence == "datasheet"
    ]
    return copy


def reference_avionics() -> Payload:
    """The stock X500 V2 avionics Holybro's no-payload figures assume.

    Pinned here for the same reason as the battery and the datasheet table: the
    published figure describes the aircraft as shipped, so the test has to keep
    describing that aircraft however the user's own build evolves.
    """
    return Payload(
        id="_ref-x500v2-avionics", name="Reference X500 V2 avionics",
        items=[
            PayloadItem(name="Pixhawk 6C flight controller", mass_g=35.0, power_w=2.0),
            PayloadItem(name="M9N GPS + compass", mass_g=32.0, power_w=0.6),
            PayloadItem(name="Telemetry radio (915 MHz)", mass_g=20.0, power_w=0.9),
            PayloadItem(name="PM02 power module", mass_g=28.0, power_w=0.4),
        ],
        provenance=Provenance(
            confidence="estimated",
            notes="Stock kit avionics. Draw is an estimate; a clamp-meter reading "
                  "would replace it.",
        ),
    )


@pytest.fixture()
def validation_system(library) -> PropulsionSystem:
    """The stock X500 V2 on the pack Holybro's published figures refer to."""
    return system_with(
        library,
        battery=reference_lipo_5000(),
        motor=datasheet_only(library.motors["holybro-2216-920kv"]),
        payload=reference_avionics(),
    )
