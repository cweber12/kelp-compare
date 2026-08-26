"""Quarterly feature arithmetic (docs/04 s2, docs/03 `quarterly_env`).

The builder is a pure function -- observation frame in, feature frame out -- so
this is where the mathematics is pinned down, against frames small enough that a
reviewer can check every number by hand. The seam mirrors the QC evaluator's:
the CLI suite drives the same code through the real command.

Numbers here are chosen to be checkable, not realistic. The realistic end-to-end
run is in `test_cli_features.py`.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.features.config import Baseline, FeatureConfig, ParameterFeatures
from kelpcompare.features.quarterly import (
    build_quarterly,
    quarterly_columns,
    threshold_label,
)
from kelpcompare.storage import (
    FLAG_FAIL,
    FLAG_NOT_EVALUATED,
    FLAG_PASS,
    OBSERVATION_COLUMNS,
)

NOW = pd.Timestamp("2026-08-26T00:00:00Z")

TEMPERATURE = ParameterFeatures(
    parameter="sea_water_temperature",
    feature_set="temperature",
    thresholds={
        "days_above": (20.0, 23.0),
        "days_below": (14.0,),
        "degree_days_above": (18.0,),
        "max_spell_above": (20.0,),
    },
)
AIR = ParameterFeatures(parameter="air_temperature", feature_set="statistics")


def config(*, coverage_floor: float = 0.6, parameters=None) -> FeatureConfig:
    return FeatureConfig(
        path=Path("features.json"),
        coverage_floor=coverage_floor,
        baseline=Baseline(start_year=2007, end_year=2019, min_years=10),
        parameters=parameters or {"sea_water_temperature": TEMPERATURE, "air_temperature": AIR},
    )


def observations(
    stamps,
    values=None,
    *,
    qc_flag: int = FLAG_PASS,
    parameter: str = "sea_water_temperature",
    depth_m: float | None = 3.4,
    site: str = "NDBC:LJAC1",
    source: str = "ndbc",
) -> pd.DataFrame:
    """A docs/03 observation frame. Values default to a constant well below every
    threshold, so a test that does not care about values does not accidentally
    trip one."""
    index = stamps if isinstance(stamps, pd.DatetimeIndex) else pd.to_datetime(stamps, utc=True)
    return pd.DataFrame(
        {
            "timestamp": index,
            "site_id": site,
            "parameter": parameter,
            "value": 15.0 if values is None else values,
            "depth_m": depth_m,
            "qc_flag": qc_flag,
            "qc_tests": "gross_range:pass",
            "source": source,
            "fetch_run_id": "20260101T000000000Z-ingest",
        }
    )[list(OBSERVATION_COLUMNS)]


def every(freq: str, start: str, end: str, values=None, **fields) -> pd.DataFrame:
    return observations(pd.date_range(start, end, freq=freq, tz="UTC"), values, **fields)


def day(date: str, *values, **fields) -> pd.DataFrame:
    """One UTC day's readings, evenly spaced through it."""
    stamps = pd.date_range(f"{date} 00:00", periods=len(values), freq="6h", tz="UTC")
    return observations(stamps, list(values), **fields)


def build(frame, cfg=None, **kwargs):
    return build_quarterly(frame, cfg or config(), now=kwargs.pop("now", NOW), **kwargs)


def one(frame, cfg=None, **kwargs) -> pd.Series:
    """The single row a one-series, one-quarter frame should produce."""
    built = build(frame, cfg, **kwargs).frame
    assert len(built) == 1, f"expected one quarter, got {len(built)}"
    return built.iloc[0]


# --------------------------------------------------------------------------
# The row key -- docs/03
# --------------------------------------------------------------------------


def test_the_row_key_is_the_qc_series_key_plus_time():
    row = one(day("2007-01-01", 15.0, 16.0))
    assert list(row[["source", "site_id", "parameter", "depth_m", "year", "quarter"]]) == [
        "ndbc",
        "NDBC:LJAC1",
        "sea_water_temperature",
        3.4,
        2007,
        1,
    ]


