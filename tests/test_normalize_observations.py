"""The UTC + SI + controlled-name boundary (docs/03, docs/06 s3-s4).

The acceptance case is the reference deployment: docs/06 pins its numbers in
Fahrenheit, and this module is what turns them into the stored Celsius record.
Everything else here is a synthetic file exercising a case the two reference
binaries cannot show -- a second unmapped series, a degC export, a missing value.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.adapters import hobo_xlsx
from kelpcompare.adapters.base import RawSeries, SeriesInfo
from kelpcompare.cli import SOURCE_NAMES
from kelpcompare.normalize import NormalizedBatch, convert_unit, to_observations
from kelpcompare.parameters import load_parameters
from kelpcompare.registry import Deployment, find_deployment, load_registry
from kelpcompare.storage import OBSERVATION_COLUMNS, validate_frame

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
ORIGINAL = FIX / "Tidbit_1__22506632__2026-08-01_07_44_27_PDT__Data_PDT_.xlsx"
EDITED = FIX / "yellow_buoy_temps.xlsx"
REGISTRY_PATH = REPO_ROOT / "data" / "registry" / "sites.json"
PARAMETERS_PATH = REPO_ROOT / "data" / "registry" / "parameters.json"

KNOWN_SERIAL = "22506632"
RUN_ID = "20260824T120000000Z-ingest"

#: The reviewed deployment sits entirely in PDT, so its fixed offset is UTC-7.
PDT = timedelta(hours=-7)


@pytest.fixture(scope="module")
def parameters():
    return load_parameters(PARAMETERS_PATH, sources=SOURCE_NAMES)


@pytest.fixture(scope="module")
def deployment():
    return find_deployment(load_registry(REGISTRY_PATH), KNOWN_SERIAL)


@pytest.fixture(scope="module")
def normalized(parameters, deployment) -> NormalizedBatch:
    return to_observations(
        hobo_xlsx.parse(ORIGINAL),
        deployment,
        parameters,
        source="project",
        run_id=RUN_ID,
    )


def naive(*parts: int) -> datetime:
    return datetime(*parts)  # noqa: DTZ001 -- a HOBO file's local time, naive by design


def fahrenheit_to_celsius(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


# --------------------------------------------------------------------------
# The acceptance case: the reference deployment, end to end
# --------------------------------------------------------------------------


def test_every_parsed_row_is_written(normalized):
    """docs/06 s3: the window excludes readings from analysis, not from the record."""
    assert len(normalized.frame) == 3029
    assert tuple(normalized.frame.columns) == OBSERVATION_COLUMNS
    assert normalized.warnings == ()
    assert normalized.skipped_series == ()


def test_the_window_is_a_flag_not_a_deletion(normalized):
    """6 installation readings and 1 retrieval reading, flagged and kept."""
    assert normalized.flag_counts == {"2": 3022, "4": 7}

    excluded = normalized.frame.loc[normalized.frame["qc_flag"] == 4]
    assert set(excluded["qc_tests"]) == {"deployment_window:fail"}
    # The install transient docs/06 s2 describes: sensor in air during setup.
    assert round(excluded["value"].min(), 2) == round(fahrenheit_to_celsius(58.60), 2)


def test_the_analysis_filter_reproduces_the_hand_edited_file(normalized):
    """docs/06 s3: the registry window, applied, equals the hand edit exactly."""
    usable = normalized.frame.loc[normalized.frame["qc_flag"] <= 2]
    assert len(usable) == 3022
    assert round(usable["value"].min(), 2) == round(fahrenheit_to_celsius(63.96), 2)
    assert round(usable["value"].min(), 2) == 17.76

    edited = pd.read_excel(EDITED, sheet_name="Data").dropna(subset=["Date-Time (PDT)"])
    assert len(usable) == len(edited)


def test_values_are_converted_to_celsius(normalized):
    """The unit is read from the header, never assumed (docs/06 s6)."""
    assert round(normalized.frame["value"].min(), 2) == round(fahrenheit_to_celsius(58.60), 2)
    assert round(normalized.frame["value"].max(), 2) == round(fahrenheit_to_celsius(75.35), 2)
    assert round(normalized.frame["value"].mean(), 2) == 21.58  # 70.84 degF


def test_timestamps_become_utc_aware(normalized):
    """First sample 2026-07-11 07:00 PDT is 14:00 UTC."""
    assert str(normalized.frame["timestamp"].dtype.tz) == "UTC"
    assert normalized.utc_offset == PDT
    assert normalized.frame["timestamp"].min() == pd.Timestamp("2026-07-11 14:00", tz="UTC")
    assert normalized.frame["timestamp"].max() == pd.Timestamp("2026-08-01 14:40", tz="UTC")


def test_the_window_is_carried_into_utc_too(normalized):
    assert normalized.window_utc == (
        pd.Timestamp("2026-07-11 15:00", tz="UTC"),
        pd.Timestamp("2026-08-01 14:30", tz="UTC"),
    )


def test_registry_metadata_lands_on_every_row(normalized, deployment):
    frame = normalized.frame
    assert set(frame["site_id"]) == {deployment.site_id}
    assert set(frame["parameter"]) == {"sea_water_temperature"}
    assert set(frame["source"]) == {"project"}
    assert set(frame["fetch_run_id"]) == {RUN_ID}

    # Whatever the deployment record declares: null while a site is unplaced, the
    # surveyed depth once it is. What is asserted is that the value comes from the
    # registry, not that it is any particular number.
    depths = {None if pd.isna(value) else value for value in frame["depth_m"]}
    assert depths == {deployment.depth_m}


def test_an_edited_file_normalizes_to_the_same_in_window_rows(parameters, deployment):
    """Accepted when it is all we have (docs/06 s3) -- and it agrees with the original."""
    batch = to_observations(
        hobo_xlsx.parse(EDITED), deployment, parameters, source="project", run_id=RUN_ID
    )
    usable = batch.frame.loc[batch.frame["qc_flag"] <= 2]
    assert len(usable) == 3022
    assert round(usable["value"].min(), 2) == 17.76


# --------------------------------------------------------------------------
# Unit conversion -- the hard-rule-2 boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["°F", "degF", "deg F", "F", "fahrenheit"])
def test_fahrenheit_spellings_all_convert(spelling):
    converted = convert_unit(pd.Series([32.0, 212.0]), spelling, "degC")
    assert converted.tolist() == [0.0, 100.0]


@pytest.mark.parametrize("spelling", ["°C", "degC", "C", "celsius"])
def test_celsius_passes_through_unchanged(spelling):
    assert convert_unit(pd.Series([14.0]), spelling, "degC").tolist() == [14.0]


@pytest.mark.parametrize("spelling", ["degree_Celsius", "degrees Celsius"])
def test_the_cf_celsius_spellings_convert(spelling):
    """What a CF-conformant feed declares in its units line.

    CeNCOOS serves the San Diego RTOMS moorings as `degree_Celsius` and the
    City's own CSV export writes `degrees Celsius` (docs/02). Folded here rather
    than in the fetcher, so the next CF source gets it without a second special
    case and the file keeps saying what it says.
    """
    assert convert_unit(pd.Series([14.0]), spelling, "degC").tolist() == [14.0]


def test_an_unknown_unit_refuses_rather_than_guesses():
    """A Fahrenheit value stored as Celsius survives into a publication."""
    with pytest.raises(ValueError, match="cannot convert"):
        convert_unit(pd.Series([1.0]), "kelvin", "degC")


def test_an_unconvertible_pair_refuses():
    with pytest.raises(ValueError, match="no conversion"):
        convert_unit(pd.Series([1.0]), "lux", "degC")


def test_a_series_in_the_wrong_family_refuses(parameters, deployment):
    """A light column mapped at temperature must not be quietly stored."""
    raw = _raw_series(unit="lux", values=[100.0, 220.0])
    with pytest.raises(ValueError, match="cannot convert"):
        to_observations(raw, deployment, parameters, source="project", run_id=RUN_ID)


# --------------------------------------------------------------------------
# Series mapping
# --------------------------------------------------------------------------


def test_an_unmapped_series_is_reported_and_skipped(tmp_path, parameters):
    """One unrecognized column must not cost the run its temperature record."""
    raw = _raw_series(
        name="Tidbit 1",
        unit="degC",
        values=[14.0, 15.0],
        extra=("Light", "lux", [100.0, 220.0]),
    )
    batch = to_observations(
        raw,
        _deployment(series_map={"Tidbit 1": "sea_water_temperature"}),
        parameters,
        source="project",
        run_id=RUN_ID,
    )
    assert batch.skipped_series == ("Light",)
    assert len(batch.warnings) == 1
    assert "no series_map entry" in batch.warnings[0]
    assert set(batch.frame["parameter"]) == {"sea_water_temperature"}
    assert len(batch.frame) == 2


def test_a_batch_with_nothing_mapped_is_still_the_storage_schema(parameters):
    """The empty frame has to be the docs/03 schema, not object columns (#51).

    Reachable here even though the ingest CLI now quarantines a file whose
    series_map names none of its series: the normalizer does not run validation
    and must not depend on someone else having done so.
    """
    batch = to_observations(
        _raw_series(name="Light", unit="degC"),
        _deployment(series_map={"Tidbit 1": "sea_water_temperature"}),
        parameters,
        source="project",
        run_id=RUN_ID,
    )

    assert batch.frame.empty
    assert batch.skipped_series == ("Light",)
    validate_frame(batch.frame)  # would raise on the object-dtype timestamp


def test_a_series_map_naming_an_unknown_parameter_refuses(parameters):
    raw = _raw_series(unit="degC")
    with pytest.raises(ValueError, match="not in"):
        to_observations(
            raw,
            _deployment(series_map={"Tidbit 1": "kelp_vibes"}),
            parameters,
            source="project",
            run_id=RUN_ID,
        )


def test_nothing_mapped_yields_an_empty_frame_with_the_schema(parameters):
    raw = _raw_series(name="Mystery", unit="degC")
    batch = to_observations(
        raw,
        _deployment(series_map={"Tidbit 1": "sea_water_temperature"}),
        parameters,
        source="project",
        run_id=RUN_ID,
    )
    assert batch.frame.empty
    assert tuple(batch.frame.columns) == OBSERVATION_COLUMNS


# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------


def test_a_missing_value_is_flagged_missing_not_evaluated(parameters):
    """docs/03 flag 9: an absent value cannot be evaluated at all."""
    raw = _raw_series(unit="degC", values=[14.0, float("nan"), 15.0])
    batch = to_observations(raw, _deployment(), parameters, source="project", run_id=RUN_ID)
    assert batch.flag_counts == {"2": 2, "9": 1}
    # The window verdict is still recorded for the missing row.
    assert set(batch.frame["qc_tests"]) == {"deployment_window:pass"}


def test_a_deployment_the_gate_should_have_stopped_raises(parameters):
    raw = _raw_series(unit="degC")
    thin = Deployment(site_id="PROJ:X", serial=KNOWN_SERIAL)
    with pytest.raises(ValueError, match="registry gate"):
        to_observations(raw, thin, parameters, source="project", run_id=RUN_ID)


# --------------------------------------------------------------------------
# The parameter vocabulary
# --------------------------------------------------------------------------


def test_the_vocabulary_loads_units_and_qc_ranges(parameters):
    temperature = parameters["sea_water_temperature"]
    assert temperature.unit == "degC"
    assert temperature.valid_range == (5.0, 35.0)
    assert parameters["water_level"].datum == "MLLW"
    assert "sea_water_temperature" in parameters


def test_an_unknown_parameter_names_the_known_ones(parameters):
    with pytest.raises(KeyError, match="sea_water_temperature"):
        parameters["kelp_vibes"]


def test_the_committed_vocabulary_covers_every_registry_series_map():
    """A series_map entry pointing at nothing would quarantine at ingest time."""
    registry = load_registry(REGISTRY_PATH)
    vocabulary = json.loads(PARAMETERS_PATH.read_text(encoding="utf-8"))["parameters"]
    for record in registry.deployments:
        for series, parameter in (record.series_map or {}).items():
            assert parameter in vocabulary, f"{record.site_id}: {series} -> {parameter}"


# --------------------------------------------------------------------------
# Synthetic RawSeries -- no workbook needed to exercise the normalizer
# --------------------------------------------------------------------------


def _deployment(series_map=None) -> Deployment:
    return Deployment(
        site_id="PROJ:TIDBIT-1",
        serial=KNOWN_SERIAL,
        deployment_number=3,
        tz="America/Los_Angeles",
        window_local=("2026-07-11 08:00", "2026-08-01 07:30"),
        series_map=series_map or {"Tidbit 1": "sea_water_temperature"},
    )


def _raw_series(*, name="Tidbit 1", unit="degC", values=None, extra=None) -> RawSeries:
    """A RawSeries as an adapter would return it: local, naive, unconverted."""
    values = [14.0, 15.0] if values is None else values
    columns = [(name, unit, values)]
    if extra:
        columns.append(extra)

    frames, infos = [], []
    for series_name, series_unit, series_values in columns:
        stamps = [
            naive(2026, 7, 11, 9, 0) + timedelta(minutes=10 * i) for i in range(len(series_values))
        ]
        frames.append(
            pd.DataFrame(
                {
                    "row_number": range(1, len(series_values) + 1),
                    "timestamp_local": pd.to_datetime(stamps),
                    "series_name": series_name,
                    "unit": series_unit,
                    "value": series_values,
                }
            )
        )
        infos.append(
            SeriesInfo(
                name=series_name,
                unit=series_unit,
                column=f"{series_name} , {series_unit}",
                n=len(series_values),
                first=stamps[0],
                last=stamps[-1],
            )
        )

    return RawSeries(
        path=Path("synthetic.xlsx"),
        provenance="original",
        edit_signals=(),
        tz_token="PDT",
        data=pd.concat(frames, ignore_index=True),
        series=tuple(infos),
    )
