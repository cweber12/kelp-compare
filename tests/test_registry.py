"""Public-station records in `sites.json` (docs/03 "Site registry").

The deployment half's *rules* are exercised through the adapter and ingest
suites, which is where they bite. This file covers `Station` — what a fetcher is
allowed to ask the registry about a public station — and in particular the
distinction the registry exists to hold: a station that has no instrument for a
parameter, versus one nobody has checked yet.

It also covers what both halves owe the reader of a hand-maintained JSON file:
that a field's declared type is the type that comes back, whatever the editor
typed. That belongs here rather than downstream because the failure it prevents
is silent everywhere else (docs/03 "Partition files and idempotence").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kelpcompare.registry import find_deployment, find_stations, load_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED = REPO_ROOT / "data" / "registry" / "sites.json"


def registry(tmp_path: Path, *sites: dict):
    target = tmp_path / "sites.json"
    target.write_text(json.dumps({"sites": list(sites)}), encoding="utf-8")
    return load_registry(target)


def station(tmp_path: Path, **fields):
    record = {"site_id": "NDBC:TEST", "operator": "ndbc", "station_code": "TEST", **fields}
    (found,) = find_stations(registry(tmp_path, record), "ndbc")
    return found


def deployment(tmp_path: Path, *, site_id: str = "PROJ:TEST", **fields):
    record = {"serial": "22506632", **fields}
    loaded = registry(tmp_path, {"site_id": site_id, "deployments": [record]})
    return find_deployment(loaded, "22506632")


def committed(site_id: str):
    loaded = load_registry(COMMITTED)
    return next(s for s in find_stations(loaded, "ndbc") if s.site_id == site_id)


# --------------------------------------------------------------------------
# Which stations a fetcher is offered
# --------------------------------------------------------------------------


def test_only_the_requested_operators_stations_come_back(tmp_path):
    loaded = registry(
        tmp_path,
        {"site_id": "NDBC:A", "operator": "ndbc", "station_code": "A"},
        {"site_id": "COOPS:B", "operator": "coops", "station_code": "B"},
    )
    assert [s.site_id for s in find_stations(loaded, "ndbc")] == ["NDBC:A"]


def test_a_site_with_no_station_code_is_skipped(tmp_path):
    """Without the identifier its provider knows it by there is nothing to ask for."""
    loaded = registry(
        tmp_path,
        {"site_id": "NDBC:A", "operator": "ndbc", "station_code": "A"},
        {"site_id": "NDBC:NOCODE", "operator": "ndbc"},
    )
    assert [s.site_id for s in find_stations(loaded, "ndbc")] == ["NDBC:A"]


def test_site_order_is_preserved_so_a_whole_operator_run_is_reproducible(tmp_path):
    loaded = registry(
        tmp_path,
        {"site_id": "NDBC:B", "operator": "ndbc", "station_code": "B"},
        {"site_id": "NDBC:A", "operator": "ndbc", "station_code": "A"},
    )
    assert [s.site_id for s in find_stations(loaded, "ndbc")] == ["NDBC:B", "NDBC:A"]


# --------------------------------------------------------------------------
# Sensor depth -- absence means "not published", never "not measured"
# --------------------------------------------------------------------------


def test_a_declared_depth_is_returned_and_an_undeclared_one_is_none(tmp_path):
    found = station(tmp_path, sensor_depths_m={"sea_water_temperature": 3.4})
    assert found.depth_for("sea_water_temperature") == 3.4
    assert found.depth_for("air_temperature") is None


# --------------------------------------------------------------------------
# Measured parameters -- "no instrument" versus "nobody checked"
# --------------------------------------------------------------------------


def test_a_station_measures_exactly_what_it_declares(tmp_path):
    found = station(tmp_path, measured_parameters=["sea_water_temperature", "wind_speed"])
    assert found.declares_parameters
    assert found.measures("sea_water_temperature")
    assert not found.measures("wave_significant_height")


def test_an_undeclared_station_measures_everything_rather_than_nothing(tmp_path):
    """Refusing instead would turn an unrecorded fact into missing data."""
    found = station(tmp_path)
    assert not found.declares_parameters
    assert found.measures("wave_significant_height")


def test_an_empty_list_reads_as_undeclared_not_as_measures_nothing(tmp_path):
    """A station that measured nothing would not be in the registry to be fetched."""
    found = station(tmp_path, measured_parameters=[])
    assert not found.declares_parameters
    assert found.measures("wave_significant_height")


def test_a_met_parameter_is_measured_without_having_a_depth(tmp_path):
    """Which is why this cannot be derived from `sensor_depths_m`: absence there
    means no depth published, not no sensor."""
    found = station(
        tmp_path,
        sensor_depths_m={"sea_water_temperature": 3.4},
        measured_parameters=["sea_water_temperature", "air_temperature"],
    )
    assert found.measures("air_temperature")
    assert found.depth_for("air_temperature") is None


# --------------------------------------------------------------------------
# The committed registry
# --------------------------------------------------------------------------


def test_ljac1_declares_the_three_parameters_it_has_instruments_for():
    """A shore station with no wave sensor. https://github.com/cweber12/kelp-compare/issues/21"""
    ljac1 = committed("NDBC:LJAC1")
    assert ljac1.measured_parameters == (
        "sea_water_temperature",
        "air_temperature",
        "wind_speed",
    )
    assert not ljac1.measures("wave_significant_height")
    assert not ljac1.measures("wave_peak_period")


