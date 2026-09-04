"""The window-generic reduction (docs/04 s2), independent of any one table.

`test_features_quarterly.py` pins the arithmetic against the Kelp Watch calendar
and `test_features_deployment.py` pins it against a deployment. What is tested
here is only what the two share and neither owns: the difference a half-open
window makes against a closed one.

Both differences bite in the same place -- an edge -- and both were defects the
quarterly table shipped when a deployment was forced through a quarter's window.
"""

from __future__ import annotations

import pandas as pd
import pytest

from kelpcompare.features.config import ParameterFeatures
from kelpcompare.features.windowed import (
    Window,
    columns_for,
    reduce_window,
    without_day_based,
)

TEMPERATURE = ParameterFeatures(
    parameter="sea_water_temperature",
    feature_set="temperature",
    thresholds={"days_above": (20.0,), "max_spell_above": (20.0,)},
)


def series(stamps, values) -> pd.DataFrame:
    return pd.DataFrame(
        {"timestamp": pd.to_datetime(stamps, utc=True), "value": [float(v) for v in values]}
    )


def reduce(frame: pd.DataFrame, window: Window, warnings: list[str] | None = None) -> dict:
    return reduce_window(
        frame,
        window=window,
        entry=TEMPERATURE,
        coverage_floor=0.6,
        label="test",
        warnings=warnings if warnings is not None else [],
    )


# --------------------------------------------------------------------------
# The coverage denominator
# --------------------------------------------------------------------------


def test_a_closed_window_expects_one_more_observation_than_a_half_open_one():
    """A 10-minute logger down for exactly one hour returns seven samples, not six.

    The `+1` is the whole difference between the two window kinds, and getting it
    wrong is not cosmetic: it puts every healthy deployment fractionally over full
    coverage.
    """
    start, end = pd.Timestamp("2026-07-11T00:00Z"), pd.Timestamp("2026-07-11T01:00Z")
    assert Window(start, end).expected_obs(600.0) == 6.0
    assert Window(start, end, inclusive_end=True).expected_obs(600.0) == 7.0


def test_a_full_closed_window_reads_as_full_coverage_and_warns_about_nothing():
    """The regression the `+1` exists for: without it this clamps and warns."""
    stamps = pd.date_range("2026-07-11T00:00Z", "2026-07-11T01:00Z", freq="10min")
    warnings: list[str] = []
    row = reduce(
        series(stamps, [15.0] * len(stamps)),
        Window(stamps[0], stamps[-1], inclusive_end=True),
        warnings,
    )
    assert row["n_obs"] == 7
    assert row["expected_obs"] == 7.0
    assert row["pct_coverage"] == 1.0
    assert row["usable"]
    assert warnings == []


def changed_cadence() -> tuple[pd.DataFrame, Window]:
    """One hour at a minute, then a hundred hours at an hour.

    The median interval is an hour, so 160 observations land against 101 expected
    and the coverage has to be clamped -- the case the warning exists for.
    """
    stamps = [
        *pd.date_range("2026-07-01T00:00Z", periods=60, freq="min"),
        *pd.date_range("2026-07-01T02:00Z", periods=100, freq="h"),
    ]
    frame = series(stamps, [15.0] * len(stamps))
    return frame, Window(pd.Timestamp("2026-07-01T00:00Z"), stamps[-1])


def test_a_cadence_that_changed_is_clamped_rather_than_reported_above_full():
    frame, window = changed_cadence()
    warnings: list[str] = []
    row = reduce(frame, window, warnings)
    assert row["n_obs"] == 160
    assert row["expected_obs"] == 101.0
    assert row["pct_coverage"] == 1.0
    assert warnings


def test_the_cadence_warning_names_the_callers_window():
    """A manifest saying "mid-deployment" says which table to go and look at."""
    frame, window = changed_cadence()

    warnings: list[str] = []
    reduce(frame, window, warnings)
    assert any("mid-window" in warning for warning in warnings)

    warnings = []
    reduce_window(
        frame,
        window=window,
        entry=TEMPERATURE,
        coverage_floor=0.6,
        label="test",
        warnings=warnings,
        noun="deployment",
    )
    assert any("mid-deployment" in warning for warning in warnings)


# --------------------------------------------------------------------------
# The spell boundary -- the defect that motivated the seam
# --------------------------------------------------------------------------


def days(first: str, count: int, value: float) -> pd.DataFrame:
    """One reading a day, all at the same value, starting at `first`."""
    stamps = pd.date_range(f"{first}T12:00Z", periods=count, freq="D")
    return series(stamps, [value] * count)


def test_a_spell_running_to_the_window_edge_is_not_a_floor():
    """A run ended by the window boundary is a limitation of the window, not a
    hole in the record, so it is reported as a measurement."""
    frame = days("2026-07-11", 5, 22.0)
    window = Window(
        pd.Timestamp("2026-07-11T08:00Z"), pd.Timestamp("2026-07-15T20:00Z"), inclusive_end=True
    )
    row = reduce(frame, window)
    assert row["max_spell_above_20c_days"] == 5.0
    assert not row["max_spell_above_20c_gap_interrupted"]


