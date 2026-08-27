"""`kelpcompare features`, end to end (docs/04 s2-s3, docs/03).

Every test runs the real commands against a `tmp_path` data root -- ingest,
then qc, then features -- so what is asserted is what an operator would get from
a real vendor file or a recorded NDBC payload. Nothing here touches the repo's
own `data/` beyond copying the committed registry, and nothing reaches the
network (CLAUDE.md).

Two fixtures, on purpose. The reviewed TidbiT deployment is three weeks long and
lands in the quarter this project is currently in, which is the honest picture of
what the feature table holds today: one under-covered, unusable quarter. The
recorded LJAC1 archive lands in a quarter that finished in 2023, so the
completeness and climatology behaviour can be asserted without the calendar
moving underneath the test.
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.fetchers import ndbc
from kelpcompare.fetchers.base import new_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
ORIGINAL = FIX / "Tidbit_1__22506632__2026-08-01_07_44_27_PDT__Data_PDT_.xlsx"
ARCHIVE = FIX / "ndbc" / "ljac1h2023_excerpt.txt"
REALTIME = FIX / "ndbc" / "LJAC1_realtime_excerpt.txt"
REGISTRY_SOURCE = REPO_ROOT / "data" / "registry"

REGISTRY_FILES = ("sites.json", "parameters.json", "features.json")


@pytest.fixture
def data_root(tmp_path) -> Path:
    """A docs/03 data root with the committed registry and an empty incoming/."""
    root = tmp_path / "data"
    (root / "registry").mkdir(parents=True)
    for name in REGISTRY_FILES:
        shutil.copy2(REGISTRY_SOURCE / name, root / "registry" / name)
    (root / "raw" / "project_sensors" / "incoming").mkdir(parents=True)
    return root


@pytest.fixture
def offline(monkeypatch):
    """Serve the recorded NDBC payloads in place of the network."""

    def realtime(station, *, session=None, validators=None):
        return new_payload(
            "ndbc",
            station.upper(),
            f"{station.upper()}.txt",
            "file://realtime",
            REALTIME.read_bytes(),
        )

    def archive(station, year, *, session=None, validators=None):
        return new_payload(
            "ndbc",
            station.upper(),
            f"{station.lower()}h{year}.txt.gz",
            f"file://{year}",
            gzip.compress(ARCHIVE.read_bytes(), mtime=0),
        )

    monkeypatch.setattr(ndbc, "fetch_realtime", realtime)
    monkeypatch.setattr(ndbc, "fetch_archive", archive)


def run(data_root: Path, command: str, *extra: str, expect: int = 0):
    result = CliRunner().invoke(main, [command, "--data-root", str(data_root), *extra])
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    assert result.exit_code == expect, f"{command} exited {result.exit_code}: {result.output}"
    return result


def hobo(data_root: Path):
    """Ingest and flag the reviewed deployment: three weeks in 2026 Q3."""
    shutil.copy2(ORIGINAL, data_root / "raw" / "project_sensors" / "incoming" / ORIGINAL.name)
    run(data_root, "ingest", "--source", "project")
    run(data_root, "qc")


def ljac1(data_root: Path, year: str = "2023"):
    """Ingest and flag one recorded NDBC archive year."""
    run(data_root, "ingest", "--source", "ndbc", "--year", year)
    run(data_root, "qc")


def quarterly(data_root: Path) -> pd.DataFrame:
    return pd.read_parquet(data_root / "features" / "quarterly_env.parquet")


def climatology(data_root: Path) -> pd.DataFrame:
    return pd.read_parquet(data_root / "features" / "climatology_env.parquet")


def manifests(data_root: Path, command: str) -> list[dict]:
    files = sorted((data_root / "raw" / "_manifests").glob("*.json"))
    payloads = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    return [m for m in payloads if m["command"] == command]


def features_manifest(data_root: Path) -> dict:
    runs = manifests(data_root, "features")
    assert len(runs) == 1, f"expected one features manifest, found {len(runs)}"
    return runs[0]


def series_row(frame: pd.DataFrame, parameter: str) -> pd.Series:
    match = frame.loc[frame["parameter"] == parameter]
    assert len(match) == 1, f"expected one {parameter} row, got {len(match)}"
    return match.iloc[0]


def edit_features_registry(data_root: Path, mutate) -> None:
    path = data_root / "registry" / "features.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------
# The reviewed deployment, start to finish
# --------------------------------------------------------------------------


def test_the_reference_deployment_becomes_one_quarter(data_root):
    """Three weeks of 10-minute readings out of a 92-day quarter: the row exists,
    carries its features, and says it is not usable."""
    hobo(data_root)
    run(data_root, "features")

    (row,) = quarterly(data_root).to_dict("records")
    assert (row["source"], row["site_id"]) == ("project", "PROJ:YELLOW-BUOY")
    assert (row["parameter"], row["year"], row["quarter"]) == ("sea_water_temperature", 2026, 3)
    assert row["feature_set"] == "temperature"
    assert row["n_obs"] == 3022  # the rows qc left at flag <= 2
    assert row["cadence_s"] == 600.0
    assert row["expected_obs"] == 92 * 24 * 6
    assert row["pct_coverage"] == pytest.approx(3022 / (92 * 24 * 6))
    assert not row["usable"]
    assert row["qc_max_flag"] == 2
    assert round(row["min"], 2) == 17.76


def test_the_quarterly_minimum_is_the_coldest_reading_qc_kept(data_root):
    """The install transient at 14.78 degC failed QC; the nitrate proxy must not
    see it, and must see the coldest in-water reading instead."""
    hobo(data_root)
    run(data_root, "features")
    assert round(series_row(quarterly(data_root), "sea_water_temperature")["min"], 2) == 17.76


def test_every_anomaly_is_null_on_todays_data(data_root):
    """Disclosed rather than discovered: no series in this project yet spans the
    2007-2019 baseline, so the columns docs/03 promises ship empty and fill in
    when real multi-decade history is ingested."""
    hobo(data_root)
    run(data_root, "features")

    built = quarterly(data_root)
    anomalies = [name for name in built.columns if name.endswith("_anom")]
    assert anomalies
    assert built[anomalies].isna().all().all()
    assert built["baseline_years"].tolist() == [0]
    assert climatology(data_root).empty


# --------------------------------------------------------------------------
# A quarter that finished, from the recorded NDBC archive
# --------------------------------------------------------------------------


def test_a_quarter_that_ended_years_ago_is_marked_complete(data_root, offline):
    hobo(data_root)
    ljac1(data_root)
    run(data_root, "features", "--source", "ndbc", expect=0)

    built = quarterly(data_root)
    ndbc_rows = built.loc[built["source"] == "ndbc"]
    assert set(ndbc_rows["year"]) == {2023}
    assert ndbc_rows["quarter_complete"].all()


def test_a_station_with_no_wave_sensor_produces_no_wave_feature_rows(data_root, offline):
    """`sites.json` says LJAC1 has no wave sensor, so nothing reaches this stage
    to be aggregated — no quarterly rows at zero coverage for a reader to learn
    to ignore (https://github.com/cweber12/kelp-compare/issues/21)."""
    ljac1(data_root)
    run(data_root, "features", expect=0)

    built = quarterly(data_root)
    assert set(built["parameter"]) == {
        "sea_water_temperature",
        "air_temperature",
        "wind_speed",
    }


def test_a_declared_sensor_that_reported_sentinel_keeps_its_row(data_root, offline):
    """The other half of the same distinction. Air temperature *is* declared, and
    part of the 2023 excerpt is sentinel; those rows land flagged missing, drop
    out of `n_obs`, and the quarter still exists to say so."""
    ljac1(data_root)
    run(data_root, "features", expect=0)

    air = series_row(quarterly(data_root), "air_temperature")
    assert air["feature_set"] == "statistics"
    assert 0 < air["n_obs"] < 400  # 400 rows landed; the sentinel ones are not observations
    assert air["pct_coverage"] == pytest.approx(air["n_obs"] / air["expected_obs"])
    assert not air["usable"]


def test_air_temperature_gets_the_statistics_set_not_the_kelp_thresholds(data_root, offline):
    """Same unit as sea water; the feature set is declared, never inferred."""
    ljac1(data_root)
    run(data_root, "features", expect=0)

    air = series_row(quarterly(data_root), "air_temperature")
    assert air["feature_set"] == "statistics"
    assert pd.isna(air["days_above_20c"])
    assert not pd.isna(air["mean"])


def test_the_registry_depth_reaches_the_feature_row(data_root, offline):
    ljac1(data_root)
    run(data_root, "features", expect=0)

    water = series_row(quarterly(data_root), "sea_water_temperature")
    assert water["depth_m"] == 3.4
    assert pd.isna(series_row(quarterly(data_root), "air_temperature")["depth_m"])


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------


def test_a_run_defaults_to_every_source_with_stored_rows(data_root, offline):
    hobo(data_root)
    ljac1(data_root)
    run(data_root, "features")
    assert set(quarterly(data_root)["source"]) == {"ndbc", "project"}


def test_a_source_restricted_run_leaves_another_sources_rows_untouched(data_root, offline):
    hobo(data_root)
    ljac1(data_root)
    run(data_root, "features")
    before = quarterly(data_root)

    run(data_root, "features", "--source", "project")
    after = quarterly(data_root)
    assert set(after["source"]) == {"ndbc", "project"}
    assert after.loc[after["source"] == "ndbc"].equals(before.loc[before["source"] == "ndbc"])


def test_a_dry_run_writes_nothing_at_all(data_root):
    hobo(data_root)
    result = run(data_root, "features", "--dry-run")
    assert "dry run" in result.output
    assert not (data_root / "features").exists()
    assert not manifests(data_root, "features")


def test_a_dry_run_still_reports_what_it_would_build(data_root):
    hobo(data_root)
    assert "1 quarter, 0 usable" in run(data_root, "features", "--dry-run").output


def test_a_pass_only_run_differs_from_the_default_and_records_that_it_did(data_root):
    """docs/04 s1 requires key results to be rerunnable at pass-only."""
    hobo(data_root)
    run(data_root, "features")
    default = series_row(quarterly(data_root), "sea_water_temperature")

    run(data_root, "features", "--qc-max-flag", "1")
    strict = series_row(quarterly(data_root), "sea_water_temperature")
    assert (default["qc_max_flag"], strict["qc_max_flag"]) == (2, 1)
    assert strict["n_obs"] == default["n_obs"]  # every kept row already passed
    assert manifests(data_root, "features")[-1]["argv"] == ["--qc-max-flag=1"]


def test_a_zone_with_nothing_in_it_is_not_an_error(data_root):
    result = run(data_root, "features")
    assert "nothing to build" in result.output
    assert not (data_root / "raw" / "_manifests").exists()


def test_naming_a_source_with_no_stored_rows_is_not_an_error(data_root):
    hobo(data_root)
    result = run(data_root, "features", "--source", "ndbc")
    assert "nothing to build" in result.output
    assert not (data_root / "features").exists()


# --------------------------------------------------------------------------
# The manifest -- hard rule 7, docs/03
# --------------------------------------------------------------------------


def test_the_run_manifest_records_the_code_version_and_the_sources(data_root):
    hobo(data_root)
    run(data_root, "features")

    payload = features_manifest(data_root)
    assert payload["command"] == "features"
    assert payload["sources"] == ["project"]
    assert payload["code_sha"] is None or len(payload["code_sha"]) == 40


def test_the_run_manifest_records_each_series_quarter_counts(data_root):
    hobo(data_root)
    run(data_root, "features")

    (series,) = features_manifest(data_root)["series"]
    assert series["site_id"] == "PROJ:YELLOW-BUOY"
    assert series["parameter"] == "sea_water_temperature"
    assert series["rows"] == 3029
    assert (series["quarters"], series["quarters_usable"]) == (1, 0)
    assert (series["first_quarter"], series["last_quarter"]) == ("2026Q3", "2026Q3")


def test_the_manifest_path_is_echoed_along_with_both_tables(data_root):
    hobo(data_root)
    output = run(data_root, "features").output
    assert "quarterly_env.parquet" in output
    assert "climatology_env.parquet" in output
    assert "manifest:" in output


# --------------------------------------------------------------------------
# Fail-soft, and saying so
# --------------------------------------------------------------------------


def test_a_parameter_with_no_configuration_is_named_and_sets_the_exit_code(data_root, offline):
    """Silence about an unbuilt parameter is impossible."""
    ljac1(data_root)
    edit_features_registry(data_root, lambda p: p["parameters"].pop("wind_speed"))

    result = run(data_root, "features", expect=1)
    assert "wind_speed" in result.output
    assert "wind_speed" not in set(quarterly(data_root)["parameter"])
    assert any("wind_speed" in w for w in features_manifest(data_root)["warnings"])


def test_a_source_that_cannot_be_read_does_not_cost_the_run_the_ones_that_can(data_root):
    """docs/02 fail-soft, and hard rule 7: recorded, stepped over, manifest either
    way. `ndbc` sorts before `project`, so a read failure that escaped would abort
    the run before the source that was fine had been built at all."""
    hobo(data_root)
    unreadable = data_root / "observations" / "source=ndbc" / "year=2026"
    unreadable.mkdir(parents=True)
    (unreadable / "part-20260101T000000000Z-ingest.parquet").write_text("not a parquet file")

    result = run(data_root, "features", expect=1)
    assert "ndbc" in result.output
    assert set(quarterly(data_root)["source"]) == {"project"}
    assert any("ndbc" in warning for warning in features_manifest(data_root)["warnings"])


def test_a_failed_source_keeps_the_rows_a_previous_run_built_for_it(data_root, offline):
    hobo(data_root)
    ljac1(data_root)
    run(data_root, "features")
    before = quarterly(data_root)

    partition = next((data_root / "observations" / "source=ndbc").rglob("part-*.parquet"))
    partition.write_text("not a parquet file")
    run(data_root, "features", expect=1)

    after = quarterly(data_root)
    assert after.loc[after["source"] == "ndbc"].equals(before.loc[before["source"] == "ndbc"])


def test_a_configuration_that_cannot_be_parsed_stops_the_run_rather_than_half_building(data_root):
    """Not fail-soft: there is no per-source failure to isolate when nothing can
    be built against the configuration at all."""
    hobo(data_root)
    edit_features_registry(data_root, lambda p: p["parameters"].update({"x": {"feature_set": "q"}}))

    result = CliRunner().invoke(main, ["features", "--data-root", str(data_root)])
    assert result.exit_code != 0
    assert not (data_root / "features").exists()


# --------------------------------------------------------------------------
# Reproducibility -- docs/03 integrity rules
# --------------------------------------------------------------------------


def test_two_runs_over_unchanged_inputs_write_the_same_bytes(data_root, offline):
    """What makes `rebuild` meaningful: the tables are a pure function of the
    observations zone and the configuration."""
    hobo(data_root)
    ljac1(data_root)
    run(data_root, "features")
    first = (data_root / "features" / "quarterly_env.parquet").read_bytes()

    run(data_root, "features")
    assert (data_root / "features" / "quarterly_env.parquet").read_bytes() == first


def test_the_feature_row_key_matches_the_qc_series_key(data_root, offline):
    """ "Every feature row traces to one QC series" has to be checkable."""
    ljac1(data_root)
    run(data_root, "features", expect=0)

    qc_series = {
        (s["site_id"], s["parameter"], s["depth_m"])
        for s in manifests(data_root, "qc")[0]["series"]
    }
    built = quarterly(data_root)
    feature_series = {
        (row["site_id"], row["parameter"], None if pd.isna(row["depth_m"]) else row["depth_m"])
        for _, row in built.iterrows()
    }
    assert feature_series <= qc_series
