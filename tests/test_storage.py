"""Zone layout, the schema guard, and dedupe on write (docs/03).

Every test writes under `tmp_path`. Nothing here may touch the repo's own
`data/` root -- raw is append-only forever (CLAUDE.md hard rule 1), and a test
that wrote there would be unrepeatable by construction.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kelpcompare import storage
from kelpcompare.storage import (
    OBSERVATION_COLUMNS,
    Zones,
    empty_observations,
    validate_frame,
    write_observations,
)

RUN_A = "20260824T120000000Z-ingest"
RUN_B = "20260824T130000000Z-ingest"


def observations(timestamps, *, values=None, run_id=RUN_A, depth_m=None, site="PROJ:TIDBIT-1"):
    """A minimal docs/03 frame: UTC-aware timestamps in, everything else fixed."""
    index = pd.to_datetime(list(timestamps), utc=True)
    count = len(index)
    return pd.DataFrame(
        {
            "timestamp": index,
            "site_id": site,
            "parameter": "sea_water_temperature",
            "value": values if values is not None else [17.0] * count,
            "depth_m": depth_m,
            "qc_flag": 2,
            "qc_tests": "deployment_window:pass",
            "source": "project",
            "fetch_run_id": run_id,
        }
    )[list(OBSERVATION_COLUMNS)]


# --------------------------------------------------------------------------
# Zones
# --------------------------------------------------------------------------


def test_zones_lay_out_the_documented_directories(tmp_path):
    zones = Zones.at(tmp_path)
    assert zones.observations == tmp_path / "observations"
    assert zones.features == tmp_path / "features"
    assert zones.manifests == tmp_path / "raw" / "_manifests"
    assert zones.quarantine == tmp_path / "quarantine"
    assert zones.sites_json == tmp_path / "registry" / "sites.json"
    assert zones.features_json == tmp_path / "registry" / "features.json"
    assert zones.raw_source("project_sensors") == tmp_path / "raw" / "project_sensors"
    assert zones.partition("project", 2026) == (
        tmp_path / "observations" / "source=project" / "year=2026"
    )


def test_zones_default_to_the_data_directory():
    assert Zones.at().root == storage.DEFAULT_ROOT
    assert Zones().observations == storage.DEFAULT_ROOT / "observations"


# --------------------------------------------------------------------------
# The schema guard
# --------------------------------------------------------------------------


def test_naive_timestamps_are_refused(tmp_path):
    """The last place a local or naive timestamp could reach storage."""
    frame = observations(["2026-07-11 14:00"])
    frame["timestamp"] = frame["timestamp"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)


def test_non_utc_timestamps_are_refused(tmp_path):
    frame = observations(["2026-07-11 14:00"])
    frame["timestamp"] = frame["timestamp"].dt.tz_convert("America/Los_Angeles")
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)


def test_a_stray_column_is_refused(tmp_path):
    frame = observations(["2026-07-11 14:00"])
    frame["unit"] = "degC"
    with pytest.raises(ValueError, match="unexpected"):
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)


def test_a_missing_column_is_refused(tmp_path):
    frame = observations(["2026-07-11 14:00"]).drop(columns=["qc_tests"])
    with pytest.raises(ValueError, match="missing"):
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)


def test_a_numeric_looking_string_depth_is_refused(tmp_path):
    """The dtype that reached stored Parquet in #47, refused at the gate (#57).

    `depth_m` is part of `OBSERVATION_KEY`, so a string that looks like a number
    keys differently from the float it is about to become: it splits the dedupe
    key silently instead of raising. Naming the column is most of the point.
    """
    frame = observations(["2026-07-11 14:00"], depth_m="8.23")
    with pytest.raises(ValueError, match="depth_m") as refused:
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)
    assert "str" in str(refused.value)


def test_a_depth_that_is_not_a_number_at_all_is_refused(tmp_path):
    """The same gate, reached before the partition writer's `astype` can raise.

    Ungated this aborted inside pandas with `could not convert string to float:
    '8.23 m': Error while type casting for column 'depth_m'`, after the fetch,
    parse, normalize and QC work was already done, and naming the storage layer
    rather than the field that produced it.
    """
    zones = Zones.at(tmp_path)
    frame = observations(["2026-07-11 14:00"], depth_m="8.23 m")
    with pytest.raises(ValueError, match="depth_m"):
        write_observations(frame, zones, source="project", run_id=RUN_A)
    assert not zones.observations.exists()


def test_a_string_value_is_refused(tmp_path):
    """Not a key column, but the one other column whose bad dtype aborts a run."""
    frame = observations(["2026-07-11 14:00"], values=["17.0"])
    with pytest.raises(ValueError, match="'value'"):
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)


def test_a_non_string_site_id_is_refused(tmp_path):
    """Also a key component: a float site id keys differently from its label."""
    frame = observations(["2026-07-11 14:00"], site=1.0)
    with pytest.raises(ValueError, match="site_id"):
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)


def test_a_non_string_parameter_is_refused(tmp_path):
    frame = observations(["2026-07-11 14:00"])
    frame["parameter"] = 3.0
    with pytest.raises(ValueError, match="parameter"):
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)


def test_a_depth_null_on_every_row_is_accepted(tmp_path):
    """A deployment with no recorded depth is the normal case, and it is `object`.

    docs/03 says `depth_m` is null for met parameters, and a frame built with
    that column null on every row carries `object` rather than `float64`. The
    numeric check therefore has to accept an entirely-null object column, or the
    ordinary shape of a met series would be refused.
    """
    frame = observations(["2026-07-11 14:00"])
    assert frame["depth_m"].dtype == object
    validate_frame(frame)


def test_an_integer_depth_and_value_are_accepted(tmp_path):
    """`int64` converts to the stored `float64` losslessly, so the gate is a
    predicate on the dtype family, not equality with `OBSERVATION_DTYPES`."""
    frame = observations(["2026-07-11 14:00"], values=[17], depth_m=8)
    assert (frame["value"].dtype, frame["depth_m"].dtype) == ("int64", "int64")
    validate_frame(frame)
    assert len(write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)) == 1


def test_a_depth_recorded_for_one_parameter_and_not_another_is_accepted(tmp_path):
    """The shape every multi-parameter fetcher builds, and the reason an `object`
    column is judged on its values rather than on being entirely null.

    A station reporting a water temperature at a known depth and an air
    temperature at no depth concatenates to one `object` column of floats and
    nulls. Measured while building this gate: refusing that took 31 tests with
    it, across the NDBC fetcher and the ingest, QC and features CLIs.
    """
    water = observations(["2026-07-11 14:00"], depth_m=8.23)
    air = observations(["2026-07-11 14:00"])
    air["parameter"] = "air_temperature"
    frame = pd.concat([water, air], ignore_index=True)

    assert frame["depth_m"].dtype == object
    validate_frame(frame)
    assert len(write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)) == 1


def test_a_site_id_carried_as_an_object_column_of_strings_is_accepted():
    """What an ordinary string column is under the pandas 2.2 floor in pyproject."""
    frame = observations(["2026-07-11 14:00"])
    frame["site_id"] = frame["site_id"].astype(object)
    validate_frame(frame)


def test_a_depth_that_is_a_float_on_one_row_and_a_string_on_another_is_refused(tmp_path):
    """The carve-out is for a column that is null throughout, not for `object`."""
    frame = observations(["2026-07-11 14:00", "2026-07-11 14:10"], depth_m=[8.23, "8.23"])
    assert frame["depth_m"].dtype == object
    with pytest.raises(ValueError, match="depth_m"):
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)


def test_a_boolean_value_is_refused(tmp_path):
    """pandas counts `bool` as numeric; storage does not -- `True` is not 1.0 degC."""
    frame = observations(["2026-07-11 14:00"], values=[True])
    with pytest.raises(ValueError, match="'value'"):
        write_observations(frame, Zones.at(tmp_path), source="project", run_id=RUN_A)


def test_the_stored_empty_frame_passes_the_guard_too():
    """The other form of `empty_observations`, which is what an empty read returns.

    Its timestamp is naive by design (DuckDB reads a naive column as TIMESTAMP,
    not TIMESTAMPTZ in the reader's zone), so the guard is given the frame the
    way the CLI hands it on after a read: localized, everything else untouched.
    """
    stored = empty_observations(stored=True)
    stored["timestamp"] = stored["timestamp"].dt.tz_localize("UTC")
    validate_frame(stored)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def test_write_partitions_by_source_and_year(tmp_path):
    zones = Zones.at(tmp_path)
    frame = observations(["2025-12-31 23:00", "2026-01-01 01:00", "2026-07-11 14:00"])
    written = write_observations(frame, zones, source="project", run_id=RUN_A)

    assert [p.parent.name for p in written] == ["year=2025", "year=2026"]
    assert all(p.parent.parent.name == "source=project" for p in written)
    assert all(p.name == f"part-{RUN_A}.parquet" for p in written)


def test_stored_timestamps_are_naive_utc(tmp_path):
    """Naive on disk so DuckDB reads TIMESTAMP, not TIMESTAMPTZ in a local zone."""
    zones = Zones.at(tmp_path)
    write_observations(observations(["2026-07-11 14:00"]), zones, source="project", run_id=RUN_A)
    stored = storage.read_observations(zones)
    assert stored["timestamp"].dt.tz is None
    assert stored["timestamp"].iloc[0] == pd.Timestamp("2026-07-11 14:00")


def test_an_empty_frame_writes_nothing(tmp_path):
    zones = Zones.at(tmp_path)
    assert write_observations(observations([]), zones, source="project", run_id=RUN_A) == ()
    assert not zones.observations.exists()


def test_the_empty_frame_the_pipeline_builds_is_also_accepted(tmp_path):
    """The case above passes a hand-typed frame, which was never the failing one.

    `pd.DataFrame(columns=OBSERVATION_COLUMNS)` -- what the parsers built for
    "no rows" -- types every column `object`, so `validate_frame` rejected it as
    not timezone-aware UTC and the documented "writes nothing" path raised
    instead. `empty_observations` is now the single definition of that frame.
    """
    zones = Zones.at(tmp_path)
    validate_frame(empty_observations())
    assert write_observations(empty_observations(), zones, source="project", run_id=RUN_A) == ()
    assert not zones.observations.exists()


def test_the_untyped_empty_frame_is_still_refused(tmp_path):
    """Accepting the schema is not the same as accepting anything empty.

    Hard rule 2 is enforced on the dtype, not on the row count: a frame with no
    rows and an `object` timestamp is not the docs/03 schema, and a caller that
    builds one by hand should still hear about it.
    """
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        write_observations(
            pd.DataFrame(columns=list(OBSERVATION_COLUMNS)),
            Zones.at(tmp_path),
            source="project",
            run_id=RUN_A,
        )


def test_rows_are_stored_in_time_order(tmp_path):
    zones = Zones.at(tmp_path)
    frame = observations(["2026-07-11 16:00", "2026-07-11 14:00", "2026-07-11 15:00"])
    write_observations(frame, zones, source="project", run_id=RUN_A)
    stored = storage.read_observations(zones)
    assert stored["timestamp"].is_monotonic_increasing


# --------------------------------------------------------------------------
# Dedupe -- docs/06 s5 check 5
# --------------------------------------------------------------------------


def test_overlapping_readouts_dedupe_to_one_row_each(tmp_path):
    """Readouts of a running logger overlap by design; observations must not."""
    zones = Zones.at(tmp_path)
    first = observations(["2026-07-11 14:00", "2026-07-11 14:10"], run_id=RUN_A)
    second = observations(
        ["2026-07-11 14:10", "2026-07-11 14:20"], values=[99.0, 18.0], run_id=RUN_B
    )
    write_observations(first, zones, source="project", run_id=RUN_A)
    write_observations(second, zones, source="project", run_id=RUN_B)

    stored = storage.read_observations(zones)
    assert len(stored) == 3
    overlap = stored.loc[stored["timestamp"] == pd.Timestamp("2026-07-11 14:10")]
    assert len(overlap) == 1
    assert overlap["value"].iloc[0] == 99.0  # the newer run wins
    assert overlap["fetch_run_id"].iloc[0] == RUN_B


def test_the_superseded_partition_file_is_removed(tmp_path):
    zones = Zones.at(tmp_path)
    write_observations(observations(["2026-07-11 14:00"]), zones, source="project", run_id=RUN_A)
    write_observations(
        observations(["2026-07-11 14:10"], run_id=RUN_B), zones, source="project", run_id=RUN_B
    )
    parts = sorted(zones.partition("project", 2026).glob("part-*.parquet"))
    assert [p.name for p in parts] == [f"part-{RUN_B}.parquet"]


def test_reingesting_the_same_run_is_idempotent(tmp_path):
    zones = Zones.at(tmp_path)
    frame = observations(["2026-07-11 14:00", "2026-07-11 14:10"])
    write_observations(frame, zones, source="project", run_id=RUN_A)
    write_observations(frame, zones, source="project", run_id=RUN_A)
    assert len(storage.read_observations(zones)) == 2


def test_a_null_depth_still_dedupes(tmp_path):
    """22506632's depth is null; a null key part must not defeat the dedupe."""
    zones = Zones.at(tmp_path)
    frame = observations(["2026-07-11 14:00"], depth_m=None)
    write_observations(frame, zones, source="project", run_id=RUN_A)
    write_observations(frame, zones, source="project", run_id=RUN_B)
    stored = storage.read_observations(zones)
    assert len(stored) == 1
    assert pd.isna(stored["depth_m"].iloc[0])


def test_different_depths_at_one_instant_are_distinct_observations(tmp_path):
    zones = Zones.at(tmp_path)
    write_observations(
        observations(["2026-07-11 14:00"], depth_m=2.0), zones, source="project", run_id=RUN_A
    )
    write_observations(
        observations(["2026-07-11 14:00"], depth_m=8.0), zones, source="project", run_id=RUN_B
    )
    assert len(storage.read_observations(zones)) == 2


def test_a_string_depth_cannot_leave_two_rows_where_one_belongs(tmp_path):
    """The silent half of #57, asserted on the file rather than through the reader.

    `_dedupe` runs before the writer's `astype`, so `"8.23"` and `8.23` were two
    keys: re-ingesting two already-stored readings left four rows in the part file
    where two belong. Nothing read them back doubled -- the writer normalises the
    depth before the file is written and the reader dedupes again on the way out --
    so the raw Parquet is the one place the duplication was ever visible, and the
    case docs/03 "Partition files and idempotence" flags for a glob reader.
    """
    zones = Zones.at(tmp_path)
    stamps = ["2026-07-11 14:00", "2026-07-11 14:10"]
    write_observations(observations(stamps, depth_m=8.23), zones, source="project", run_id=RUN_A)

    with pytest.raises(ValueError, match="depth_m"):
        write_observations(
            observations(stamps, depth_m="8.23", run_id=RUN_B),
            zones,
            source="project",
            run_id=RUN_B,
        )

    files = sorted(zones.partition("project", 2026).glob("part-*.parquet"))
    assert [path.name for path in files] == [f"part-{RUN_A}.parquet"]
    assert len(pd.read_parquet(files[0])) == 2


def test_sources_are_stored_separately(tmp_path):
    zones = Zones.at(tmp_path)
    project = observations(["2026-07-11 14:00"])
    ndbc = observations(["2026-07-11 14:00"], site="NDBC:LJAC1")
    ndbc["source"] = "ndbc"
    write_observations(project, zones, source="project", run_id=RUN_A)
    write_observations(ndbc, zones, source="ndbc", run_id=RUN_A)

    assert len(storage.read_observations(zones)) == 2
    assert len(storage.read_observations(zones, source="project")) == 1
    assert storage.read_observations(zones, source="ndbc")["site_id"].iloc[0] == "NDBC:LJAC1"


def test_a_partition_left_holding_two_files_reads_as_one_series(tmp_path):
    """The write that leaves one file per partition is not atomic (issue #3).

    `_write_partition` creates the new file and only then removes the ones it
    supersedes, so an interrupted run leaves both. The two overlap completely --
    the newer is a rewrite of the older -- so a naive read returns every row
    twice, and the QARTOD tests read a row's neighbours.
    """
    zones = Zones.at(tmp_path)
    stamps = ["2026-07-11T15:00Z", "2026-07-11T15:10Z"]

    (superseded,) = write_observations(
        observations(stamps, values=[17.0, 17.1], run_id=RUN_A),
        zones,
        source="project",
        run_id=RUN_A,
    )
    leftover = superseded.read_bytes()

    (current,) = write_observations(
        observations(stamps, values=[18.0, 18.1], run_id=RUN_B),
        zones,
        source="project",
        run_id=RUN_B,
    )
    assert not superseded.exists()

    superseded.write_bytes(leftover)  # the unlink that did not happen
    assert len(sorted(current.parent.glob("part-*.parquet"))) == 2

    rows = storage.read_observations(zones, "project")
    assert len(rows) == 2
    assert list(rows["value"]) == [18.0, 18.1]


def test_a_leftover_file_in_one_partition_does_not_touch_another(tmp_path):
    """Dedupe is per partition, not zone-wide: `OBSERVATION_KEY` has no `source`."""
    zones = Zones.at(tmp_path)
    stamps = ["2026-07-11T15:00Z"]
    write_observations(observations(stamps), zones, source="project", run_id=RUN_A)
    write_observations(observations(stamps, site="NDBC:LJAC1"), zones, source="ndbc", run_id=RUN_A)

    rows = storage.read_observations(zones)
    assert sorted(rows["site_id"]) == ["NDBC:LJAC1", "PROJ:TIDBIT-1"]


def test_a_rewrite_that_preserves_fetch_run_id_still_wins(tmp_path):
    """What the qc rewrite depends on, named directly.

    qc rewrites the zone preserving each row's `fetch_run_id` (docs/03), so both
    copies of a rewritten row carry the same one and "newest `fetch_run_id`
    wins" cannot break the tie. What resolves it is that the incoming rows are
    concatenated last and `_dedupe` sorts stably. Reorder either and qc silently
    writes back the flags it just replaced -- so this pins the ordering rather
    than leaving it to the end-to-end qc tests to notice indirectly.
    """
    zones = Zones.at(tmp_path)
    stamps = ["2026-07-11T15:00Z"]
    write_observations(
        observations(stamps, values=[17.0], run_id=RUN_A), zones, source="project", run_id=RUN_A
    )
    write_observations(
        observations(stamps, values=[99.0], run_id=RUN_A), zones, source="project", run_id=RUN_B
    )

    assert list(storage.read_observations(zones, "project")["value"]) == [99.0]


def test_an_interrupted_write_leaves_the_previous_partition_intact(tmp_path, monkeypatch):
    """The new file lands under a staging name, so a half-written Parquet never
    becomes the partition (issue #3). Before this, a failed write left a
    truncated `part-*.parquet` that every later read raised on."""
    zones = Zones.at(tmp_path)
    stamps = ["2026-07-11T15:00Z"]
    write_observations(observations(stamps, values=[17.0]), zones, source="project", run_id=RUN_A)

    def die_mid_write(self, path, **kwargs):
        Path(path).write_bytes(b"half a parquet file")
        raise OSError("no space left on device")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", die_mid_write)
    with pytest.raises(OSError):
        write_observations(
            observations(stamps, values=[99.0]), zones, source="project", run_id=RUN_B
        )
    monkeypatch.undo()

    directory = zones.partition("project", 2026)
    assert [p.name for p in sorted(directory.glob("part-*.parquet"))] == [f"part-{RUN_A}.parquet"]
    assert list(storage.read_observations(zones, "project")["value"]) == [17.0]


def test_reading_an_empty_zone_returns_the_documented_columns(tmp_path):
    empty = storage.read_observations(Zones.at(tmp_path))
    assert empty.empty
    assert tuple(empty.columns) == OBSERVATION_COLUMNS


def test_an_empty_read_has_the_dtypes_of_a_non_empty_one(tmp_path):
    """Same columns is not enough: a caller reaching for `.dt` needs the dtype.

    An empty zone used to come back all `object`, so the shape of the answer
    depended on whether anything had been ingested yet.
    """
    zones = Zones.at(tmp_path)
    before = storage.read_observations(zones)
    write_observations(observations(["2026-07-11 14:00"]), zones, source="project", run_id=RUN_A)
    after = storage.read_observations(zones)

    assert before.dtypes.astype(str).to_dict() == after.dtypes.astype(str).to_dict()
    assert before["timestamp"].dtype.kind == "M"


def test_a_frame_read_back_out_of_the_zone_still_passes_the_guard(tmp_path):
    """The round trip is closed: what storage hands back, storage would accept.

    The stored timestamp is naive by design (the DuckDB reason in the storage
    module docstring), so this localizes it exactly as the `qc` and `features`
    commands do after a read. Both then pass the frame to `validate_frame`, so a
    `string` column or a `float64` depth coming back as something the dtype
    checks refuse would take those two commands down with it.
    """
    zones = Zones.at(tmp_path)
    write_observations(
        observations(["2026-07-11 14:00"], depth_m=8.23), zones, source="project", run_id=RUN_A
    )

    stored = storage.read_observations(zones)
    stored["timestamp"] = stored["timestamp"].dt.tz_localize("UTC")
    validate_frame(stored)


# --------------------------------------------------------------------------
# The features zone (docs/03)
# --------------------------------------------------------------------------

FEATURE_KEY = ("source", "site_id", "year", "quarter")


def features(rows) -> pd.DataFrame:
    """A minimal feature table: `(source, site_id, year, quarter, mean)` tuples."""
    return pd.DataFrame(
        [
            {"source": s, "site_id": site, "year": y, "quarter": q, "mean": mean}
            for s, site, y, q, mean in rows
        ],
        columns=["source", "site_id", "year", "quarter", "mean"],
    ).astype({"source": "string", "site_id": "string", "year": "int32", "quarter": "int8"})


def write(frame, zones, *, replacing, table="quarterly_env"):
    return storage.write_features(frame, zones, table=table, key=FEATURE_KEY, replacing=replacing)


def test_a_feature_table_is_one_file_in_the_features_zone(tmp_path):
    zones = Zones.at(tmp_path)
    path = write(features([("ndbc", "NDBC:LJAC1", 2007, 1, 15.0)]), zones, replacing=("ndbc",))
    assert path == tmp_path / "features" / "quarterly_env.parquet"
    assert path.exists()


def test_a_written_table_reads_back_with_its_dtypes(tmp_path):
    zones = Zones.at(tmp_path)
    frame = features([("ndbc", "NDBC:LJAC1", 2007, 1, 15.0)])
    write(frame, zones, replacing=("ndbc",))
    stored = storage.read_features(zones, "quarterly_env")
    assert stored.dtypes.to_dict() == frame.dtypes.to_dict()
    assert stored["mean"].tolist() == [15.0]


def test_reading_a_table_that_was_never_built_returns_an_empty_frame(tmp_path):
    assert storage.read_features(Zones.at(tmp_path), "climatology_env").empty


def test_a_source_restricted_write_leaves_every_other_sources_rows_intact(tmp_path):
    """A targeted rerun must not be silent data loss for the sources it skipped."""
    zones = Zones.at(tmp_path)
    write(
        features(
            [("ndbc", "NDBC:LJAC1", 2007, 1, 15.0), ("project", "PROJ:TIDBIT-1", 2026, 3, 21.0)]
        ),
        zones,
        replacing=("ndbc", "project"),
    )

    write(features([("ndbc", "NDBC:LJAC1", 2007, 1, 99.0)]), zones, replacing=("ndbc",))
    stored = storage.read_features(zones, "quarterly_env")
    assert stored.set_index("source")["mean"].to_dict() == {"ndbc": 99.0, "project": 21.0}


def test_rebuilding_a_source_retires_the_rows_it_no_longer_produces(tmp_path):
    """A site removed from the registry must not keep its feature rows forever."""
    zones = Zones.at(tmp_path)
    write(
        features([("ndbc", "NDBC:LJAC1", 2007, 1, 15.0), ("ndbc", "NDBC:GONE", 2007, 1, 9.0)]),
        zones,
        replacing=("ndbc",),
    )

    write(features([("ndbc", "NDBC:LJAC1", 2007, 1, 15.0)]), zones, replacing=("ndbc",))
    assert storage.read_features(zones, "quarterly_env")["site_id"].tolist() == ["NDBC:LJAC1"]


def test_the_table_is_sorted_by_its_key_whatever_order_the_rows_arrive_in(tmp_path):
    zones = Zones.at(tmp_path)
    write(
        features([("ndbc", "NDBC:LJAC1", 2008, 1, 16.0), ("ndbc", "NDBC:LJAC1", 2007, 3, 22.0)]),
        zones,
        replacing=("ndbc",),
    )
    write(features([("coops", "COOPS:9410230", 2007, 1, 15.0)]), zones, replacing=("coops",))

    stored = storage.read_features(zones, "quarterly_env")
    assert stored["source"].tolist() == ["coops", "ndbc", "ndbc"]
    assert stored["year"].tolist() == [2007, 2007, 2008]


def test_an_empty_result_writes_an_empty_table_rather_than_no_table(tmp_path):
    """A source that produced nothing is a fact, and a missing file is not one."""
    zones = Zones.at(tmp_path)
    path = write(features([]), zones, replacing=("ndbc",))
    assert path.exists()
    assert storage.read_features(zones, "quarterly_env").empty


def test_retained_rows_follow_the_current_configurations_columns(tmp_path):
    """A retuned threshold renames its column; the source not yet rebuilt shows
    null there rather than the old column lingering beside the new one."""
    zones = Zones.at(tmp_path)
    old = features([("project", "PROJ:TIDBIT-1", 2026, 3, 21.0)])
    old["days_above_20c"] = 4.0
    write(old, zones, replacing=("project",))

    new = features([("ndbc", "NDBC:LJAC1", 2007, 1, 15.0)])
    new["days_above_21c"] = 0.0
    write(new, zones, replacing=("ndbc",))

    stored = storage.read_features(zones, "quarterly_env")
    retuned = stored.set_index("source")["days_above_21c"]
    assert "days_above_20c" not in stored.columns
    assert retuned["ndbc"] == 0.0
    assert pd.isna(retuned["project"])


def test_a_table_name_the_data_model_does_not_define_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not a docs/03 features table"):
        storage.read_features(Zones.at(tmp_path), "quarterly_vibes")


def test_an_interrupted_feature_write_leaves_the_previous_table_intact(tmp_path, monkeypatch):
    zones = Zones.at(tmp_path)
    write(features([("ndbc", "NDBC:LJAC1", 2007, 1, 15.0)]), zones, replacing=("ndbc",))

    def die_mid_write(self, path, **kwargs):
        Path(path).write_bytes(b"half a parquet file")
        raise OSError("no space left on device")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", die_mid_write)
    with pytest.raises(OSError):
        write(features([("ndbc", "NDBC:LJAC1", 2007, 1, 99.0)]), zones, replacing=("ndbc",))
    monkeypatch.undo()

    assert storage.read_features(zones, "quarterly_env")["mean"].tolist() == [15.0]


def test_a_leftover_staging_file_is_not_the_table(tmp_path):
    """So a DuckDB query written against the zone cannot pick one up."""
    zones = Zones.at(tmp_path)
    write(features([("ndbc", "NDBC:LJAC1", 2007, 1, 15.0)]), zones, replacing=("ndbc",))
    assert [p.name for p in sorted(zones.features.glob("*.parquet"))] == ["quarterly_env.parquet"]
