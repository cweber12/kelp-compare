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
# A moored string -- one parameter at many depths, declared as a set
# --------------------------------------------------------------------------


def test_a_depth_set_makes_the_station_self_describing_and_supplies_no_depth(tmp_path):
    """The whole point of the list form: the payload carries the depth, not the registry.

    `depth_for` returning None here is the load-bearing half. A fetcher that fell
    back to it would write one depth for every sensor on the string, collapsing
    eleven series into one -- and `depth_m` is part of `OBSERVATION_KEY`, so that
    is not a mistake a later run can correct (docs/03).
    """
    found = station(tmp_path, sensor_depths_m={"sea_water_temperature": [1.0, 10.0, 18.0]})
    assert found.describes_own_depth("sea_water_temperature")
    assert found.depth_for("sea_water_temperature") is None
    assert found.declared_depths("sea_water_temperature") == (1.0, 10.0, 18.0)


def test_a_scalar_depth_is_not_self_describing_and_still_answers_as_a_set(tmp_path):
    """The two declaration forms flatten to one shape, so callers need only one path."""
    found = station(tmp_path, sensor_depths_m={"sea_water_temperature": 3.4})
    assert not found.describes_own_depth("sea_water_temperature")
    assert found.declared_depths("sea_water_temperature") == (3.4,)


def test_an_undeclared_parameter_declares_no_depths_at_all(tmp_path):
    found = station(tmp_path, sensor_depths_m={"sea_water_temperature": 3.4})
    assert found.declared_depths("air_temperature") == ()
    assert not found.describes_own_depth("air_temperature")


def test_a_depth_set_is_read_as_floats_whatever_the_editor_typed(tmp_path):
    """Same reason `_depth` coerces: a string depth splits a series and nothing raises."""
    found = station(tmp_path, sensor_depths_m={"sea_water_temperature": [1, "10.0", 18.5]})
    depths = found.declared_depths("sea_water_temperature")
    assert depths == (1.0, 10.0, 18.5)
    assert all(isinstance(depth, float) for depth in depths)


def test_a_depth_set_that_is_not_a_number_is_refused_by_name(tmp_path):
    with pytest.raises(ValueError, match="sensor_depths_m.*not a number"):
        station(tmp_path, sensor_depths_m={"sea_water_temperature": [1.0, "deep"]})


def test_an_empty_depth_set_is_refused_rather_than_read_as_undeclared(tmp_path):
    """It would reach the fetcher looking exactly like a source that changed every depth."""
    with pytest.raises(ValueError, match="empty list"):
        station(tmp_path, sensor_depths_m={"sea_water_temperature": []})


def test_the_two_declaration_forms_coexist_on_one_station(tmp_path):
    """A string carrying a met sensor at a fixed height is the ordinary mixed case."""
    found = station(
        tmp_path,
        sensor_depths_m={"sea_water_temperature": [1.0, 10.0], "water_level": 0.0},
    )
    assert found.describes_own_depth("sea_water_temperature")
    assert not found.describes_own_depth("water_level")
    assert found.depth_for("water_level") == 0.0


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


def test_a_numeric_site_id_loads_as_a_string_on_both_halves(tmp_path):
    """The same hole as `depth_m`, on the other key field docs/03 names with it."""
    loaded = registry(
        tmp_path,
        {
            "site_id": 1234,
            "operator": "ndbc",
            "station_code": "TEST",
            "deployments": [{"serial": "22506632"}],
        },
    )
    (found,) = find_stations(loaded, "ndbc")
    assert found.site_id == "1234"
    assert find_deployment(loaded, "22506632").site_id == "1234"


def test_an_explicit_null_site_id_is_empty_rather_than_the_word_none(tmp_path):
    """`site.get("site_id", "")` returns None for a present-but-null key, and a
    bare `str()` would turn that into a site called "None"."""
    loaded = registry(tmp_path, {"site_id": None, "deployments": [{"serial": "22506632"}]})
    assert find_deployment(loaded, "22506632").site_id == ""


# --------------------------------------------------------------------------
# The archive a hand-downloaded station's landings came from (docs/03)
# --------------------------------------------------------------------------


def test_a_station_with_no_archive_block_pins_nothing(tmp_path):
    """Most sites are pulled, and a pulled station has nothing to pin: the
    provider serves whatever is current and the identifier is the version."""
    assert station(tmp_path).archive is None


