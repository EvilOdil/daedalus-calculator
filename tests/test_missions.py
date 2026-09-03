"""Mission log: records round-trip through the same store as component profiles."""

from __future__ import annotations

import pytest

from dronecalc.backends import FileBackend
from dronecalc.missions import Mission, MissionFlight, generate_mission_id
from dronecalc.store import Library

DEFAULT_SETUP = "x500v2-2216-default"


def _mission(**overrides) -> Mission:
    fields = dict(
        id="_test-mission",
        name="Test mission",
        setup_id=DEFAULT_SETUP,
        location="Test field",
        flights=[
            MissionFlight(
                date="2026-09-01",
                ardupilot_log_url="https://example.com/logs/flight1.bin",
                wind_speed_mps=3.5,
                video_stream=True,
                threat_detection=False,
                precision_landing=True,
                notes="Calm morning, nominal flight.",
            )
        ],
    )
    fields.update(overrides)
    return Mission(**fields)


def test_mission_round_trips_through_the_store(library, tmp_path):
    lib = Library(backend=FileBackend(tmp_path))
    mission = _mission()
    lib.save("missions", mission)
    reloaded = Library.load(tmp_path)
    assert reloaded.missions["_test-mission"] == mission
    assert reloaded.missions["_test-mission"].flights[0].wind_speed_mps == 3.5


def test_missions_for_setup_filters_by_setup_id(library, tmp_path):
    lib = Library(backend=FileBackend(tmp_path))
    lib.save("missions", _mission(id="_m1", setup_id="x500v2-2216-default"))
    lib.save("missions", _mission(id="_m2", setup_id="scouts_v2_0"))
    matches = lib.missions_for_setup("x500v2-2216-default")
    assert [m.id for m in matches] == ["_m1"]


def test_flight_checkboxes_default_false():
    flight = MissionFlight()
    assert flight.video_stream is False
    assert flight.threat_detection is False
    assert flight.precision_landing is False
    assert flight.wind_speed_mps is None


def test_negative_wind_speed_is_rejected():
    with pytest.raises(Exception):
        MissionFlight(wind_speed_mps=-1.0)


def test_mission_rejects_unknown_fields():
    with pytest.raises(Exception):
        Mission(id="x", name="x", setup_id="x", not_a_field=True)


def test_mission_carries_no_date():
    """A mission is a place, not a point in time - only its flights are dated."""
    assert "date" not in Mission.model_fields


def test_mission_names_can_repeat_across_missions(tmp_path):
    """The same named campaign can legitimately be logged as separate mission
    records - only the id has to be unique, names are never validated as such."""
    lib = Library(backend=FileBackend(tmp_path))
    lib.save("missions", _mission(id="_m1"))
    lib.save("missions", Mission(id="_m2", name="Test mission", setup_id=DEFAULT_SETUP))
    reloaded = Library.load(tmp_path)
    names = {reloaded.missions["_m1"].name, reloaded.missions["_m2"].name}
    assert names == {"Test mission"}


def test_generate_mission_id_slugifies_the_name():
    assert generate_mission_id("Scouts V2 Field Test", []) == "scouts-v2-field-test"
    assert generate_mission_id("  weird///chars!!  ", []) == "weird-chars"
    assert generate_mission_id("", []) == "mission"


def test_generate_mission_id_resolves_collisions_with_a_numeric_suffix():
    """Two missions can legitimately share a name - the id generator must not
    refuse that, just keep ids unique."""
    first = generate_mission_id("Scouts V2 field test", [])
    second = generate_mission_id("Scouts V2 field test", [first])
    third = generate_mission_id("Scouts V2 field test", [first, second])
    assert len({first, second, third}) == 3
    assert first == "scouts-v2-field-test"
    assert second == "scouts-v2-field-test-2"
    assert third == "scouts-v2-field-test-3"


def test_one_bad_mission_file_does_not_take_down_the_library(library, tmp_path):
    scratch = Library(backend=FileBackend(tmp_path))
    scratch.save("missions", _mission())
    (tmp_path / "missions" / "broken.json").write_text('{"id": "broken"}')

    reloaded = Library.load(tmp_path)
    assert any("broken.json" in e for e in reloaded.load_errors)
    assert "_test-mission" in reloaded.missions