def test_a_shallow_and_a_deep_logger_at_one_site_are_never_averaged():
    """The quarterly minimum and cold-day counts are exactly what that would corrupt."""
    frame = pd.concat(
        [day("2007-01-01", 20.0, 20.0, depth_m=1.0), day("2007-01-01", 10.0, 10.0, depth_m=9.0)],
        ignore_index=True,
    )
    built = build(frame).frame
    assert list(built["depth_m"]) == [1.0, 9.0]
    assert list(built["mean"]) == [20.0, 10.0]


def test_an_observation_either_side_of_a_utc_quarter_boundary_lands_in_its_own_quarter():
    frame = observations(["2007-03-31T23:59:59Z", "2007-04-01T00:00:00Z"], [15.0, 16.0])
    built = build(frame).frame
    assert list(zip(built["year"], built["quarter"], strict=True)) == [(2007, 1), (2007, 2)]


def test_a_quarter_with_no_stored_rows_gets_no_row_rather_than_an_empty_one():
    """Inventing a quarter nobody observed is the imputation hard rule 3 forbids."""
    frame = pd.concat([day("2007-01-01", 15.0), day("2007-07-01", 15.0)], ignore_index=True)
    assert list(build(frame).frame["quarter"]) == [1, 3]


def test_the_columns_are_the_documented_ones_in_order():
    built = build(day("2007-01-01", 15.0)).frame
    assert tuple(built.columns) == quarterly_columns(config())


# --------------------------------------------------------------------------
# The distribution -- docs/04 s2
# --------------------------------------------------------------------------


def test_the_statistics_are_reproducible_by_hand():
    """[10, 12, 14, 20]: mean 14; sample variance 56/3; p05 and p95 interpolate
    linearly at positions 0.15 and 2.85 of the sorted values."""
    row = one(day("2007-01-01", 10.0, 12.0, 14.0, 20.0))
    assert row["mean"] == 14.0
    assert (row["min"], row["max"]) == (10.0, 20.0)
    assert row["variance"] == pytest.approx(56 / 3)
    assert row["p05"] == pytest.approx(10.3)
    assert row["p95"] == pytest.approx(19.1)


def test_a_single_observation_quarter_reports_a_null_variance_not_a_zero():
    """Zero would claim the water did not vary. It claims nothing."""
    row = one(day("2007-01-01", 15.0))
    assert pd.isna(row["variance"])
    assert row["mean"] == 15.0


def test_a_parameter_on_the_statistics_set_gets_no_temperature_columns():
    frame = day("2007-01-01", 15.0, 16.0, parameter="air_temperature")
    row = one(frame)
    assert row["feature_set"] == "statistics"
    assert row["mean"] == 15.5
    assert pd.isna(row["days_above_20c"])
    assert pd.isna(row["max_spell_above_20c_gap_interrupted"])


# --------------------------------------------------------------------------
# Coverage -- the series' own cadence
# --------------------------------------------------------------------------


def test_a_fully_sampled_quarter_scores_full_coverage_at_any_cadence():
    """An hourly station and a 10-minute logger are judged on the same scale."""
    hourly = one(every("h", "2007-01-01", "2007-03-31 23:00"))
    ten_minute = one(every("10min", "2007-01-01", "2007-03-31 23:50"))
    assert hourly["pct_coverage"] == 1.0
    assert ten_minute["pct_coverage"] == 1.0
    assert (hourly["cadence_s"], ten_minute["cadence_s"]) == (3600.0, 600.0)
    assert hourly["n_obs"] == 2160  # 90 days x 24
    assert ten_minute["n_obs"] == 12960


def test_half_a_quarter_of_hourly_data_scores_one_half():
    """The median interval is unmoved by the gap: gaps are the tail of the
    interval distribution, not its middle."""
    row = one(every("h", "2007-01-01", "2007-02-14 23:00"))
    assert row["n_obs"] == 1080
    assert row["expected_obs"] == 2160.0
    assert row["pct_coverage"] == 0.5


