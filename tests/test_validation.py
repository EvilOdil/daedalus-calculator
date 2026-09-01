"""Validation against published, independently-measured reality.

These are the tests that stop the model drifting into self-consistent fiction.
Both targets come from Holybro's own X500 V2 figures, which were never used to
tune anything in the model - the physics is driven entirely by the T-Motor
thrust table.
"""

from __future__ import annotations

import pytest

from dronecalc import endurance
from dronecalc.solver import PropulsionSystem

#: Holybro: "approximately 18 minutes hovering with no payload" on a 5000 mAh pack.
HOLYBRO_HOVER_MIN = 18.0
#: Holybro: "maximum payload 1500 g (without battery, 70% throttle)".
HOLYBRO_PAYLOAD_G = 1500.0


def test_hover_endurance_matches_published_figure(validation_system):
    """Predicted hover endurance lands close to Holybro's published ~18 min.

    Run against the 4S 5000 mAh LiPo their figure refers to, not whatever pack
    the library currently carries - otherwise the anchor drifts with the database.

    The band is deliberately wider than the model's own precision: a
    manufacturer's flight-time claim is itself approximate, and pack mass and
    avionics draw are both estimates in the seed data. Tightening this band
    would be tuning to a number rather than validating against it.
    """
    minutes = endurance.integrate(validation_system).minutes
    assert 16.0 <= minutes <= 19.5, f"{minutes:.1f} min is far from the published ~18 min"
    assert abs(minutes - HOLYBRO_HOVER_MIN) / HOLYBRO_HOVER_MIN < 0.12


def test_max_payload_matches_published_figure(validation_system):
    """Payload at 70% throttle reproduces Holybro's 1500 g claim within 10%."""
    payload = validation_system.max_payload_at_throttle_g(70.0)
    assert payload is not None
    assert abs(payload - HOLYBRO_PAYLOAD_G) / HOLYBRO_PAYLOAD_G < 0.10


def test_thrust_table_round_trip(default_system):
    """Every datasheet row, pushed back through the solver, recovers itself.

    Feeding a row's thrust in at the table's own test voltage must return that
    row's rpm and battery current. This is what makes the fitted model a faithful
    re-encoding of the measurement rather than a loose curve near it.
    """
    system = default_system
    table = system.table
    assert table is not None

    for row in table.rows:
        from dronecalc.physics.atmosphere import grams_to_newtons

        rotor = system.rotor_at_thrust(grams_to_newtons(row.thrust_g), table.test_voltage_v)
        rpm_err = abs(rotor.rpm - row.rpm) / row.rpm
        amp_err = abs(rotor.bus_current_a - row.current_a) / row.current_a
        assert rpm_err < 0.02, f"{row.throttle_pct}%: rpm off by {rpm_err:.1%}"
        assert amp_err < 0.05, f"{row.throttle_pct}%: current off by {amp_err:.1%}"


def test_fit_quality(default_system):
    """The joint fit reproduces the datasheet's electrical column tightly."""
    fit = default_system.fit
    assert fit is not None
    assert fit.power_residual_pct < 3.0
    assert fit.ct_scatter_pct < 10.0
    assert 0.35 <= fit.figure_of_merit <= 0.90
    assert fit.warnings == []


def test_fitted_parameters_are_physically_plausible(default_system):
    """Recovered Rm and Io stay in the right ballpark for a 2216-class motor."""
    fit = default_system.fit
    assert 0.02 < fit.rm_ohm < 0.30, f"Rm {fit.rm_ohm * 1000:.0f} mOhm is implausible"
    assert 0.0 <= fit.io_a < 4.0, f"Io {fit.io_a:.2f} A is implausible"
