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

from kelpcompare.registry import (
    find_deployment,
    find_station,
    find_stations,
    load_registry,
    neighbor_refs,
)

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
# A site derived from a polygon rather than placed at a point (docs/03)
# --------------------------------------------------------------------------


def test_an_ordinary_station_derives_from_nothing(tmp_path):
    """The overwhelming majority of sites are an instrument somewhere, so the
    absent block is the ordinary case and not a gap in the record."""
    found = station(tmp_path)
    assert found.derived_from is None
    assert found.is_derived is False


def test_a_derived_site_names_the_polygon_it_reduces(tmp_path):
    found = station(tmp_path, derived_from={"polygon_id": "KELP:LA-JOLLA"})
    assert found.is_derived is True
    assert found.derived_from.polygon_id == "KELP:LA-JOLLA"


def test_derivation_is_declared_rather_than_read_off_the_site_id(tmp_path):
    """The whole point of the block. `SST:LA-JOLLA` resembling `KELP:LA-JOLLA`
    is the string-match between a station name and a polygon name that docs/03's
    integrity rules forbid, so a derived site is one that *says* so and the
    polygon it names is not required to resemble it."""
    found = station(
        tmp_path, site_id="SST:ANYTHING", derived_from={"polygon_id": "KELP:IMPERIAL-BEACH"}
    )
    assert found.derived_from.polygon_id == "KELP:IMPERIAL-BEACH"


@pytest.mark.parametrize(
    "polygon_id",
    ["", "   ", None, 23, ["KELP:LA-JOLLA"]],
    ids=["empty", "blank", "null", "number", "list"],
)
def test_a_derivation_without_a_usable_polygon_id_is_refused(tmp_path, polygon_id):
    """It reaches the polygon registry as a lookup, where a registry typo and a
    polygon nobody has drawn produce the same silence."""
    with pytest.raises(ValueError, match="derived_from.polygon_id"):
        station(tmp_path, derived_from={"polygon_id": polygon_id})


def test_a_polygon_id_is_taken_without_its_surrounding_space(tmp_path):
    found = station(tmp_path, derived_from={"polygon_id": "  KELP:DEL-MAR  "})
    assert found.derived_from.polygon_id == "KELP:DEL-MAR"


@pytest.mark.parametrize("block", ["KELP:LA-JOLLA", [], {}], ids=["string", "list", "empty"])
def test_a_derivation_that_is_not_a_block_is_refused(tmp_path, block):
    with pytest.raises(ValueError, match="`derived_from` on NDBC:TEST"):
        station(tmp_path, derived_from=block)


def test_an_unknown_key_in_the_block_is_refused_rather_than_ignored(tmp_path):
    """A misspelt key in a one-key block is silently the block being absent,
    which downstream reads as an ordinary station with an odd name."""
    with pytest.raises(ValueError, match="polygon_ids"):
        station(tmp_path, derived_from={"polygon_ids": "KELP:LA-JOLLA"})


def test_an_unreadable_derivation_names_the_site_it_is_on(tmp_path):
    with pytest.raises(ValueError, match="SST:LA-JOLLA"):
        station(tmp_path, site_id="SST:LA-JOLLA", derived_from={"polygon_id": ""})


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


def test_find_station_resolves_one_site_id(tmp_path):
    """What `neighbor_refs` hands back is a site_id, so that is what resolves it."""
    loaded = registry(
        tmp_path,
        {"site_id": "NDBC:A", "station_code": "A", "operator": "ndbc"},
        {"site_id": "NDBC:B", "station_code": "B", "operator": "ndbc"},
    )

    assert find_station(loaded, "NDBC:B").station_code == "B"


def test_find_station_does_not_answer_for_a_project_site(tmp_path):
    """A project sensor has no `station_code`, so there is nothing to ask a
    provider for -- and a `neighbor_refs` entry naming one is a registry error
    the caller has to see, not a Station this function should invent."""
    loaded = registry(tmp_path, {"site_id": "PROJ:X", "deployments": []})

    assert find_station(loaded, "PROJ:X") is None