def test_the_fraction_is_auditable_from_the_columns_beside_it():
    row = one(every("h", "2007-01-01", "2007-02-14 23:00"))
    assert row["n_obs"] / row["expected_obs"] == row["pct_coverage"]
    assert row["expected_obs"] * row["cadence_s"] == 90 * 86400


def test_a_quarter_with_one_observation_has_no_cadence_and_no_coverage():
    row = one(day("2007-01-01", 15.0))
    assert pd.isna(row["cadence_s"])
    assert pd.isna(row["expected_obs"])
    assert row["pct_coverage"] == 0.0
    assert not row["usable"]


def test_a_cadence_that_changed_mid_quarter_is_clamped_and_named():
    """Ten days at 10 minutes then eighty at an hour: the median interval is an
    hour, so 3360 observations land against 2160 expected."""
    frame = pd.concat(
        [
            every("10min", "2007-01-01", "2007-01-10 23:50"),
            every("h", "2007-01-11", "2007-03-31 23:00"),
        ],
        ignore_index=True,
    )
    built = build(frame)
    row = built.frame.iloc[0]
    assert row["n_obs"] == 3360
    assert row["expected_obs"] == 2160.0
    assert row["pct_coverage"] == 1.0
    assert any("cadence changed mid-quarter" in warning for warning in built.warnings)
    assert any("2007Q1" in warning for warning in built.warnings)


def test_the_number_of_observed_days_is_recorded_so_a_day_count_reads_as_a_floor():
    frame = pd.concat([day("2007-01-01", 15.0, 16.0), day("2007-01-05", 15.0)], ignore_index=True)
    assert one(frame)["n_days_observed"] == 2


# --------------------------------------------------------------------------
# QC filtering -- coverage counts the rows the features are computed from
# --------------------------------------------------------------------------


def test_a_quarter_that_failed_qc_on_every_row_scores_zero_coverage_not_full():
    frame = every("h", "2007-01-01", "2007-03-31 23:00", qc_flag=FLAG_FAIL)
    row = one(frame)
    assert row["n_obs"] == 0
    assert row["pct_coverage"] == 0.0
    assert not row["usable"]
    assert pd.isna(row["mean"])
    assert pd.isna(row["days_above_20c"])


def test_the_qc_filter_level_is_recorded_on_every_row():
    assert one(day("2007-01-01", 15.0))["qc_max_flag"] == 2


def test_a_stricter_filter_changes_the_result_and_says_so():
    """docs/04 s1 requires key results to be rerunnable at pass-only.

    The default filter keeps not-evaluated rows; pass-only does not. A suspect
    row is outside both, which is why the second day here is at flag 2.
    """
    frame = pd.concat(
        [
            day("2007-01-01", 15.0, 16.0),
            day("2007-01-02", 25.0, 25.0, qc_flag=FLAG_NOT_EVALUATED),
        ],
        ignore_index=True,
    )
    default = one(frame)
    strict = one(frame, qc_max_flag=FLAG_PASS)
    assert (default["n_obs"], strict["n_obs"]) == (4, 2)
    assert (default["max"], strict["max"]) == (25.0, 16.0)
    assert (default["qc_max_flag"], strict["qc_max_flag"]) == (2, 1)


# --------------------------------------------------------------------------
# Usability -- flagged, never dropped
# --------------------------------------------------------------------------


def test_a_quarter_below_the_floor_keeps_its_features_and_is_flagged_unusable():
    """Hard rule 4's discipline, applied to quarters instead of rows."""
    row = one(every("h", "2007-01-01", "2007-01-10 23:00"))
    assert row["pct_coverage"] == pytest.approx(240 / 2160)
    assert not row["usable"]
    assert row["mean"] == 15.0


def test_the_floor_is_a_knob_rather_than_a_filter_already_applied():
    frame = every("h", "2007-01-01", "2007-01-10 23:00")
    assert not one(frame)["usable"]
    assert one(frame, config(coverage_floor=0.1))["usable"]