def test_the_same_spell_against_a_wider_window_is_a_floor():
    """The defect, stated as a test. Widen the window to the calendar quarter and
    the unobserved days before the deployment turn the same five warm days into a
    spell that "may have been longer" -- which is false, because the logger was
    not in the water."""
    frame = days("2026-07-11", 5, 22.0)
    quarter = Window(pd.Timestamp("2026-07-01T00:00Z"), pd.Timestamp("2026-10-01T00:00Z"))
    row = reduce(frame, quarter)
    assert row["max_spell_above_20c_days"] == 5.0
    assert row["max_spell_above_20c_gap_interrupted"]


def test_a_genuine_gap_inside_the_window_is_still_a_floor():
    """The marker must keep working for the case it was built for: a day nobody
    measured, inside the window, between two qualifying runs."""
    frame = pd.concat([days("2026-07-11", 3, 22.0), days("2026-07-15", 3, 22.0)])
    window = Window(
        pd.Timestamp("2026-07-11T00:00Z"), pd.Timestamp("2026-07-17T23:00Z"), inclusive_end=True
    )
    row = reduce(frame, window)
    assert row["max_spell_above_20c_days"] == 3.0
    assert row["max_spell_above_20c_gap_interrupted"]


def test_a_run_ended_by_a_cool_observed_day_is_a_measurement():
    """An observed day that simply did not qualify ends a spell honestly."""
    frame = pd.concat([days("2026-07-11", 3, 22.0), days("2026-07-14", 1, 15.0)])
    window = Window(
        pd.Timestamp("2026-07-11T00:00Z"), pd.Timestamp("2026-07-14T23:00Z"), inclusive_end=True
    )
    row = reduce(frame, window)
    assert row["max_spell_above_20c_days"] == 3.0
    assert not row["max_spell_above_20c_gap_interrupted"]


# --------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inclusive", "expected"),
    [(False, [True, True, False]), (True, [True, True, True])],
)
def test_only_a_closed_window_contains_its_final_instant(inclusive: bool, expected: list[bool]):
    stamps = pd.Series(
        pd.to_datetime(["2026-07-11T00:00Z", "2026-07-11T00:30Z", "2026-07-11T01:00Z"], utc=True)
    )
    window = Window(stamps[0], stamps[2], inclusive_end=inclusive)
    assert list(window.contains(stamps)) == expected


# --------------------------------------------------------------------------
# Band occupancy, the one feature here that is not day-based
# --------------------------------------------------------------------------

BANDED = ParameterFeatures(
    parameter="sea_water_temperature",
    feature_set="temperature",
    thresholds={"days_above": (20.0,), "days_below": (14.0,), "time_in_band": ((14.0, 20.0),)},
)


def banded(values: list[float]) -> dict:
    """One reading every ten minutes from the top of an hour, reduced over it."""
    stamps = pd.date_range("2026-07-11T00:00Z", periods=len(values), freq="10min")
    window = Window(stamps[0], stamps[-1], inclusive_end=True)
    return reduce_window(
        series(stamps, values),
        window=window,
        entry=BANDED,
        coverage_floor=0.6,
        label="test",
        warnings=[],
    )


def test_the_band_column_is_named_from_both_of_its_edges():
    """ADR-006's rename-on-retune rule has to hold when either edge moves."""
    assert "frac_in_band_14c_20c" in banded([15.0, 16.0])


def test_the_band_is_the_fraction_of_observations_inside_it():
    assert banded([13.0, 15.0, 16.0, 21.0])["frac_in_band_14c_20c"] == 0.5


def test_the_band_is_closed_at_both_edges():
    """Exactly `low` and exactly `high` are inside, which is what makes the band
    the exact complement of the two strict tail tests beside it."""
    assert banded([14.0, 20.0])["frac_in_band_14c_20c"] == 1.0


def test_the_band_and_the_two_tails_partition_the_value_axis():
    """No reading belongs to none of them -- the reason the band is closed.

    A single day holding one reading at each edge and one beyond each: the band
    holds the two edge readings, and the two day-based counts each fire on the
    day, so nothing in the day is unaccounted for.
    """
    reduced = banded([13.0, 14.0, 20.0, 21.0])
    assert reduced["frac_in_band_14c_20c"] == 0.5
    assert reduced["days_below_14c"] == 1.0
    assert reduced["days_above_20c"] == 1.0


def test_a_window_whose_every_row_failed_qc_gets_a_null_band_not_a_zero():
    """A zero would read as "the water was never in the band", which is a claim."""
    window = Window(pd.Timestamp("2026-07-11T00:00Z"), pd.Timestamp("2026-07-11T01:00Z"))
    reduced = reduce_window(
        series([], []),
        window=window,
        entry=BANDED,
        coverage_floor=0.6,
        label="test",
        warnings=[],
    )
    assert reduced["frac_in_band_14c_20c"] is None


def test_a_sub_day_window_keeps_the_band_and_drops_the_day_based_counts():
    """`days_above_20c` over an hour is 0 or 1; occupancy over an hour is real."""
    kept = without_day_based(BANDED)
    assert kept.of("time_in_band") == ((14.0, 20.0),)
    assert kept.of("days_above") == ()
    assert kept.of("days_below") == ()


def test_dropping_the_day_based_kinds_drops_their_columns_too():
    names = [name for name, _ in columns_for(without_day_based(BANDED))]
    assert "frac_in_band_14c_20c" in names
    assert not [name for name in names if name.startswith(("days_", "max_spell_", "degree_days_"))]