def test_find_station_does_not_answer_for_an_unknown_site_id(tmp_path):
    """A reference naming a station nobody has registered is a gap the caller
    reports; it must not raise here and abort every other pair."""
    loaded = registry(tmp_path, {"site_id": "NDBC:A", "station_code": "A", "operator": "ndbc"})

    assert find_station(loaded, "NDBC:MISSING") is None


def test_neighbor_refs_keep_registry_order(tmp_path):
    """docs/03 calls them ordered: the first is the one to reach for first."""
    loaded = registry(
        tmp_path,
        {"site_id": "PROJ:X", "neighbor_refs": ["NDBC:SECOND", "NDBC:FIRST"], "deployments": []},
    )

    assert neighbor_refs(loaded, "PROJ:X") == ("NDBC:SECOND", "NDBC:FIRST")


def test_neighbor_refs_are_empty_when_undeclared(tmp_path):
    """Empty means nobody has recorded them, which produces no validation rows.
    The alternative -- guessing the nearest station -- would make the table look
    complete on a site nobody has reviewed."""
    loaded = registry(tmp_path, {"site_id": "PROJ:X", "deployments": []})

    assert neighbor_refs(loaded, "PROJ:X") == ()
    assert neighbor_refs(loaded, "PROJ:UNKNOWN") == ()


def test_neighbor_refs_are_a_site_fact_not_a_deployment_one(tmp_path):
    """The same argument as lat/lon: a deployment carrying its own copy could
    disagree with its sibling, so there is nowhere on `Deployment` to put one."""
    found = deployment(tmp_path, serial="1")

    assert not hasattr(found, "neighbor_refs")


def test_the_committed_project_sites_both_name_one_platform_twice():
    """Both TidbiT sites name LJAC1 and 9410230, which are one NOS package --
    docs/04 s1 says validation must not count them as two references, so the
    registry hands over both and the fold happens where the comparison is made."""
    loaded = load_registry(COMMITTED)

    for site_id in ("PROJ:TIDBIT-1", "PROJ:TIDBIT-2"):
        assert neighbor_refs(loaded, site_id) == ("NDBC:LJAC1", "COOPS:9410230")

    ljac1 = find_station(loaded, "NDBC:LJAC1")
    assert ljac1.same_platform_as == ("COOPS:9410230",)
    assert ljac1.depth_for("sea_water_temperature") == 3.4


# --------------------------------------------------------------------------
# The two nearshore Waveriders (46254, 46266)
# --------------------------------------------------------------------------

WAVERIDERS = ("NDBC:46254", "NDBC:46266")


@pytest.mark.parametrize("site_id", WAVERIDERS)
def test_a_waverider_declares_the_three_parameters_it_reports(site_id):
    """Checked against the payloads, not the header: the stdmet layout lists
    every column whether the station has that sensor or not, and these two
    carry data in five of them. `APD` and `MWD` are two of the five and have no
    controlled name in `parameters.json`, so they are absent here rather than
    invented (https://github.com/cweber12/kelp-compare/issues/97)."""
    station = committed(site_id)

    assert station.measured_parameters == (
        "sea_water_temperature",
        "wave_significant_height",
        "wave_peak_period",
    )
    assert not station.measures("air_temperature")
    assert not station.measures("wind_speed")


@pytest.mark.parametrize("site_id", WAVERIDERS)
def test_a_waverider_measures_at_the_surface_not_at_an_intake(site_id):
    """0.46 m below the water line, from the NDBC station pages.

    Pinned because the number is what stops this being compared to a logger at
    another depth (docs/02). It is 2.9 m above LJAC1's intake and 7.8 m above
    PROJ:TIDBIT-1, so these are the shallowest references in the registry --
    nearest in plan distance is not nearest in depth, which bears on the
    neighbour depth tolerance in
    https://github.com/cweber12/kelp-compare/issues/73.
    """
    assert committed(site_id).depth_for("sea_water_temperature") == 0.46


