"""Physics invariants that must hold regardless of which components are loaded."""

from __future__ import annotations

import math

import pytest

from dronecalc import endurance
from dronecalc.backends import FileBackend
from dronecalc.metrics import evaluate, power_budget
from dronecalc.physics.atmosphere import air_density, grams_to_newtons
from dronecalc.physics.propeller import ParametricProp, figure_of_merit, ideal_hover_power_w
from dronecalc.solver import PropulsionSystem

from conftest import generic_prop, reference_lipo_5000, system_with, unrated_motor


def test_power_budget_closes(default_system):
    """Itemised losses must sum to the power drawn from the cells."""
    hp = default_system.hover()
    assert power_budget(default_system, hp).closure_error_pct < 0.1


def test_momentum_theory_scales_as_mass_to_the_three_halves():
    """Tier B induced power must follow T^1.5, the momentum-theory signature."""
    rho = 1.225
    area = math.pi * (0.254**2) / 4
    p1 = ideal_hover_power_w(10.0, area, rho)
    p2 = ideal_hover_power_w(20.0, area, rho)
    assert p2 / p1 == pytest.approx(2**1.5, rel=1e-9)


def test_figure_of_merit_is_bounded(default_system):
    """A rotor cannot beat the momentum-theory ideal."""
    hp = default_system.hover()
    fm = figure_of_merit(
        hp.rotor.thrust_n,
        hp.rotor.shaft_power_w,
        default_system.resolved.prop.disc_area_m2,
        hp.rho,
    )
    assert 0.0 < fm < 1.0


def test_motor_efficiency_below_one(default_system):
    hp = default_system.hover()
    assert 0.0 < hp.rotor.motor_efficiency < 1.0


def test_hover_power_rises_with_weight(default_system):
    """More mass is never cheaper to hold up."""
    light = default_system.hover(1500).total_bus_power_w
    heavy = default_system.hover(2000).total_bus_power_w
    assert heavy > light


def test_endurance_falls_with_weight(default_system):
    assert endurance.integrate(default_system, 2000).minutes < endurance.integrate(
        default_system, 1500
    ).minutes


def test_thinner_air_costs_power(library):
    """Hot and high must cost more power than sea level on a cool day."""
    setup = library.setups["x500v2-2216-default"]
    sea = library.resolve(setup.model_copy(deep=True))
    high = setup.model_copy(deep=True)
    high.assumptions.altitude_m = 2000
    high.assumptions.temperature_c = 35
    p_sea = PropulsionSystem(sea).hover().total_bus_power_w
    p_high = PropulsionSystem(library.resolve(high)).hover().total_bus_power_w
    assert p_high > p_sea
    assert air_density(2000, 35) < air_density(0, 15)


def test_payload_power_costs_endurance_but_not_thrust(library):
    """Powering a payload drains the pack without changing what must be lifted.

    This is the accounting you asked for: payload mass drives thrust, payload
    watts drive discharge, and the two are separable.
    """
    setup = library.setups["x500v2-2216-default"]
    base = PropulsionSystem(library.resolve(setup))
    hungry_resolved = library.resolve(setup.model_copy(deep=True))
    for item in hungry_resolved.payload.items:
        item.power_w *= 5.0
    hungry = PropulsionSystem(hungry_resolved)

    assert hungry.hover().auw_g == pytest.approx(base.hover().auw_g)
    # Same thrust demand, so identical rotor operating point...
    assert hungry.hover().rotor.rpm == pytest.approx(base.hover().rotor.rpm, rel=1e-9)
    # ...but more bus power and less endurance.
    assert hungry.hover().total_bus_power_w > base.hover().total_bus_power_w
    assert endurance.integrate(hungry).minutes < endurance.integrate(base).minutes


def test_payload_tax_is_positive_and_bounded(default_system):
    tax = endurance.payload_tax_minutes(default_system)
    total = endurance.integrate(default_system).minutes
    assert 0.0 < tax < total


def test_endurance_rises_with_usable_energy(library):
    """A bigger pack on the same airframe must fly longer here.

    True only while the extra mass has not yet eaten the gain - the seed battery
    family stays on the rising side of that curve, which the knee finder locates.
    """
    small = reference_lipo_5000()
    small.capacity_mah, small.weight_g = 3000, 295
    big = reference_lipo_5000()
    big.capacity_mah, big.weight_g = 6000, 590
    assert (
        endurance.integrate(system_with(library, battery=big)).minutes
        > endurance.integrate(system_with(library, battery=small)).minutes
    )


def test_bigger_slower_prop_is_more_efficient(library):
    """Lower disc loading must buy efficiency - the core of the design ideology."""
    small = system_with(library, prop=generic_prop(9.0, 4.5, 10.0))
    large = system_with(library, prop=generic_prop(13.0, 4.4, 27.0))
    assert large.hover().hover_efficiency_g_per_w > small.hover().hover_efficiency_g_per_w
    assert large.hover().disc_loading_n_per_m2 < small.hover().disc_loading_n_per_m2


def test_tier_b_fallback_is_flagged_estimated(library):
    """A setup with no measured table must announce that it is guessing."""
    system = system_with(library, motor=unrated_motor())
    assert system.tier == "B"
    assert system.confidence == "estimated"
    assert any("momentum theory" in w for w in evaluate(system, with_sensitivity=False).warnings)


def test_available_thrust_falls_as_the_pack_drains(default_system):
    """Back-EMF limits top speed, so a sagging pack lifts less."""
    full = default_system.hover(dod=0.0)
    empty = default_system.hover(dod=0.8)
    assert empty.max_thrust_per_rotor_g <= full.max_thrust_per_rotor_g


def test_search_excludes_what_it_cannot_verify(library, tmp_path):
    """The optimizer must not rank a guess above a measurement.

    Before this guard, a 22x13 motor swinging a 13" propeller topped the ranking
    at ~30 min: momentum theory does not know the motor would cook, the motor
    declares no ratings, and the propeller does not even fit the frame. The junk
    components live in a throwaway library so the real one stays clean.
    """
    from dronecalc import optimize
    from dronecalc.store import Library

    scratch = Library(backend=FileBackend(tmp_path))
    for kind in ("frames", "props", "escs", "motors", "batteries", "payloads", "setups"):
        for cid in library.list_ids(kind):
            scratch.save(kind, getattr(library, kind)[cid])
    scratch.save("props", generic_prop(13.0, 4.4, 27.0))
    scratch.save("motors", unrated_motor())

    # Pin the payload too. The assertion below is that *something* passes the
    # T/W and throttle filters, which stops being true once the user loads the
    # aircraft up - and that would be the test reporting on their build rather
    # than on the optimizer's exclusion logic.
    from conftest import reference_avionics

    scratch.save("payloads", reference_avionics())
    base = scratch.setups["x500v2-2216-default"].model_copy(deep=True)
    base.payload_id = "_ref-x500v2-avionics"
    df = optimize.search(scratch, base)
    passing = df[df["passes"]]

    assert not passing.empty
    assert (passing["tier"] == "A").all()
    assert (passing["prop"] == base.prop_id).all()
    assert (passing["motor"] == base.motor_id).all()

    oversized = df[df["prop"] == "_generic-13x4.4"]
    assert not oversized["passes"].any()
    assert oversized["rejected_because"].str.contains("will not fit").all()

    relaxed = optimize.search(scratch, base, allow_unverifiable=True)
    assert relaxed["passes"].sum() > df["passes"].sum()
    # Even relaxed, a propeller that does not fit the frame stays out.
    assert not relaxed[relaxed["prop"] == "_generic-13x4.4"]["passes"].any()
