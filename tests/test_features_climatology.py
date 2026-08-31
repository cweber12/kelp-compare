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
    ENV_SERIES,
    anomaly_columns,
    build_climatology,
    climatology_columns,
    climatology_key,
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


# --------------------------------------------------------------------------
# One implementation, two series keys
# --------------------------------------------------------------------------
#
# The environmental half is keyed on source/site/parameter/depth; the kelp half
# is keyed on polygon. These cases drive the same functions with the second key,
# because a generalisation whose only caller is the one it was generalised out
# of proves nothing -- and "both sides of a correlation were treated the same
# way" has to be a fact about the program rather than a claim about two of them.


POLYGON_SERIES = ("polygon_id",)


def polygons(rows, cfg=None) -> pd.DataFrame:
    """A quarterly table keyed on polygon rather than on a QC series.

    Carries only what the climatology contracts for -- the key, year, quarter,
    the two usability columns and one measured feature -- so a column the
    environmental table happens to have cannot be what makes this work.
    """
    built = [
        {
            "polygon_id": polygon_id,
            "year": year,
            "quarter": quarter,
            "usable": True,
            "quarter_complete": True,
            "canopy_area_m2": value,
            **(rest[0] if rest else {}),
        }
        for polygon_id, year, quarter, value, *rest in rows
    ]
    return pd.DataFrame(built).astype({"polygon_id": "string", "quarter": "int8"})


def kelp(rows, cfg=None):
    cfg = cfg or config()
    frame = polygons(rows, cfg)
    measured = ("canopy_area_m2",)
    climatology = build_climatology(frame, cfg, series=POLYGON_SERIES, measured=measured)
    built = with_anomalies(frame, climatology, cfg, series=POLYGON_SERIES, measured=measured)
    return climatology, built


def test_a_polygon_keyed_table_gets_a_polygon_keyed_climatology():
    climatology, _ = kelp(
        [("KELP:A", 2007, 1, 100.0), ("KELP:A", 2008, 1, 200.0), ("KELP:A", 2009, 1, 300.0)]
    )
    assert tuple(climatology.columns) == climatology_columns(POLYGON_SERIES)
    assert climatology_key(POLYGON_SERIES) == ("polygon_id", "quarter", "feature")

    row = climatology.iloc[0]
    assert (row["polygon_id"], row["quarter"], row["feature"]) == ("KELP:A", 1, "canopy_area_m2")
    assert (row["n_years"], row["baseline_mean"]) == (3, 200.0)
    assert (row["baseline_start_year"], row["baseline_end_year"]) == (2007, 2011)


def test_the_same_anomaly_arithmetic_applies_under_the_other_key():
    _, built = kelp(
        [("KELP:A", 2007, 1, 100.0), ("KELP:A", 2008, 1, 200.0), ("KELP:A", 2009, 1, 300.0)]
    )
    assert built["canopy_area_m2_anom"].tolist() == [-100.0, 0.0, 100.0]
    assert built["baseline_years"].tolist() == [3, 3, 3]


def test_two_polygons_keep_separate_baselines():
    """The property `depth_m` gives the environmental key, under a key of one column."""
    climatology, built = kelp(
        [
            ("KELP:A", 2007, 1, 100.0),
            ("KELP:A", 2008, 1, 200.0),
            ("KELP:A", 2009, 1, 300.0),
            ("KELP:B", 2007, 1, 10.0),
            ("KELP:B", 2008, 1, 20.0),
            ("KELP:B", 2009, 1, 30.0),
        ]
    )
    means = climatology.set_index("polygon_id")["baseline_mean"]
    assert means.to_dict() == {"KELP:A": 200.0, "KELP:B": 20.0}
    assert built["canopy_area_m2_anom"].tolist() == [-100.0, 0.0, 100.0, -10.0, 0.0, 10.0]


def test_a_thin_baseline_produces_no_kelp_anomaly_either():
    """The same rule, not a second copy of it: a difference against a two-year
    mean is not an anomaly on either side of the comparison."""
    _, built = kelp([("KELP:A", 2007, 1, 100.0), ("KELP:A", 2008, 1, 200.0)])
    assert built["canopy_area_m2_anom"].isna().all()
    assert built["baseline_years"].tolist() == [2, 2]


def test_a_cloud_gapped_kelp_quarter_does_not_drag_its_own_baseline():
    """Hard rule 3 on the kelp side, through the environmental contributor rule."""
    rows = [
        ("KELP:A", 2007, 1, 100.0),
        ("KELP:A", 2008, 1, 200.0),
        ("KELP:A", 2009, 1, 300.0),
        ("KELP:A", 2010, 1, 5000.0, {"usable": False}),
    ]
    climatology, built = kelp(rows)
    assert climatology.iloc[0]["baseline_mean"] == 200.0
    assert climatology.iloc[0]["n_years"] == 3
    # ...and the unusable quarter still gets its anomaly, as an unusable
    # environmental quarter does. `usable` stays the single gate.
    assert built["canopy_area_m2_anom"].tolist()[-1] == 4800.0