@pytest.mark.parametrize("site_id", WAVERIDERS)
def test_a_waverider_is_its_own_platform(site_id):
    """Neither is co-located with anything already recorded, so the
    one-instrument-counted-twice problem
    (https://github.com/cweber12/kelp-compare/issues/69) does not arise here."""
    assert committed(site_id).same_platform_as == ()


# --------------------------------------------------------------------------
# A station's record spread across more than one dataset (docs/03)
# --------------------------------------------------------------------------


def test_an_ordinary_station_is_one_dataset_with_no_window(tmp_path):
    """Which is every station but one, and the reason callers can be written
    against `datasets` without every registry record growing a block."""
    found = station(tmp_path)
    assert [(d.station_code, d.starts_at, d.ends_at) for d in found.datasets] == [
        ("TEST", None, None)
    ]
    assert found.predecessors == ()
    assert found.datasets[0].is_current


def test_a_project_sensor_spans_no_dataset_at_all(tmp_path):
    """No `station_code`, so there is nothing a fetcher could ask for -- and an
    empty tuple rather than one dataset named by the empty string."""
    loaded = registry(tmp_path, {"site_id": "PROJ:ONE", "operator": "project"})
    assert find_station(loaded, "PROJ:ONE") is None


def test_a_predecessor_and_its_successor_partition_the_timeline(tmp_path):
    """The chain is half-open and consecutive, so no instant belongs to two
    datasets and none belongs to neither."""
    found = station(
        tmp_path,
        station_code="current",
        predecessor_datasets=[
            {"station_code": "older", "covers_until": "2021-11-04T00:00:00Z"},
        ],
    )
    assert [(d.station_code, d.starts_at, d.ends_at) for d in found.datasets] == [
        ("older", None, "2021-11-04T00:00:00Z"),
        ("current", "2021-11-04T00:00:00Z", None),
    ]


def test_only_the_last_dataset_is_the_current_one(tmp_path):
    """`is_current` is what keeps the realtime feed off a dataset whose record
    stopped growing years ago."""
    found = station(
        tmp_path,
        predecessor_datasets=[{"station_code": "older", "covers_until": "2021-11-04T00:00:00Z"}],
    )
    assert [d.is_current for d in found.datasets] == [False, True]


def test_the_oldest_dataset_comes_first_so_the_current_one_wins_a_shared_key(tmp_path):
    """`storage._write_partition` lets the rows written last win, so ingest
    order is what decides which dataset's copy of a shared reading survives."""
    found = station(
        tmp_path,
        station_code="current",
        predecessor_datasets=[
            {"station_code": "oldest", "covers_until": "2020-01-01T00:00:00Z"},
            {"station_code": "middle", "covers_until": "2021-11-04T00:00:00Z"},
        ],
    )
    assert [d.station_code for d in found.datasets] == ["oldest", "middle", "current"]
    assert [d.starts_at for d in found.datasets] == [
        None,
        "2020-01-01T00:00:00Z",
        "2021-11-04T00:00:00Z",
    ]


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2019, ["older"]),
        (2020, ["older"]),
        (2021, ["older", "current"]),
        (2022, ["current"]),
    ],
)
def test_a_year_is_offered_only_to_the_datasets_that_hold_part_of_it(tmp_path, year, expected):
    """The boundary falls inside 2021, so that year alone is asked of both --
    and 2022 is never asked of the dataset that would answer it in the other
    one's depth labels (docs/02)."""
    found = station(
        tmp_path,
        station_code="current",
        predecessor_datasets=[{"station_code": "older", "covers_until": "2021-11-04T00:00:00Z"}],
    )
    assert [d.station_code for d in found.datasets if d.covers_year(year)] == expected


def test_a_predecessor_without_a_boundary_is_refused(tmp_path):
    """The whole point of the block. An unbounded predecessor is fetched over
    its successor's window too, and the two disagree about depth labels."""
    with pytest.raises(ValueError, match="covers_until"):
        station(tmp_path, predecessor_datasets=[{"station_code": "older"}])


