"""The component library loads, round-trips and reports provenance honestly."""

from __future__ import annotations

import pytest

from conftest import generic_prop, unrated_motor
from dronecalc.backends import FileBackend
from dronecalc.models import CONFIDENCE_ORDER, weakest_confidence
from dronecalc.store import Library, LibraryError


def test_library_loads_cleanly(library):
    assert library.list_ids("setups")
    assert "x500v2-2216-default" in library.setups


def test_every_profile_declares_provenance(library):
    for kind in ("motors", "props", "escs", "batteries", "frames", "payloads"):
        for cid in library.list_ids(kind):
            prov = getattr(library, kind)[cid].provenance
            assert prov.confidence in CONFIDENCE_ORDER, f"{kind}/{cid}"
            if prov.confidence != "estimated":
                assert prov.source_url, f"{kind}/{cid} claims {prov.confidence} with no source"


def test_json_round_trip(library, tmp_path):
    lib = Library(backend=FileBackend(tmp_path))
    motor = library.motors["holybro-2216-920kv"]
    lib.save("motors", motor)
    reloaded = Library.load(tmp_path)
    assert reloaded.motors["holybro-2216-920kv"] == motor


def test_unknown_id_names_the_alternatives(library):
    setup = library.setups["x500v2-2216-default"].model_copy(update={"motor_id": "nope"})
    with pytest.raises(LibraryError, match="Known:"):
        library.resolve(setup)


def test_weakest_confidence_wins():
    assert weakest_confidence("measured", "estimated", "datasheet") == "estimated"
    assert weakest_confidence("measured", "datasheet") == "datasheet"


def test_overvolting_is_a_hard_incompatibility(library):
    """A 6S pack on a 4S-rated motor is an error, not a warning."""
    resolved = library.resolve("x500v2-2216-default")
    resolved.battery.cells_s = 6
    assert any("6S" in e for e in library.compatibility_errors(resolved))


def test_missing_table_is_reported_as_a_warning(library):
    resolved = library.resolve("x500v2-2216-default")
    resolved.prop = generic_prop(10.0, 4.5, 13.0)
    warns = library.compatibility_warnings(resolved)
    assert any("momentum theory" in w for w in warns)


def test_resolve_returns_an_independent_copy(library):
    """Editing a resolved setup must not reach back into the library.

    Sweeps and sensitivity runs mutate resolved setups as a matter of course
    (heavier pack, payload power zeroed). When `resolve` handed out references to
    the shared profiles, one such edit rewrote every other setup using that
    component and silently changed later results.
    """
    first = library.resolve("x500v2-2216-default")
    original_cells = first.battery.cells_s
    original_power = first.payload.items[0].power_w

    first.battery.cells_s = 6
    first.payload.items[0].power_w = 999.0
    first.motor.kv_rpm_per_v = 1.0

    second = library.resolve("x500v2-2216-default")
    assert second.battery.cells_s == original_cells
    assert second.payload.items[0].power_w == original_power
    assert second.motor.kv_rpm_per_v == library.motors["holybro-2216-920kv"].kv_rpm_per_v


def test_oversized_prop_is_a_hard_incompatibility(library):
    """A propeller that fouls the airframe is not a marginal design."""
    resolved = library.resolve("x500v2-2216-default")
    resolved.prop = generic_prop(13.0, 4.4, 27.0)
    assert any("will not fit" in e for e in library.compatibility_errors(resolved))


def test_motor_without_ratings_is_flagged_unverifiable(library):
    """Nothing in the model stops an unrated motor being over-worked, so say so."""
    resolved = library.resolve("x500v2-2216-default")
    resolved.motor = unrated_motor()
    warns = library.compatibility_warnings(resolved)
    assert any("declares no current or power rating" in w for w in warns)


def test_field_constraints_reject_impossible_values(library):
    """Inline editing writes straight to disk, so the model has to hold the line."""
    from dronecalc.models import Battery, ESC, Propeller

    esc = library.escs["blheli-s-20a"]
    for field, bad in [("efficiency", 5.0), ("cont_current_a", -1.0), ("weight_g", 0.0)]:
        broken = esc.model_dump()
        broken[field] = bad
        with pytest.raises(Exception):
            ESC.model_validate(broken)

    prop = library.props["t1045ii"].model_dump()
    prop["blades"] = 0
    with pytest.raises(Exception):
        Propeller.model_validate(prop)

    batt = library.batteries["upgrade-energy-red-v4-4s2p-10000"].model_dump()
    batt["v_cutoff_per_cell"] = 4.5  # above nominal and maximum
    with pytest.raises(Exception, match="must increase"):
        Battery.model_validate(batt)


def test_one_bad_file_does_not_take_down_the_library(library, tmp_path):
    """A hand-edited profile that will not validate is skipped, not fatal."""
    scratch = Library(backend=FileBackend(tmp_path))
    for kind in ("frames", "props", "escs", "motors", "batteries", "payloads", "setups"):
        for cid in library.list_ids(kind):
            scratch.save(kind, getattr(library, kind)[cid])
    (tmp_path / "props" / "broken.json").write_text('{"id": "broken", "name": "x"}')

    reloaded = Library.load(tmp_path)
    assert len(reloaded.load_errors) == 1
    assert "broken.json" in reloaded.load_errors[0]
    # Everything valid still loaded.
    assert "t1045ii" in reloaded.props
    assert reloaded.resolve("x500v2-2216-default").prop.id == "t1045ii"


def test_setup_can_pin_a_specific_thrust_table(library):
    """A setup names the table it trusts; without a name, confidence decides."""
    motor = library.motors["holybro-2216-920kv"]
    tables = motor.tables_for("t1045ii")
    assert len(tables) > 1, "this test needs a motor carrying competing tables"

    # Automatic: best confidence wins, and is listed first.
    assert motor.table_for("t1045ii") is tables[0]
    assert tables[0].provenance.confidence == "measured"

    # Explicit: the named table is used even though it ranks lower.
    weaker = tables[-1]
    assert motor.table_for("t1045ii", weaker.display_name) is weaker

    # And the choice reaches the solver, changing the answer it gives.
    from dronecalc.solver import PropulsionSystem

    auto = library.resolve("x500v2-2216-default")
    pinned = library.resolve("x500v2-2216-default")
    pinned.setup.thrust_table = weaker.display_name
    assert PropulsionSystem(auto).table.display_name != (
        PropulsionSystem(pinned).table.display_name
    )


def test_a_stale_table_name_warns_but_still_flies(library):
    """A name that no longer exists must not silently drop the setup to Tier B."""
    resolved = library.resolve("x500v2-2216-default")
    resolved.setup.thrust_table = "a run that was deleted"

    from dronecalc.solver import PropulsionSystem

    assert PropulsionSystem(resolved).tier == "A"
    warnings = library.compatibility_warnings(resolved)
    assert any("a run that was deleted" in w for w in warnings)