def test_the_appended_columns_are_the_same_on_both_sides():
    assert anomaly_columns(("canopy_area_m2",)) == ("baseline_years", "canopy_area_m2_anom")
    _, built = kelp([("KELP:A", 2007, 1, 100.0)])
    assert tuple(built.columns)[-2:] == anomaly_columns(("canopy_area_m2",))


def test_a_series_key_the_table_is_not_keyed_on_raises_rather_than_coming_back_empty():
    """Passing one half's key against the other half's table would otherwise
    produce an empty climatology, which reads as "no baseline yet"."""
    frame = polygons([("KELP:A", 2007, 1, 100.0)])

    with pytest.raises(ValueError) as raised:
        build_climatology(frame, config(), series=ENV_SERIES)
    assert "site_id" in str(raised.value)

    with pytest.raises(ValueError) as raised:
        with_anomalies(frame, pd.DataFrame(), config(), series=ENV_SERIES)
    assert "site_id" in str(raised.value)


def test_a_quarterly_table_missing_its_usability_bookkeeping_raises():
    """Those two columns decide who contributes to a baseline, so a table
    without them cannot have one built -- silently or otherwise."""
    frame = polygons([("KELP:A", 2007, 1, 100.0)]).drop(columns=["quarter_complete"])

    with pytest.raises(ValueError) as raised:
        build_climatology(frame, config(), series=POLYGON_SERIES)
    assert "quarter_complete" in str(raised.value)


# --------------------------------------------------------------------------
# Per-series baseline windows (docs/04 s3)
# --------------------------------------------------------------------------


def overriding(site_id, start, end, *, cfg=None) -> FeatureConfig:
    """The same configuration, with one site given its own fixed window."""
    cfg = cfg or config()
    return FeatureConfig(
        path=cfg.path,
        coverage_floor=cfg.coverage_floor,
        baseline=cfg.baseline,
        parameters=cfg.parameters,
        baseline_overrides={
            site_id: Baseline(start_year=start, end_year=end, min_years=cfg.baseline.min_years)
        },
    )


def two_sites(rows_by_site, cfg=None) -> pd.DataFrame:
    """One frame carrying two sites, so both windows are exercised in one run."""
    frames = []
    for site_id, rows in rows_by_site.items():
        frame = quarters(rows, cfg)
        frame["site_id"] = site_id
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def test_a_site_with_no_override_still_takes_the_canonical_window():
    cfg = overriding("NDBC:46254", 2015, 2019)
    built = build_climatology(quarters([(y, 3, 15.0) for y in range(2007, 2012)]), cfg)

    assert set(built["baseline_start_year"]) == {2007}
    assert set(built["baseline_end_year"]) == {2011}


def test_an_overridden_site_takes_its_declared_window():
    """The window is stamped per row, so which one applied is readable off the table."""
    cfg = overriding("NDBC:46254", 2015, 2019)
    frame = quarters([(y, 3, 15.0) for y in range(2013, 2020)])
    frame["site_id"] = "NDBC:46254"

    built = build_climatology(frame, cfg)

    assert set(built["baseline_start_year"]) == {2015}
    assert set(built["baseline_end_year"]) == {2019}
    # 2013 and 2014 are outside the declared window and must not contribute.
    assert set(built["n_years"]) == {5}


def test_two_sites_in_one_run_get_their_own_windows():
    cfg = overriding("NDBC:46254", 2015, 2019)
    frame = two_sites(
        {
            "NDBC:LJAC1": [(y, 3, 15.0) for y in range(2007, 2012)],
            "NDBC:46254": [(y, 3, 18.0) for y in range(2015, 2020)],
        },
        cfg,
    )

    built = build_climatology(frame, cfg)
    windows = {
        row.site_id: (row.baseline_start_year, row.baseline_end_year) for row in built.itertuples()
    }

    assert windows["NDBC:LJAC1"] == (2007, 2011)
    assert windows["NDBC:46254"] == (2015, 2019)


def test_an_override_does_not_move_the_anomalies_of_an_unoverridden_site():
    """The measured claim behind shipping this inert: nothing else changes."""
    rows = [(y, 3, 15.0 + y - 2007) for y in range(2007, 2012)]
    plain = with_anomalies(quarters(rows), build_climatology(quarters(rows), config()), config())

    cfg = overriding("NDBC:46254", 2015, 2019)
    with_one = with_anomalies(quarters(rows, cfg), build_climatology(quarters(rows, cfg), cfg), cfg)

    pd.testing.assert_series_equal(plain["mean_anom"], with_one["mean_anom"])
    pd.testing.assert_series_equal(plain["baseline_years"], with_one["baseline_years"])


