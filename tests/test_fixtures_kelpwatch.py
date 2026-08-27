"""Sanity checks on the reference Kelp Watch exports, pinning the docs/02 findings.

These pass before any parser exists; they define what the Kelp Watch adapter
must reproduce. Both files are whole real exports, so every quirk asserted here
is one the site actually emits.

The findings that matter are the two the published field dictionary gets wrong
or leaves ambiguous, and both are recorded here so a future format change breaks
a test rather than a result:

* **An unobserved quarter is written as `0`, not as a blank.** The dictionary
  says "cells with no numerical value correspond to instances when the scene was
  obstructed by clouds"; there is not one blank cell in either file. A quarter
  nobody could see and a quarter with genuinely no kelp are the same six
  characters apart from `count_cells_no_clouds`.
* **`count_cells_no_clouds` counts the whole footprint**, not the part of it
  without kelp. The dictionary calls it cells "within the unoccupied kelp
  habitat"; if that were literal it would exclude the kelp-bearing cells, and it
  would sometimes be smaller than `count_cells_kelp`. It never is.
"""

from pathlib import Path

import pandas as pd

FIX = Path(__file__).parent / "fixtures" / "kelpwatch"
LAJOLLA = FIX / "kelp_lajolla.csv"
DELMAR = FIX / "kelp_delmar.csv"

#: The header the export writes, in order. Read rather than assumed by the
#: parser, but pinned here so a column added upstream is a failing test.
COLUMNS = [
    "year",
    "quarter",
    "kelp_area_m2",
    "count_cells_kelp",
    "count_cells_no_clouds",
    "count_cells_historic_footprint",
]

_NUMERIC = [c for c in COLUMNS if c != "quarter"]


def _read(path: Path) -> pd.DataFrame:
    """Every cell as text, with pandas' own NA tokens left alone.

    `keep_default_na=False` is the point of the exercise: it is the only way to
    tell a blank cell from a zero, which is the distinction both of the findings
    above turn on.
    """
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _quarters(path: Path) -> pd.DataFrame:
    frame = _read(path)
    real = frame.loc[frame["quarter"] != "max"].copy()
    for column in _NUMERIC:
        real[column] = pd.to_numeric(real[column])
    real["quarter"] = real["quarter"].astype(int)
    return real


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def test_the_export_layout_is_one_header_line_and_no_preamble():
    for path in (LAJOLLA, DELMAR):
        raw = path.read_bytes()
        assert b"\r\n" not in raw, f"{path.name} should use LF endings"
        assert raw.splitlines()[0].decode() == ",".join(COLUMNS)
        assert list(_read(path).columns) == COLUMNS


def test_the_export_names_no_geometry_anywhere_in_the_file():
    """Which bed a file describes lives only in its filename, which is why the
    polygon registry has to declare it (docs/03)."""
    for path in (LAJOLLA, DELMAR):
        assert not set(_read(path).columns) - set(COLUMNS)


def test_both_files_cover_the_same_span_of_quarters():
    for path in (LAJOLLA, DELMAR):
        frame = _read(path)
        real = _quarters(path)
        assert len(frame) == 212
        assert len(real) == 170
        assert (real["year"].min(), real["year"].max()) == (1984, 2026)
        assert f"{real['year'].iloc[0]}Q{real['quarter'].iloc[0]}" == "1984Q1"
        assert f"{real['year'].iloc[-1]}Q{real['quarter'].iloc[-1]}" == "2026Q2"


def test_the_in_progress_year_has_no_max_row():
    """2026 has published Q1 and Q2 only, and no growing-season maximum yet --
    so the `max` rows cannot be relied on to enumerate the years either."""
    frame = _read(LAJOLLA)
    maxima = frame.loc[frame["quarter"] == "max", "year"].astype(int)
    assert len(maxima) == 42
    assert maxima.max() == 2025
    assert set(_quarters(LAJOLLA).loc[lambda f: f["year"] == 2026, "quarter"]) == {1, 2}


# --------------------------------------------------------------------------
# The `max` row is derived, and is not a quarter
# --------------------------------------------------------------------------


def test_the_max_row_is_a_column_wise_maximum_not_the_peak_quarters_row():
    """So it cannot be read as a fifth quarter, and cannot be read as "the best
    quarter" either: its cell counts come from a different quarter than its area.

    La Jolla 1984 is the case in one year. Q4 is the peak area at 112,905 m2 on
    5,426 observed cells, but the `max` row reports 8,309 observed cells, which
    is Q1's and Q2's figure.
    """
    frame = _read(LAJOLLA)
    row = frame.loc[(frame["year"] == "1984") & (frame["quarter"] == "max")].iloc[0]
    assert row["kelp_area_m2"] == "112905"
    assert row["count_cells_no_clouds"] == "8309"

    quarters = _quarters(LAJOLLA).loc[lambda f: f["year"] == 1984]
    peak = quarters.loc[quarters["kelp_area_m2"].idxmax()]
    assert (peak["quarter"], peak["kelp_area_m2"]) == (4, 112905)
    assert peak["count_cells_no_clouds"] == 5426  # not the 8309 the max row claims