def test_an_archive_block_pins_the_snapshot_and_how_to_cite_it(tmp_path):
    found = station(
        tmp_path,
        archive={
            "archived": "2026-06-30",
            "source_file": "LaJolla_TEMP_1916-202603.csv",
            "doi": "10.6075/J06T0K0M",
            "citation": "Carter, Melissa L.; ... Award# C22820005.",
        },
    )
    assert found.archive is not None
    assert found.archive.archived == "2026-06-30"
    assert found.archive.source_file == "LaJolla_TEMP_1916-202603.csv"
    assert found.archive.doi == "10.6075/J06T0K0M"
    assert "C22820005" in found.archive.citation


def test_the_archive_date_is_the_raw_landing_directory(tmp_path):
    """So two archives of one cumulative record cannot interleave in `raw/`."""
    found = station(tmp_path, archive={"archived": "2026-06-30"})
    assert found.archive.label == "2026-06-30"


def test_everything_but_the_date_is_optional_provenance(tmp_path):
    found = station(tmp_path, archive={"archived": "2026-06-30"})
    assert (found.archive.source_file, found.archive.doi, found.archive.citation) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    "archived",
    ["June 2026", "2026-6-30", "20260630", "", 20260630, None],
    ids=["prose", "unpadded", "compact", "empty", "number", "null"],
)
def test_an_archive_date_that_is_not_iso_is_refused_by_name(tmp_path, archived):
    """It names a directory and is compared against the file's own header, so a
    spelling nobody can reproduce is a registry error rather than a value."""
    with pytest.raises(ValueError, match="archive.archived"):
        station(tmp_path, archive={"archived": archived})


@pytest.mark.parametrize("block", ["2026-06-30", [], {}], ids=["string", "list", "empty"])
def test_an_archive_that_is_not_a_block_is_refused(tmp_path, block):
    with pytest.raises(ValueError, match="`archive` on NDBC:TEST"):
        station(tmp_path, archive=block)


def test_an_unreadable_archive_names_the_site_it_is_on(tmp_path):
    """Registry errors have to say which record to open, since nothing else can."""
    with pytest.raises(ValueError, match="SIO:LAJOLLA-PIER"):
        find_stations(
            registry(
                tmp_path,
                {
                    "site_id": "SIO:LAJOLLA-PIER",
                    "operator": "sio_shore_stations",
                    "station_code": "LaJolla",
                    "archive": {"archived": "whenever"},
                },
            ),
            "sio_shore_stations",
        )


# --------------------------------------------------------------------------
# A public station's position (docs/03)
# --------------------------------------------------------------------------


def test_a_public_stations_position_reaches_the_station_record(tmp_path):
    """`Deployment` refuses one and `Station` carries one, and the asymmetry is
    deliberate: a project logger can be recording before anyone has surveyed it,
    while a public station's position is something its operator published."""
    found = station(tmp_path, lat=32.866944, lon=-117.257139)
    assert (found.lat, found.lon) == (32.866944, -117.257139)


def test_a_station_with_no_position_declares_none_rather_than_zero(tmp_path):
    found = station(tmp_path)
    assert (found.lat, found.lon) == (None, None)


def test_a_position_is_read_as_a_float_whatever_the_editor_typed(tmp_path):
    found = station(tmp_path, lat="32.866944", lon=-117)
    assert (found.lat, found.lon) == (32.866944, -117.0)


@pytest.mark.parametrize("bad", ["32 deg 52'", "", []], ids=["dms", "empty", "list"])
def test_a_position_that_is_present_and_unreadable_is_refused(tmp_path, bad):
    """Not read as absent. Position is a reviewed fact here -- docs/02 leaves a
    whole RTOMS window out because its provider gave three answers for one -- and
    a coordinate silently dropped turns a placed station into one that matches
    nothing."""
    with pytest.raises(ValueError, match="lat on NDBC:TEST"):
        station(tmp_path, lat=bad, lon=-117.25)


def test_the_committed_shore_station_carries_the_position_its_archive_prints():
    """32 deg 52' 01.0" N 117 deg 15' 25.7" W, from the file's own header."""
    loaded = load_registry(COMMITTED)
    (found,) = find_stations(loaded, "sio_shore_stations")

    assert found.site_id == "SIO:LAJOLLA-PIER"
    assert found.lat == pytest.approx(32 + 52 / 60 + 1.0 / 3600, abs=1e-6)
    assert found.lon == pytest.approx(-(117 + 15 / 60 + 25.7 / 3600), abs=1e-6)
    assert found.archive.archived == "2026-06-30"
    assert found.declared_depths("sea_water_temperature") == (0.5, 5.0)
    assert found.describes_own_depth("sea_water_temperature")
    assert found.depth_for("sea_water_temperature") is None