def test_an_overridden_series_that_reaches_min_years_gets_real_anomalies():
    """The point of the mechanism: a post-baseline station stops being all-null."""
    cfg = overriding("NDBC:46254", 2015, 2019)
    frame = quarters([(y, 3, 15.0) for y in range(2015, 2021)])
    frame["site_id"] = "NDBC:46254"

    built = with_anomalies(frame, build_climatology(frame, cfg), cfg)

    assert built["mean_anom"].notna().all()
    # 2020 sits outside the declared window, so five years stand behind them.
    assert set(built["baseline_years"]) == {5}


def test_a_series_too_short_for_its_own_window_is_still_null():
    """min_years is not overridable, so a thin record stays thin."""
    cfg = overriding("NDBC:46266", 2020, 2025)
    frame = quarters([(y, 3, 15.0) for y in (2020, 2021)])
    frame["site_id"] = "NDBC:46266"

    built = with_anomalies(frame, build_climatology(frame, cfg), cfg)

    assert built["mean_anom"].isna().all()


def test_the_kelp_half_is_untouched_by_a_site_id_override():
    """Keyed on `polygon_id`, so no override can reach it (docs/04 s3)."""
    from kelpcompare.features.kelp import KELP_SERIES, MEASURED, quarterly_kelp_columns

    cfg = overriding("NDBC:46254", 2015, 2019)
    rows = [
        {
            "source": "kelp_watch",
            "polygon_id": "KELP:LA-JOLLA",
            "year": year,
            "quarter": 3,
            "usable": True,
            "quarter_complete": True,
            "kelp_area_m2": 1000.0 + year,
            "n_cells_kelp": 10,
        }
        for year in range(2007, 2012)
    ]
    frame = pd.DataFrame(rows).reindex(columns=list(quarterly_kelp_columns()))
    frame[["usable", "quarter_complete"]] = True

    built = build_climatology(frame, cfg, series=KELP_SERIES, measured=MEASURED)

    assert set(built["baseline_start_year"]) == {2007}
    assert set(built["baseline_end_year"]) == {2011}


def warnings_for(frame, cfg):
    from kelpcompare.features.climatology import override_warnings

    return override_warnings(frame, cfg)


def test_an_override_on_a_series_that_did_not_need_one_warns():
    """The one way this mechanism could quietly move anomalies that were fine."""
    cfg = overriding("NDBC:LJAC1", 2009, 2011)
    frame = quarters([(y, 3, 15.0) for y in range(2007, 2012)], cfg)

    warned = warnings_for(frame, cfg)

    assert len(warned) == 1
    assert "NDBC:LJAC1" in warned[0]
    assert "2009-2011" in warned[0] and "2007-2011" in warned[0]
    assert "did not need an override" in warned[0]


def test_an_override_on_a_series_that_needed_one_is_silent():
    """A station whose record post-dates the window is the case this is for."""
    cfg = overriding("NDBC:46254", 2015, 2019)
    frame = quarters([(y, 3, 15.0) for y in range(2015, 2020)], cfg)
    frame["site_id"] = "NDBC:46254"

    assert warnings_for(frame, cfg) == ()


def test_a_series_short_of_min_years_in_the_canonical_window_is_silent():
    """Partial coverage of the canonical window is exactly what an override is for."""
    cfg = overriding("NDBC:46254", 2009, 2011)
    frame = quarters([(y, 3, 15.0) for y in (2010, 2011)], cfg)
    frame["site_id"] = "NDBC:46254"

    assert warnings_for(frame, cfg) == ()


def test_no_overrides_declared_warns_about_nothing():
    frame = quarters([(y, 3, 15.0) for y in range(2007, 2012)])

    assert warnings_for(frame, config()) == ()


def test_unusable_quarters_do_not_make_an_override_look_redundant():
    """The same contributor rule the baseline itself uses, or the warning lies."""
    cfg = overriding("NDBC:46254", 2015, 2019)
    rows = [(y, 3, 15.0, {"usable": False}) for y in range(2007, 2012)]
    frame = quarters(rows, cfg)
    frame["site_id"] = "NDBC:46254"

    assert warnings_for(frame, cfg) == ()


def test_the_warning_reaches_the_build_outcome():
    """Warned through `build_features`, so it lands in the run manifest (docs/01 s5)."""
    from kelpcompare.features.build import build_features
    from kelpcompare.storage import OBSERVATION_COLUMNS

    cfg = overriding("NDBC:LJAC1", 2009, 2011)
    stamps = pd.date_range("2007-01-01", "2011-12-31 23:00", freq="6h", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": stamps,
            "site_id": "NDBC:LJAC1",
            "parameter": "sea_water_temperature",
            "value": 15.0,
            "depth_m": 3.4,
            "qc_flag": 1,
            "qc_tests": "gross_range:pass",
            "source": "ndbc",
            "fetch_run_id": "20260101T000000000Z-ingest",
        }
    )[list(OBSERVATION_COLUMNS)]

    outcome = build_features(frame, cfg, now=pd.Timestamp("2012-06-01", tz="UTC"))

    assert any("did not need an override" in warning for warning in outcome.warnings)
    assert set(outcome.climatology["baseline_start_year"]) == {2009}
