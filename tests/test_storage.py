"""Zone layout, the schema guard, and dedupe on write (docs/03).

Every test writes under `tmp_path`. Nothing here may touch the repo's own
`data/` root -- raw is append-only forever (CLAUDE.md hard rule 1), and a test
that wrote there would be unrepeatable by construction.
"""

from __future__ import annotations

import pandas as pd
import pytest

from kelpcompare import storage
from kelpcompare.storage import OBSERVATION_COLUMNS, Zones, write_observations

RUN_A = "20260824T120000000Z-ingest"
RUN_B = "20260824T130000000Z-ingest"


def observations(timestamps, *, values=None, run_id=RUN_A, depth_m=None, site="PROJ:YELLOW-BUOY"):
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


def test_reading_an_empty_zone_returns_the_documented_columns(tmp_path):
    empty = storage.read_observations(Zones.at(tmp_path))
    assert empty.empty
    assert tuple(empty.columns) == OBSERVATION_COLUMNS