@pytest.mark.parametrize(
    "until",
    ["2021-11-04", "2021-11-04T00:00:00", "2021-11-04T00:00:00+00:00", "yesterday", 20211104],
)
def test_a_boundary_that_is_not_a_utc_instant_is_refused_by_name(tmp_path, until):
    """One fixed-width spelling, because `covers_year` compares them as strings
    and a second spelling would compare wrong rather than raise."""
    with pytest.raises(ValueError, match="covers_until"):
        station(
            tmp_path,
            predecessor_datasets=[{"station_code": "older", "covers_until": until}],
        )


def test_boundaries_out_of_order_are_refused_rather_than_read_backwards(tmp_path):
    """Read as a chain, so a list in the wrong order hands a dataset a window
    that runs backwards -- which fetches nothing and says nothing."""
    with pytest.raises(ValueError, match="chain"):
        station(
            tmp_path,
            predecessor_datasets=[
                {"station_code": "a", "covers_until": "2021-11-04T00:00:00Z"},
                {"station_code": "b", "covers_until": "2020-01-01T00:00:00Z"},
            ],
        )


def test_one_dataset_named_twice_is_refused(tmp_path):
    """Including naming the current one as its own predecessor: one dataset
    holds one window, or the manifest reports one dataset under two entries."""
    with pytest.raises(ValueError, match="twice"):
        station(
            tmp_path,
            station_code="current",
            predecessor_datasets=[
                {"station_code": "older", "covers_until": "2020-01-01T00:00:00Z"},
                {"station_code": "current", "covers_until": "2021-11-04T00:00:00Z"},
            ],
        )


def test_a_predecessor_with_no_current_dataset_is_refused(tmp_path):
    """Nothing would hold the record after the last boundary."""
    loaded = registry(
        tmp_path,
        {
            "site_id": "NDBC:TEST",
            "operator": "ndbc",
            "predecessor_datasets": [
                {"station_code": "older", "covers_until": "2021-11-04T00:00:00Z"}
            ],
        },
    )
    with pytest.raises(ValueError, match="station_code"):
        find_stations(loaded, "ndbc")


@pytest.mark.parametrize("block", [[], {}, "older", 0])
def test_a_predecessor_block_that_is_not_a_list_of_datasets_is_refused(tmp_path, block):
    """An empty list is refused with the rest: omitting the block is how the
    registry says there is one dataset, and an empty one says nothing."""
    with pytest.raises(ValueError, match="predecessor_datasets"):
        station(tmp_path, predecessor_datasets=block)


def test_an_entry_that_is_not_a_dataset_names_the_site_it_is_on(tmp_path):
    """A hand-edited registry is the only thing that produces this, so the
    refusal has to say which record to open."""
    with pytest.raises(ValueError, match="NDBC:TEST"):
        station(tmp_path, predecessor_datasets=["point-loma-ocean-outfall-histori"])


def committed_station(site_id: str):
    """One committed public-station record, whatever operator it belongs to.

    `committed` above asks for NDBC's, which is every case that needed one until
    a station's record started spanning two datasets.
    """
    return find_station(load_registry(COMMITTED), site_id)


def test_point_loma_names_the_dataset_that_holds_it_before_2021_11():
    """The one committed site whose record spans two datasets. Pinned here
    rather than in the ingest suite, which tests the mechanism against a
    registry of its own -- this is a fact about the data (docs/02)."""
    datasets = committed_station("SDRTOMS:PLOO").datasets
    assert [d.station_code for d in datasets] == [
        "point-loma-ocean-outfall-histori",
        "point-loma-ocean-outfall-real-ti",
    ]
    assert datasets[0].ends_at == "2021-11-04T00:00:00Z"
    assert datasets[1].starts_at == "2021-11-04T00:00:00Z"


def test_point_loma_declares_the_deep_sensor_the_older_dataset_alone_reports():
    """89 m is the deep sensor's label on the 2020 deployment and appears in no
    other dataset, so nine months of the deepest series in the region exist only
    because the registry declares it."""
    assert 89.0 in committed_station("SDRTOMS:PLOO").declared_depths("sea_water_temperature")