def test_the_quarter_a_run_happens_in_is_marked_incomplete():
    """0.479 coverage because the quarter had not finished is a different fact
    from 0.539 because the station was down."""
    frame = every("h", "2026-07-01", "2026-08-25 23:00")
    row = one(frame, now=NOW)
    assert not row["quarter_complete"]
    assert one(day("2007-01-01", 15.0, 16.0))["quarter_complete"]


# --------------------------------------------------------------------------
# Threshold features -- docs/04 s2
# --------------------------------------------------------------------------


def test_days_above_counts_days_whose_daily_maximum_exceeds_the_threshold():
    frame = pd.concat(
        [day("2007-01-01", 19.0, 21.0), day("2007-01-02", 19.0, 19.9)], ignore_index=True
    )
    row = one(frame)
    assert row["days_above_20c"] == 1.0
    assert row["days_above_23c"] == 0.0


def test_days_below_counts_days_whose_daily_minimum_falls_under_the_threshold():
    """The nitrate proxy: in Southern California nitrate is high only in cold water."""
    frame = pd.concat(
        [day("2007-01-01", 13.0, 18.0), day("2007-01-02", 15.0, 18.0)], ignore_index=True
    )
    assert one(frame)["days_below_14c"] == 1.0


def test_degree_days_sum_the_positive_excess_of_each_days_mean():
    """Day one means 21 and contributes 3; day two means 11 and contributes nothing,
    never a negative."""
    frame = pd.concat(
        [day("2007-01-01", 20.0, 22.0), day("2007-01-02", 10.0, 12.0)], ignore_index=True
    )
    assert one(frame)["degree_days_above_18c"] == 3.0


def test_a_spell_is_broken_by_an_unobserved_day_and_says_it_was():
    """Bridging the gap would invent a reading for a day nobody measured."""
    frame = pd.concat(
        [
            day("2007-01-01", 21.0),
            day("2007-01-02", 21.0),
            day("2007-01-04", 21.0),
            day("2007-01-05", 21.0),
        ],
        ignore_index=True,
    )
    row = one(frame)
    assert row["max_spell_above_20c_days"] == 2.0
    assert row["max_spell_above_20c_gap_interrupted"]


def test_a_spell_ended_by_an_observed_cool_day_is_a_measurement_not_a_floor():
    frame = pd.concat(
        [
            day("2007-01-01", 21.0),
            day("2007-01-02", 21.0),
            day("2007-01-03", 21.0),
            day("2007-01-04", 15.0),
        ],
        ignore_index=True,
    )
    row = one(frame)
    assert row["max_spell_above_20c_days"] == 3.0
    assert not row["max_spell_above_20c_gap_interrupted"]


def test_a_spell_running_to_the_quarter_boundary_is_not_called_a_gap():
    """The quarter boundary is a limitation of quarterly features, not a hole in
    the record: 1 April is not an unobserved day of Q1, it is not a day of Q1."""
    frame = pd.concat(
        [day("2007-03-29", 15.0), day("2007-03-30", 21.0), day("2007-03-31", 21.0)],
        ignore_index=True,
    )
    row = one(frame)
    assert row["max_spell_above_20c_days"] == 2.0
    assert not row["max_spell_above_20c_gap_interrupted"]


def test_a_spell_that_starts_after_an_unobserved_day_is_marked():
    frame = pd.concat(
        [
            day("2007-01-02", 21.0),
            day("2007-01-03", 21.0),
            day("2007-01-04", 21.0),
            day("2007-01-05", 15.0),
        ],
        ignore_index=True,
    )
    row = one(frame)
    assert row["max_spell_above_20c_days"] == 3.0
    assert row["max_spell_above_20c_gap_interrupted"]


def test_a_quarter_with_no_qualifying_day_reports_a_zero_spell_that_no_gap_touched():
    row = one(day("2007-01-01", 15.0, 16.0))
    assert row["max_spell_above_20c_days"] == 0.0
    assert not row["max_spell_above_20c_gap_interrupted"]


