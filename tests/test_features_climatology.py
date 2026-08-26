"""Climatology and anomalies (docs/04 s3).

The promise this stage makes is that an anomaly computed today still means the
same thing after next year's backfill. That is a property of two runs, not of
one number, so several cases here build a table, append data, rebuild, and
assert nothing moved.

Frames are constructed directly in the quarterly shape rather than driven from
observations: the arithmetic under test is what happens between quarters, and a
hand-written quarter is easier to check than one derived from 2160 readings.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.features.climatology import (
    CLIMATOLOGY_COLUMNS,
    build_climatology,
    quarterly_env_columns,
    with_anomalies,
)
from kelpcompare.features.config import Baseline, FeatureConfig, ParameterFeatures
from kelpcompare.features.quarterly import quarterly_columns

TEMPERATURE = ParameterFeatures(
    parameter="sea_water_temperature",
    feature_set="temperature",
    thresholds={"days_above": (20.0,)},
)


def config(*, start=2007, end=2011, min_years=3) -> FeatureConfig:
    return FeatureConfig(
        path=Path("features.json"),
        coverage_floor=0.6,
        baseline=Baseline(start_year=start, end_year=end, min_years=min_years),
        parameters={"sea_water_temperature": TEMPERATURE},
    )


def quarters(rows, cfg=None) -> pd.DataFrame:
    """Build a `quarterly_env` frame (pre-anomaly) from `(year, quarter, mean)`
    triples or full dicts."""
    cfg = cfg or config()
    built = []
    for row in rows:
        year, quarter, mean = row if len(row) == 3 else row[:3]
        extra = row[3] if len(row) == 4 else {}
        built.append(
            {
                "source": "ndbc",
                "site_id": "NDBC:LJAC1",
                "parameter": "sea_water_temperature",
                "depth_m": 3.4,
                "year": year,
                "quarter": quarter,
                "feature_set": "temperature",
                "n_obs": 2160,
                "n_days_observed": 90,
                "cadence_s": 3600.0,
                "expected_obs": 2160.0,
                "pct_coverage": 1.0,
                "usable": True,
                "quarter_complete": True,
                "qc_max_flag": 2,
                "mean": mean,
                "min": mean - 1.0,
                "max": mean + 1.0,
                "p05": mean - 0.9,
                "p95": mean + 0.9,
                "variance": 1.0,
                "days_above_20c": 0.0,
                "max_spell_above_20c_days": 0.0,
                "max_spell_above_20c_gap_interrupted": False,
                **extra,
            }
        )
    return pd.DataFrame(built).reindex(columns=list(quarterly_columns(cfg)))


def finished(rows, cfg=None) -> pd.DataFrame:
    cfg = cfg or config()
    frame = quarters(rows, cfg)
    return with_anomalies(frame, build_climatology(frame, cfg), cfg)


def cell(climatology: pd.DataFrame, feature: str, quarter: int = 1) -> pd.Series:
    match = climatology.loc[
        (climatology["feature"] == feature) & (climatology["quarter"] == quarter)
    ]
    assert len(match) == 1, f"expected one {feature} cell for Q{quarter}, got {len(match)}"
    return match.iloc[0]


# --------------------------------------------------------------------------
# The climatology table -- docs/03
# --------------------------------------------------------------------------


def test_the_climatology_records_its_baseline_window_mean_spread_and_year_count():
    """ "The anomalies did not shift" has to be checkable by diffing two runs."""
    climatology = build_climatology(quarters([(2007, 1, 14.0), (2008, 1, 16.0)]), config())
    row = cell(climatology, "mean")
    assert (row["baseline_start_year"], row["baseline_end_year"]) == (2007, 2011)
    assert row["n_years"] == 2
    assert row["baseline_mean"] == 15.0
    assert row["baseline_std"] == pytest.approx(2**0.5)  # sample convention, ddof=1


def test_the_climatology_columns_are_the_documented_ones():
    climatology = build_climatology(quarters([(2007, 1, 14.0)]), config())
    assert tuple(climatology.columns) == CLIMATOLOGY_COLUMNS


def test_a_single_year_baseline_has_a_mean_but_no_spread():
    row = cell(build_climatology(quarters([(2007, 1, 14.0)]), config()), "mean")
    assert row["baseline_mean"] == 14.0
    assert pd.isna(row["baseline_std"])


def test_each_quarter_of_the_year_gets_its_own_baseline():
    """Removing the seasonal cycle is the whole point; one annual mean would not."""
    frame = quarters([(2007, 1, 14.0), (2008, 1, 14.0), (2007, 3, 22.0), (2008, 3, 22.0)])
    climatology = build_climatology(frame, config())
    assert cell(climatology, "mean", quarter=1)["baseline_mean"] == 14.0
    assert cell(climatology, "mean", quarter=3)["baseline_mean"] == 22.0


def test_every_measured_feature_gets_a_baseline_and_no_bookkeeping_column_does():
    climatology = build_climatology(quarters([(2007, 1, 14.0)]), config())
    features = set(climatology["feature"])
    assert {"mean", "min", "max", "p05", "p95", "variance", "days_above_20c"} <= features
    assert not features & {"n_obs", "pct_coverage", "cadence_s", "usable", "expected_obs"}
    assert "max_spell_above_20c_gap_interrupted" not in features


def test_a_year_outside_the_window_does_not_contribute():
    frame = quarters([(2007, 1, 14.0), (2008, 1, 16.0), (2020, 1, 30.0)])
    assert cell(build_climatology(frame, config()), "mean")["baseline_mean"] == 15.0


def test_an_unusable_quarter_does_not_drag_the_baseline_it_is_compared_against():
    frame = quarters(
        [
            (2007, 1, 14.0),
            (2008, 1, 16.0),
            (2009, 1, 40.0, {"usable": False, "pct_coverage": 0.1}),
        ]
    )
    row = cell(build_climatology(frame, config()), "mean")
    assert row["baseline_mean"] == 15.0
    assert row["n_years"] == 2


def test_an_incomplete_quarter_does_not_contribute_however_well_covered():
    """Otherwise the baseline is biased toward whatever part of the year the run
    happened in."""
    frame = quarters(
        [(2007, 1, 14.0), (2008, 1, 16.0), (2009, 1, 40.0, {"quarter_complete": False})]
    )
    assert cell(build_climatology(frame, config()), "mean")["n_years"] == 2


def test_a_series_with_no_contributing_quarter_gets_no_climatology_row():
    frame = quarters([(2020, 1, 14.0), (2021, 1, 16.0)])
    assert build_climatology(frame, config()).empty


def test_two_series_at_one_site_keep_separate_baselines():
    frame = quarters([(2007, 1, 14.0), (2008, 1, 16.0)])
    deep = frame.copy()
    deep["depth_m"] = 9.0
    deep["mean"] = [4.0, 6.0]
    climatology = build_climatology(pd.concat([frame, deep], ignore_index=True), config())
    means = climatology.loc[climatology["feature"] == "mean"].set_index("depth_m")["baseline_mean"]
    assert means.to_dict() == {3.4: 15.0, 9.0: 5.0}


def test_a_null_depth_series_is_matched_to_its_own_baseline():
    """Every met parameter has a null depth, so the anomaly lookup key -- which is
    not a merge, precisely because of this -- has to survive one."""
    frame = quarters([(2007, 1, 14.0), (2008, 1, 15.0), (2009, 1, 16.0)])
    frame["depth_m"] = None
    climatology = build_climatology(frame, config())
    assert cell(climatology, "mean")["baseline_mean"] == 15.0
    assert with_anomalies(frame, climatology, config())["mean_anom"].tolist() == [-1.0, 0.0, 1.0]


# --------------------------------------------------------------------------
# The anomalies -- docs/04 s3
# --------------------------------------------------------------------------


def test_every_measured_feature_gets_an_anom_twin_and_no_bookkeeping_column_does():
    built = finished([(2007, 1, 14.0), (2008, 1, 16.0), (2009, 1, 15.0)])
    assert tuple(built.columns) == quarterly_env_columns(config())
    assert {"mean_anom", "min_anom", "days_above_20c_anom", "variance_anom"} <= set(built.columns)
    assert not {"n_obs_anom", "pct_coverage_anom", "usable_anom"} & set(built.columns)
    assert "max_spell_above_20c_gap_interrupted_anom" not in built.columns


def test_an_anomaly_is_the_feature_minus_its_own_quarterly_baseline():
    built = finished([(2007, 1, 14.0), (2008, 1, 15.0), (2009, 1, 16.0)])
    assert built["mean_anom"].tolist() == [-1.0, 0.0, 1.0]


def test_the_row_carries_how_many_years_stand_behind_its_anomaly():
    built = finished([(2007, 1, 14.0), (2008, 1, 15.0), (2009, 1, 16.0)])
    assert built["baseline_years"].tolist() == [3, 3, 3]


def test_a_baseline_below_the_minimum_produces_no_anomaly_at_all():
    """A difference against a two-year mean is not an anomaly."""
    built = finished([(2007, 1, 14.0), (2008, 1, 16.0)])
    assert built["mean_anom"].isna().all()
    assert built["baseline_years"].tolist() == [2, 2]


def test_a_quarter_outside_the_window_still_gets_an_anomaly():
    built = finished([(2007, 1, 14.0), (2008, 1, 15.0), (2009, 1, 16.0), (2024, 1, 20.0)])
    assert built.set_index("year")["mean_anom"][2024] == 5.0


def test_an_unusable_quarter_still_gets_an_anomaly():
    """`usable` is the single gate on this table; a second one would be a second
    vocabulary for the same warning."""
    rows = [
        (2007, 1, 14.0),
        (2008, 1, 15.0),
        (2009, 1, 16.0),
        (2010, 1, 25.0, {"usable": False, "pct_coverage": 0.2}),
    ]
    built = finished(rows).set_index("year")
    assert built["mean_anom"][2010] == 10.0
    assert not built["usable"][2010]


def test_a_quarter_the_baseline_cannot_reach_gets_a_null_anomaly_not_a_zero():
    frame = quarters([(2007, 1, 14.0), (2008, 1, 15.0), (2009, 1, 16.0), (2009, 2, 18.0)])
    built = with_anomalies(frame, build_climatology(frame, config()), config())
    q2 = built.loc[built["quarter"] == 2].iloc[0]
    assert pd.isna(q2["mean_anom"])
    assert q2["baseline_years"] == 1


# --------------------------------------------------------------------------
# The promise: anomalies do not shift
# --------------------------------------------------------------------------


def test_appending_a_year_outside_the_window_moves_no_existing_anomaly():
    """The property the fixed baseline exists to guarantee."""
    history = [(2007, 1, 14.0), (2008, 1, 15.0), (2009, 1, 16.0)]
    before = finished(history)
    after = finished([*history, (2024, 1, 30.0)])
    assert after["mean_anom"].tolist()[:3] == before["mean_anom"].tolist()
    assert after["baseline_years"].tolist()[:3] == before["baseline_years"].tolist()


def test_appending_a_year_inside_the_window_does_move_them_and_should():
    """A backfill of the baseline period is a different baseline. The window is
    chosen to end well before the tail of the record so this does not happen by
    accident (docs/04 s3)."""
    history = [(2008, 1, 15.0), (2009, 1, 16.0), (2010, 1, 17.0)]
    before = finished(history)
    after = finished([(2007, 1, 4.0), *history])
    assert after["mean_anom"].tolist()[1:] != before["mean_anom"].tolist()


def test_two_builds_over_unchanged_input_are_identical():
    rows = [(2007, 1, 14.0), (2008, 1, 15.0), (2009, 1, 16.0)]
    assert finished(rows).equals(finished(rows))
    frame = quarters(rows)
    assert build_climatology(frame, config()).equals(build_climatology(frame, config()))


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


def test_an_empty_quarterly_table_produces_empty_tables_with_the_right_columns():
    empty = quarters([])
    assert tuple(build_climatology(empty, config()).columns) == CLIMATOLOGY_COLUMNS
    built = with_anomalies(empty, build_climatology(empty, config()), config())
    assert built.empty
    assert tuple(built.columns) == quarterly_env_columns(config())


def test_a_feature_null_in_every_contributing_year_gets_no_baseline_cell():
    rows = [(2007, 1, 14.0), (2008, 1, 15.0), (2009, 1, 16.0)]
    frame = quarters(rows)
    frame["variance"] = None
    climatology = build_climatology(frame, config())
    assert "variance" not in set(climatology["feature"])
    assert with_anomalies(frame, climatology, config())["variance_anom"].isna().all()