def test_point_loma_does_not_declare_the_depth_the_two_datasets_disagree_on():
    """74 m is the historic dataset's label for the sensor the real-time one
    calls 75 m -- same timestamp, same value to the millidegree (docs/02). Both
    declared, one reading would be stored twice under two permanent depths.

    A deliberate omission is indistinguishable from an oversight in a JSON file,
    which is what this case is for.
    """
    declared = committed_station("SDRTOMS:PLOO").declared_depths("sea_water_temperature")
    assert 74.0 not in declared
    assert 75.0 in declared


def test_south_bay_still_spans_one_dataset():
    """Its historic sibling gives three different answers for where it was, so
    it stays out until the provider fixes that -- the missing thing is a
    reviewed position, not the mechanism to name a second dataset (docs/02)."""
    assert committed_station("SDRTOMS:SBOO").predecessors == ()


# --------------------------------------------------------------------------
# Two deployments that would write one series between them
# --------------------------------------------------------------------------


def logger(
    *,
    serial: str,
    window: tuple[str, str],
    depth_m: float | None = 16.76,
    parameter: str = "sea_water_temperature",
    series: str = "Temperature",
    tz: str = "America/Los_Angeles",
    number: int = 1,
) -> dict:
    record = {
        "serial": serial,
        "deployment_number": number,
        "tz": tz,
        "window_local": list(window),
        "series_map": {series: parameter},
    }
    if depth_m is not None:
        record["depth_m"] = depth_m
    return record


def site(*loggers: dict, site_id: str = "PROJ:STRING") -> dict:
    return {"site_id": site_id, "operator": "project", "deployments": list(loggers)}


def refuses(tmp_path: Path, *sites: dict) -> str:
    with pytest.raises(ValueError) as raised:
        registry(tmp_path, *sites)
    return str(raised.value)


def test_two_loggers_at_one_depth_recording_one_parameter_are_refused(tmp_path):
    """They are not two series. The storage key is (source, site_id, parameter,
    depth_m, timestamp), so their rows land in one series and the deduper drops
    one of every pair sharing a timestamp -- by ingest order, which says nothing
    about which logger was right."""
    message = refuses(
        tmp_path,
        site(
            logger(serial="AAA", window=("2026-09-01 08:00", "2026-09-30 08:00")),
            logger(serial="BBB", window=("2026-09-15 08:00", "2026-10-15 08:00")),
        ),
    )

    assert "PROJ:STRING" in message
    assert "16.76 m" in message
    assert "sea_water_temperature" in message
    assert "serial AAA" in message and "serial BBB" in message
    assert "2026-09-15 15:00Z" in message


def test_a_par_logger_beside_a_tidbit_at_one_depth_is_allowed(tmp_path):
    """The case #71 Decision 2 requires: 55 fsw carries both, and they are two
    series because they carry different parameters. One site and one depth is not
    itself the collision -- a shared parameter is."""
    loaded = registry(
        tmp_path,
        site(
            logger(serial="AAA", window=("2026-09-01 08:00", "2026-09-30 08:00")),
            logger(
                serial="BBB",
                window=("2026-09-01 08:00", "2026-09-30 08:00"),
                parameter="downwelling_photosynthetic_photon_flux_in_sea_water",
                series="PAR",
            ),
        ),
    )

    assert len(loaded.deployments) == 2


def test_the_same_logger_redeployed_is_allowed(tmp_path):
    """Sequential deployments of one instrument are the ordinary case -- the
    reviewed TidbiT is on its third. Only an overlap is refused."""
    loaded = registry(
        tmp_path,
        site(
            logger(serial="AAA", window=("2026-07-01 08:00", "2026-08-01 08:00"), number=1),
            logger(serial="AAA", window=("2026-08-01 08:01", "2026-09-01 08:00"), number=2),
        ),
    )

    assert len(loaded.deployments) == 2