def test_every_year_but_the_last_carries_exactly_one_max_row():
    for path in (LAJOLLA, DELMAR):
        frame = _read(path)
        maxima = frame.loc[frame["quarter"] == "max"]
        assert len(maxima) == maxima["year"].nunique() == 42


# --------------------------------------------------------------------------
# Missing is written as zero -- the finding the whole parser turns on
# --------------------------------------------------------------------------


def test_there_is_not_one_blank_cell_in_either_export():
    """The published field dictionary says an obstructed scene has "no numerical
    value". It has a zero."""
    for path in (LAJOLLA, DELMAR):
        frame = _read(path)
        for column in COLUMNS:
            assert not (frame[column].str.strip() == "").any(), f"{path.name}/{column}"


def test_an_unobserved_quarter_is_written_exactly_like_an_empty_one():
    """The trap, stated as an assertion: `kelp_area_m2` alone cannot tell them
    apart, and `count_cells_no_clouds` is the only thing that can."""
    delmar = _quarters(DELMAR)
    unobserved = delmar.loc[delmar["count_cells_no_clouds"] == 0]
    observed_empty = delmar.loc[
        (delmar["count_cells_no_clouds"] > 0) & (delmar["kelp_area_m2"] == 0)
    ]

    assert len(unobserved) == 8
    assert len(observed_empty) == 112
    # Identical in the value column, and in the kelp-cell count beside it.
    assert set(unobserved["kelp_area_m2"]) == set(observed_empty["kelp_area_m2"]) == {0}
    assert set(unobserved["count_cells_kelp"]) == set(observed_empty["count_cells_kelp"]) == {0}


def test_an_unobserved_quarter_never_reports_kelp():
    """The rule the parser relies on, checked rather than assumed: zero observed
    cells always means zero area, so nulling the value loses no measurement."""
    for path in (LAJOLLA, DELMAR):
        frame = _quarters(path)
        blind = frame.loc[frame["count_cells_no_clouds"] == 0]
        assert (blind["kelp_area_m2"] == 0).all()
        assert (blind["count_cells_kelp"] == 0).all()


def test_the_unobserved_quarters_lean_winter_as_docs_04_warns():
    """Cloud-gap missingness is seasonally biased, so fabricated zeros would not
    scatter -- they would pile up in Q4 and Q1 and read as a seasonal signal.

    Six of La Jolla's seven blind quarters are Q1 or Q4. The seventh, 1984Q3,
    is why this asserts a lean rather than a rule: the bias is a tendency across
    the record, not a property of any one quarter, and a parser that assumed
    winter-only would be wrong about a real hole in the data.
    """
    unobserved = _quarters(LAJOLLA).loc[lambda f: f["count_cells_no_clouds"] == 0]
    labels = [f"{r.year}Q{r.quarter}" for r in unobserved.itertuples()]

    assert labels == ["1984Q3", "1985Q1", "1987Q4", "1988Q4", "1990Q1", "1991Q4", "1995Q4"]
    assert sum(1 for q in unobserved["quarter"] if q in (1, 4)) == 6


# --------------------------------------------------------------------------
# The cell counts, and what coverage can be built from them
# --------------------------------------------------------------------------


def test_the_cloud_free_count_covers_the_whole_footprint_not_the_empty_part():
    """The dictionary's word is "unoccupied"; if that were literal this would
    sometimes fall below the kelp-bearing count. It never does."""
    for path in (LAJOLLA, DELMAR):
        frame = _quarters(path)
        assert (frame["count_cells_no_clouds"] >= frame["count_cells_kelp"]).all()
        assert (frame["count_cells_no_clouds"] <= frame["count_cells_historic_footprint"]).all()


def test_the_historic_footprint_is_constant_within_a_file():
    """It is the denominator every coverage fraction is taken against, so a
    footprint that moved between rows would make those fractions incomparable."""
    assert set(_quarters(LAJOLLA)["count_cells_historic_footprint"]) == {8309}
    assert set(_quarters(DELMAR)["count_cells_historic_footprint"]) == {130}


def test_the_fixtures_bracket_the_coverage_floor_from_both_sides():
    """0.6 is the configured floor; a fixture that only held full quarters would
    not exercise it."""
    lajolla = _quarters(LAJOLLA)
    coverage = lajolla["count_cells_no_clouds"] / lajolla["count_cells_historic_footprint"]
    labelled = dict(
        zip(lajolla["year"].astype(str) + "Q" + lajolla["quarter"].astype(str), coverage)
    )

    assert round(labelled["1984Q4"], 3) == 0.653  # above the floor
    assert round(labelled["1990Q4"], 3) == 0.118  # below it, but observed
    assert round(labelled["1985Q1"], 3) == 0.0  # not observed at all


def test_area_is_fractional_cover_and_not_derivable_from_the_cell_count():
    """A 30 m cell is 900 m2, but a cell counted as kelp is only partly kelp --
    so the area column carries information the counts do not."""
    for path in (LAJOLLA, DELMAR):
        frame = _quarters(path)
        assert (frame["kelp_area_m2"] <= frame["count_cells_kelp"] * 900).all()
        occupied = frame.loc[frame["count_cells_kelp"] > 0]
        per_cell = occupied["kelp_area_m2"] / occupied["count_cells_kelp"]
        assert 0 < per_cell.mean() < 900
