"""The kelp quarterly table and its climatology (docs/03, docs/04 s2-s3).

Frames are built in the parser's output shape rather than driven from a CSV: the
arithmetic under test is what happens *after* parsing, and a hand-written
quarter is easier to check than one read out of a 212-row export. The end-to-end
path from a real export is covered by the CLI suite.

The recurring theme is that a Kelp Watch quarter carries two different kinds of
bad news -- nothing was seen, and not enough was seen -- and they must stay
distinguishable all the way to the table.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.features.climatology import CLIMATOLOGY_COLUMNS, climatology_columns
from kelpcompare.features.config import Baseline, FeatureConfig, ParameterFeatures
from kelpcompare.features.kelp import (
    CLIMATOLOGY_KELP_KEY,
    KELP_SERIES,
    MEASURED,
    build_kelp,
    quarterly_kelp_columns,
)

NOW = pd.Timestamp("2026-08-26", tz="UTC")


def config(*, start=2007, end=2011, min_years=3, floor=0.6) -> FeatureConfig:
    return FeatureConfig(
        path=Path("features.json"),
        coverage_floor=floor,
        baseline=Baseline(start_year=start, end_year=end, min_years=min_years),
        parameters={
            "sea_water_temperature": ParameterFeatures(
                parameter="sea_water_temperature", feature_set="statistics"
            )
        },
    )


def parsed(rows, *, polygon_id: str = "KELP:A", footprint: int = 100) -> pd.DataFrame:
    """`(year, quarter, area, kelp_cells, observed_cells)` in the parser's shape.

    `area` and `kelp_cells` are None for a quarter the parser found unobserved,
    which is what it hands over (docs/02).
    """
    built = [
        {
            "polygon_id": polygon_id,
            "year": year,
            "quarter": quarter,
            "kelp_area_m2": area,
            "n_cells_kelp": cells,
            "n_cells_observed": observed,
            "n_cells": footprint,
        }
        for year, quarter, area, cells, observed in rows
    ]
    return pd.DataFrame(built)


def build(rows, cfg=None, **kwargs):
    frame = rows if isinstance(rows, pd.DataFrame) else parsed(rows)
    return build_kelp(
        frame,
        cfg or config(),
        source="kelpwatch",
        revision=23,
        now=NOW,
        **kwargs,
    )


def quarter(frame: pd.DataFrame, year: int, quarter: int) -> pd.Series:
    match = frame.loc[(frame["year"] == year) & (frame["quarter"] == quarter)]
    assert len(match) == 1, f"expected one {year}Q{quarter} row, got {len(match)}"
    return match.iloc[0]


# --------------------------------------------------------------------------
# The two kinds of bad news
# --------------------------------------------------------------------------


def test_an_unobserved_quarter_keeps_its_row_its_null_and_its_counts():
    """It is the record of a cloud gap. Dropping it would make a hole in the
    series indistinguishable from a quarter the export never covered."""
    built = build([(2007, 1, None, None, 0), (2007, 2, 500.0, 5, 100)]).quarterly
    blind = quarter(built, 2007, 1)

    assert pd.isna(blind["kelp_area_m2"])
    assert pd.isna(blind["n_cells_kelp"])
    assert (blind["n_cells_observed"], blind["n_cells"]) == (0, 100)
    assert blind["pct_cells_observed"] == 0.0
    assert not blind["usable"]


def test_a_thinly_observed_quarter_keeps_its_value_and_is_flagged():
    """Hard rule 4 one layer up: flags, never deletions. It also leaves the floor
    a sensitivity knob rather than a filter already applied."""
    built = build([(2007, 1, 300.0, 3, 50)], config(floor=0.6)).quarterly
    thin = quarter(built, 2007, 1)

    assert thin["kelp_area_m2"] == 300.0  # the value survives
    assert thin["pct_cells_observed"] == 0.5
    assert not thin["usable"]


def test_an_observed_quarter_with_no_kelp_is_zero_and_usable():
    """The distinction the whole source turns on, at the far end of the pipeline:
    a bed that was seen and was empty is a measurement."""
    built = build([(2007, 1, 0.0, 0, 100)]).quarterly
    empty = quarter(built, 2007, 1)

    assert empty["kelp_area_m2"] == 0.0
    assert empty["pct_cells_observed"] == 1.0
    assert empty["usable"]


def test_the_floor_is_a_knob_and_moving_it_moves_the_verdict_not_the_value():
    rows = [(2007, 1, 300.0, 3, 50)]
    assert not build(rows, config(floor=0.6)).quarterly.iloc[0]["usable"]

    relaxed = build(rows, config(floor=0.4)).quarterly.iloc[0]
    assert relaxed["usable"]
    assert relaxed["kelp_area_m2"] == 300.0


# --------------------------------------------------------------------------
# Shape, bookkeeping and provenance
# --------------------------------------------------------------------------


def test_the_table_is_the_documented_columns_in_order():
    built = build([(2007, 1, 500.0, 5, 100)]).quarterly
    assert tuple(built.columns) == quarterly_kelp_columns()


def test_every_row_carries_the_revision_it_came_from():
    """The export has no version of its own, so this is the only place a number
    can be traced to a citable dataset (docs/02)."""
    built = build([(2007, 1, 500.0, 5, 100)]).quarterly
    assert set(built["kelp_watch_revision"]) == {23}
    assert set(built["source"]) == {"kelpwatch"}
    # Deliberately no fetch_run_id: on a derived table it would change every
    # build and cost the zone its byte-for-byte reproducibility.
    assert "fetch_run_id" not in built.columns


def test_a_finished_quarter_is_complete_and_the_current_one_is_not():
    built = build([(2026, 2, 500.0, 5, 100), (2026, 3, 500.0, 5, 100)]).quarterly
    assert quarter(built, 2026, 2)["quarter_complete"]
    assert not quarter(built, 2026, 3)["quarter_complete"]


def test_rows_come_back_in_calendar_order_per_polygon():
    frame = pd.concat(
        [
            parsed([(2008, 1, 1.0, 1, 100), (2007, 1, 2.0, 1, 100)], polygon_id="KELP:B"),
            parsed([(2007, 2, 3.0, 1, 100)], polygon_id="KELP:A"),
        ],
        ignore_index=True,
    )
    built = build(frame).quarterly
    assert built[["polygon_id", "year", "quarter"]].apply(tuple, axis=1).tolist() == [
        ("KELP:A", 2007, 2),
        ("KELP:B", 2007, 1),
        ("KELP:B", 2008, 1),
    ]


def test_two_builds_over_unchanged_input_are_identical():
    rows = [(2007, 1, 500.0, 5, 100), (2008, 1, 600.0, 6, 100)]
    assert build(rows).quarterly.equals(build(rows).quarterly)


def test_an_empty_input_yields_empty_tables_with_the_right_columns():
    outcome = build(parsed([]))
    assert outcome.quarterly.empty
    assert tuple(outcome.quarterly.columns) == quarterly_kelp_columns()
    assert tuple(outcome.climatology.columns) == climatology_columns(KELP_SERIES)


def test_a_polygon_with_no_historic_footprint_scores_zero_rather_than_dividing_by_it():
    built = build(parsed([(2007, 1, 0.0, 0, 0)], footprint=0)).quarterly
    assert built.iloc[0]["pct_cells_observed"] == 0.0
    assert not built.iloc[0]["usable"]


def test_two_rows_for_one_polygon_quarter_raise_rather_than_being_averaged():
    """Two exports of one bed read as one series. Averaging them would produce a
    plausible number from an incoherent input."""
    doubled = pd.concat(
        [parsed([(2007, 1, 500.0, 5, 100)]), parsed([(2007, 1, 900.0, 9, 100)])],
        ignore_index=True,
    )
    with pytest.raises(ValueError) as raised:
        build(doubled)
    assert "KELP:A 2007Q1" in str(raised.value)


# --------------------------------------------------------------------------
# The climatology, through the shared implementation
# --------------------------------------------------------------------------


def test_the_climatology_is_keyed_on_the_polygon_not_on_a_site():
    built = build([(2007, 1, 100.0, 1, 100), (2008, 1, 200.0, 2, 100), (2009, 1, 300.0, 3, 100)])
    assert tuple(built.climatology.columns) == climatology_columns(KELP_SERIES)
    assert CLIMATOLOGY_KELP_KEY == ("polygon_id", "quarter", "feature")
    assert set(built.climatology.columns) != set(CLIMATOLOGY_COLUMNS)  # not the env table's


def test_both_measured_quantities_get_a_baseline_and_an_anomaly():
    """Area is how much canopy there was; the cell count is how far it spread. A
    bed can thin without shrinking, so the notebook chooses."""
    built = build([(2007, 1, 100.0, 10, 100), (2008, 1, 200.0, 20, 100), (2009, 1, 300.0, 30, 100)])
    assert set(built.climatology["feature"]) == set(MEASURED)
    assert built.quarterly["kelp_area_m2_anom"].tolist() == [-100.0, 0.0, 100.0]
    assert built.quarterly["n_cells_kelp_anom"].tolist() == [-10.0, 0.0, 10.0]


def test_no_bookkeeping_column_gets_an_anomaly():
    built = build([(2007, 1, 100.0, 1, 100)])
    assert not {"n_cells_anom", "pct_cells_observed_anom", "usable_anom"} & set(
        built.quarterly.columns
    )
    assert not set(built.climatology["feature"]) & {"n_cells", "pct_cells_observed"}


def test_a_cloud_gapped_quarter_does_not_contribute_to_the_baseline():
    """It has no value to contribute -- but the point is that it also cannot
    drag the baseline it is later compared against."""
    rows = [
        (2007, 1, 100.0, 1, 100),
        (2008, 1, 200.0, 2, 100),
        (2009, 1, 300.0, 3, 100),
        (2010, 1, None, None, 0),
    ]
    built = build(rows)
    area = built.climatology.loc[built.climatology["feature"] == "kelp_area_m2"].iloc[0]
    assert area["baseline_mean"] == 200.0
    assert area["n_years"] == 3


def test_a_thin_quarter_does_not_drag_the_baseline_but_still_gets_an_anomaly():
    """`usable` is the single gate on this table, on both halves of the project."""
    rows = [
        (2007, 1, 100.0, 1, 100),
        (2008, 1, 200.0, 2, 100),
        (2009, 1, 300.0, 3, 100),
        (2010, 1, 900.0, 9, 10),
    ]
    built = build(rows)
    area = built.climatology.loc[built.climatology["feature"] == "kelp_area_m2"].iloc[0]
    assert (area["baseline_mean"], area["n_years"]) == (200.0, 3)

    thin = quarter(built.quarterly, 2010, 1)
    assert not thin["usable"]
    assert thin["kelp_area_m2_anom"] == 700.0


def test_two_polygons_keep_separate_baselines():
    frame = pd.concat(
        [
            parsed(
                [(2007, 1, 100.0, 1, 100), (2008, 1, 200.0, 2, 100), (2009, 1, 300.0, 3, 100)],
                polygon_id="KELP:A",
            ),
            parsed(
                [(2007, 1, 10.0, 1, 100), (2008, 1, 20.0, 2, 100), (2009, 1, 30.0, 3, 100)],
                polygon_id="KELP:B",
            ),
        ],
        ignore_index=True,
    )
    built = build(frame)
    area = built.climatology.loc[built.climatology["feature"] == "kelp_area_m2"]
    assert area.set_index("polygon_id")["baseline_mean"].to_dict() == {
        "KELP:A": 200.0,
        "KELP:B": 20.0,
    }


def test_appending_a_year_outside_the_window_moves_no_existing_anomaly():
    """The property the fixed baseline exists to guarantee, on the kelp half."""
    history = [(2007, 1, 100.0, 1, 100), (2008, 1, 200.0, 2, 100), (2009, 1, 300.0, 3, 100)]
    before = build(history).quarterly["kelp_area_m2_anom"].tolist()
    after = build([*history, (2024, 1, 5000.0, 50, 100)]).quarterly["kelp_area_m2_anom"].tolist()
    assert after[:3] == before


# --------------------------------------------------------------------------
# What the run reports
# --------------------------------------------------------------------------


def test_the_outcome_counts_quarters_observed_and_quarters_usable_separately():
    """A bed can be fully observed and mostly unusable, or the reverse."""
    rows = [
        (2007, 1, 100.0, 1, 100),
        (2007, 2, None, None, 0),
        (2007, 3, 300.0, 3, 30),
    ]
    (entry,) = build(rows).polygons

    assert entry.polygon_id == "KELP:A"
    assert (entry.quarters, entry.quarters_observed, entry.quarters_usable) == (3, 2, 1)
    assert (entry.first_quarter, entry.last_quarter) == ("2007Q1", "2007Q3")


def test_both_kinds_of_coverage_loss_are_reported_separately():
    rows = [
        (2007, 1, 100.0, 1, 100),
        (2007, 2, None, None, 0),
        (2007, 3, 300.0, 3, 30),
    ]
    warnings = build(rows).warnings

    assert any("no cloud-free observation" in w for w in warnings)
    assert any("coverage floor" in w and "run low" in w for w in warnings)


def test_a_fully_observed_polygon_reports_nothing():
    assert build([(2007, 1, 100.0, 1, 100)]).warnings == ()