def test_windows_that_meet_at_an_instant_do_overlap(tmp_path):
    """A deployment window is closed at both ends, so the shared instant is a
    sample slot both loggers claim. Refused rather than left to storage."""
    message = refuses(
        tmp_path,
        site(
            logger(serial="AAA", window=("2026-07-01 08:00", "2026-08-01 08:00")),
            logger(serial="BBB", window=("2026-08-01 08:00", "2026-09-01 08:00")),
        ),
    )

    assert "overlap from 2026-08-01 15:00Z to 2026-08-01 15:00Z" in message


def test_two_depths_at_one_site_are_two_series(tmp_path):
    loaded = registry(
        tmp_path,
        site(
            logger(serial="AAA", window=("2026-09-01 08:00", "2026-09-30 08:00"), depth_m=8.23),
            logger(serial="BBB", window=("2026-09-01 08:00", "2026-09-30 08:00"), depth_m=16.76),
        ),
    )

    assert len(loaded.deployments) == 2


def test_one_depth_at_two_sites_is_two_series(tmp_path):
    """Position forks a site record (#48), and two site records are two series
    however deep each one is."""
    loaded = registry(
        tmp_path,
        site(logger(serial="AAA", window=("2026-09-01 08:00", "2026-09-30 08:00")), site_id="P:1"),
        site(logger(serial="BBB", window=("2026-09-01 08:00", "2026-09-30 08:00")), site_id="P:2"),
    )

    assert len(loaded.deployments) == 2


def test_two_unrecorded_depths_collide_like_any_other_repeated_value(tmp_path):
    """An unrecorded depth is one value of the storage key, not an unknown that
    excuses the pair from the check."""
    message = refuses(
        tmp_path,
        site(
            logger(serial="AAA", window=("2026-09-01 08:00", "2026-09-30 08:00"), depth_m=None),
            logger(serial="BBB", window=("2026-09-15 08:00", "2026-10-15 08:00"), depth_m=None),
        ),
    )

    assert "an unrecorded depth" in message


def test_a_deployment_that_cannot_be_placed_in_time_is_left_to_the_ingest_gate(tmp_path):
    """A missing timezone or window is the docs/06 s5 check-4 gate's business,
    which reports it against the file that needs it. Raising here would fail every
    command that merely loads the registry, over a record no file has arrived for."""
    incomplete = logger(serial="BBB", window=("2026-09-15 08:00", "2026-10-15 08:00"))
    del incomplete["tz"]

    loaded = registry(
        tmp_path,
        site(logger(serial="AAA", window=("2026-09-01 08:00", "2026-09-30 08:00")), incomplete),
    )

    assert len(loaded.deployments) == 2


def test_an_unparseable_window_edge_is_left_to_the_ingest_gate_too(tmp_path):
    loaded = registry(
        tmp_path,
        site(
            logger(serial="AAA", window=("2026-09-01 08:00", "2026-09-30 08:00")),
            logger(serial="BBB", window=("whenever", "2026-10-15 08:00")),
        ),
    )

    assert len(loaded.deployments) == 2


def test_the_overlap_is_judged_in_utc_across_two_timezones(tmp_path):
    """Two records written in different zones are still one series, and the
    conversion is what decides it. 01:00 UTC is 18:00 the previous day in
    Los Angeles, so these overlap by an hour despite reading as disjoint locally."""
    message = refuses(
        tmp_path,
        site(
            logger(
                serial="AAA",
                window=("2026-09-01 17:00", "2026-09-01 18:00"),
                tz="America/Los_Angeles",
            ),
            logger(serial="BBB", window=("2026-09-02 00:00", "2026-09-02 01:00"), tz="UTC"),
        ),
    )

    assert "overlap from 2026-09-02 00:00Z to 2026-09-02 01:00Z" in message


def test_the_committed_registry_declares_no_merged_series():
    """The gate is only worth having if the file it guards passes it."""
    assert len(load_registry(COMMITTED).deployments) == 2