def test_ljac1_still_carries_its_intake_depth_and_its_platform_twin():
    ljac1 = committed("NDBC:LJAC1")
    assert ljac1.depth_for("sea_water_temperature") == 3.4
    assert ljac1.same_platform_as == ("COOPS:9410230",)


# --------------------------------------------------------------------------
# Key fields are coerced on load -- a hand-edit must not change a row's identity
#
# `site_id` and `depth_m` are two of the four `storage.OBSERVATION_KEY`
# components and both are read from here, not from the instrument's file. The
# partition write dedupes *before* it casts dtypes, so a key part that arrives
# as the wrong type does not compare equal to the same value already on disk:
# the reading survives twice and nothing raises. Asserting on the type rather
# than the value is the whole point -- `"8.23" == 8.23` is False, which is
# exactly the bug, so a value-only assertion would pass with it present.
# --------------------------------------------------------------------------


def test_a_string_depth_loads_as_a_float(tmp_path):
    """The hand-edit this exists for: quoting a number changes nothing visible."""
    found = deployment(tmp_path, depth_m="8.23")
    assert found.depth_m == 8.23
    assert isinstance(found.depth_m, float)


def test_an_integer_depth_loads_as_a_float(tmp_path):
    """A whole-metre depth is the one an editor is most likely to write unquoted."""
    found = deployment(tmp_path, depth_m=8)
    assert isinstance(found.depth_m, float)


def test_a_null_depth_stays_none_rather_than_becoming_a_number(tmp_path):
    """Unsurveyed is not a depth. `float(None)` would be a crash; 0.0 would be a lie."""
    assert deployment(tmp_path, depth_m=None).depth_m is None
    assert deployment(tmp_path).depth_m is None


def test_a_depth_that_is_not_a_number_is_refused_at_load(tmp_path):
    """Left uncoerced this still fails, but inside the partition write -- after the
    fetch and QC are done, and naming storage rather than the field at fault."""
    with pytest.raises(ValueError, match="depth_m on PROJ:TEST"):
        deployment(tmp_path, depth_m="8.23 m")


def test_the_committed_registrys_depths_are_floats():
    """The file this all protects. Both deployments carry a surveyed depth."""
    loaded = load_registry(COMMITTED)
    assert loaded.deployments
    assert all(d.depth_m is None or isinstance(d.depth_m, float) for d in loaded.deployments)