def test_a_daylight_saving_transition_changes_nothing():
    """Spring forward falls inside Q1; UTC arithmetic makes it irrelevant, and this
    is what proves that rather than assumes it."""
    frame = every("h", "2007-01-01", "2007-03-31 23:00", values=15.0)
    row = one(frame)
    assert row["n_days_observed"] == 90
    assert row["n_obs"] == 2160
    assert row["pct_coverage"] == 1.0


# --------------------------------------------------------------------------
# Column naming -- docs/03
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("threshold", "label"), [(20.0, "20c"), (23.0, "23c"), (20.5, "20_5c"), (-1.0, "neg1c")]
)
def test_a_threshold_names_its_own_column(threshold, label):
    assert threshold_label(threshold) == label


def test_retuning_a_threshold_renames_its_column_rather_than_redefining_it():
    retuned = ParameterFeatures(
        parameter="sea_water_temperature",
        feature_set="temperature",
        thresholds={"days_above": (21.5,)},
    )
    built = build(day("2007-01-01", 22.0), config(parameters={"sea_water_temperature": retuned}))
    assert "days_above_21_5c" in built.frame.columns
    assert "days_above_20c" not in built.frame.columns


# --------------------------------------------------------------------------
# Unconfigured parameters, and determinism
# --------------------------------------------------------------------------


def test_a_parameter_with_no_configuration_is_skipped_and_named():
    frame = day("2007-01-01", 3.0, 4.0, parameter="wind_speed")
    built = build(frame)
    assert built.frame.empty
    assert len(built.warnings) == 1
    assert "wind_speed" in built.warnings[0]
    assert "NDBC:LJAC1" in built.warnings[0]


def test_a_configured_parameter_beside_an_unconfigured_one_is_still_built():
    frame = pd.concat(
        [day("2007-01-01", 15.0, 16.0), day("2007-01-01", 3.0, 4.0, parameter="wind_speed")],
        ignore_index=True,
    )
    built = build(frame)
    assert list(built.frame["parameter"]) == ["sea_water_temperature"]
    assert built.warnings


def test_an_empty_frame_produces_the_documented_columns_and_no_rows():
    built = build(observations(pd.DatetimeIndex([], tz="UTC"), []))
    assert built.frame.empty
    assert tuple(built.frame.columns) == quarterly_columns(config())


def test_two_builds_over_unchanged_input_are_identical():
    """What makes `rebuild` meaningful (docs/03 integrity rules)."""
    frame = every("h", "2007-01-01", "2007-03-31 23:00")
    first, second = build(frame).frame, build(frame).frame
    assert first.equals(second)


def test_a_naive_timestamp_column_never_reaches_the_builder():
    frame = day("2007-01-01", 15.0)
    frame["timestamp"] = frame["timestamp"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="hard rule 2"):
        build(frame)


# --------------------------------------------------------------------------
# What the manifest is told
# --------------------------------------------------------------------------


def test_each_series_reports_its_quarters_and_how_many_survived_the_floor():
    frame = pd.concat(
        [
            every("h", "2007-01-01", "2007-03-31 23:00"),  # full quarter, usable
            every("h", "2007-04-01", "2007-04-05 23:00"),  # five days, not usable
        ],
        ignore_index=True,
    )
    (series,) = build(frame).series
    assert (series.source, series.site_id) == ("ndbc", "NDBC:LJAC1")
    assert series.parameter == "sea_water_temperature"
    assert series.depth_m == 3.4
    assert series.feature_set == "temperature"
    assert (series.quarters, series.quarters_usable) == (2, 1)
    assert (series.first_quarter, series.last_quarter) == ("2007Q1", "2007Q2")


def test_a_series_with_a_null_depth_reports_one():
    frame = day("2007-01-01", 15.0, 16.0, depth_m=None, parameter="air_temperature")
    (series,) = build(frame).series
    assert series.depth_m is None
    assert pd.isna(build(frame).frame.iloc[0]["depth_m"])
