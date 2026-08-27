"""The Kelp Watch export parser (docs/02).

Two seams. The recorded fixtures, where the numbers are real and a reviewer can
check them against `tests/test_fixtures_kelpwatch.py`; and small hand-written
files for the refusals, where what matters is that a format surprise stops the
parse rather than entering the record.

The case this module exists for is the first one below. Every other test here is
scaffolding around it.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.fetchers import kelpwatch
from kelpcompare.polygons import Polygon

FIX = Path(__file__).parent / "fixtures" / "kelpwatch"
LAJOLLA = FIX / "kelp_lajolla.csv"
DELMAR = FIX / "kelp_delmar.csv"

HEADER = ",".join(kelpwatch.EXPORT_COLUMNS)


def polygon(polygon_id: str = "KELP:TEST", source_file: str = "test.csv") -> Polygon:
    return Polygon(
        polygon_id=polygon_id,
        purpose="control",
        site_ids=("NDBC:LJAC1",),
        source_file=source_file,
    )


def export(tmp_path: Path, *rows: str, header: str = HEADER) -> Path:
    """A hand-written export. Rows are `year,quarter,area,kelp,clear,footprint`."""
    target = tmp_path / "export.csv"
    target.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return target


def refuses(tmp_path: Path, *rows: str, header: str = HEADER) -> str:
    with pytest.raises(ValueError) as raised:
        kelpwatch.parse(export(tmp_path, *rows, header=header), polygon())
    return str(raised.value)


def quarter(frame: pd.DataFrame, year: int, quarter: int) -> pd.Series:
    match = frame.loc[(frame["year"] == year) & (frame["quarter"] == quarter)]
    assert len(match) == 1, f"expected one {year}Q{quarter} row, got {len(match)}"
    return match.iloc[0]


# --------------------------------------------------------------------------
# The rule the module exists for
# --------------------------------------------------------------------------


def test_an_unobserved_quarter_is_null_and_an_empty_one_is_zero():
    """Hard rule 3, against the two rows the export writes identically.

    Del Mar 1985Q1 saw nothing: no cloud-free cell in the whole footprint. Del
    Mar 1984Q1 was fully observed and held no kelp. The export writes
    `0,0,0,130` and `0,0,130,130`. One is a measurement and one is a hole, and
    only the cloud-free count says which.
    """
    parsed = kelpwatch.parse(DELMAR, polygon("KELP:DEL-MAR"))

    blind = quarter(parsed.frame, 1985, 1)
    assert blind["n_cells_observed"] == 0
    assert pd.isna(blind["kelp_area_m2"])
    assert pd.isna(blind["n_cells_kelp"])

    empty = quarter(parsed.frame, 1984, 1)
    assert empty["n_cells_observed"] == 130
    assert empty["kelp_area_m2"] == 0.0
    assert empty["n_cells_kelp"] == 0.0


def test_the_counts_that_make_the_null_auditable_survive_it():
    """`n_cells_observed = 0` is a fact about the quarter, and it is what makes
    the null readable as a cloud gap rather than as a mystery."""
    blind = quarter(kelpwatch.parse(DELMAR, polygon()).frame, 1985, 1)
    assert (blind["n_cells_observed"], blind["n_cells"]) == (0, 130)


def test_the_whole_marginal_bed_comes_out_with_both_kinds_of_zero_intact():
    """Del Mar is the bed where getting this wrong would be invisible: zero is
    the normal reading in 112 of its 170 quarters, so eight fabricated ones
    would look like nothing at all."""
    frame = kelpwatch.parse(DELMAR, polygon()).frame

    assert len(frame) == 170
    assert int(frame["kelp_area_m2"].isna().sum()) == 8
    assert int((frame["kelp_area_m2"] == 0).sum()) == 112


def test_a_partially_observed_quarter_keeps_its_value():
    """Observed is observed. The coverage floor is the feature stage's business,
    and applying it here would be a second gate on one warning."""
    partial = quarter(kelpwatch.parse(LAJOLLA, polygon()).frame, 1990, 4)
    assert partial["n_cells_observed"] == 979
    assert partial["n_cells"] == 8309
    assert partial["kelp_area_m2"] > 0


# --------------------------------------------------------------------------
# The derived row
# --------------------------------------------------------------------------


def test_the_max_rows_are_dropped_and_the_drop_is_reported():
    parsed = kelpwatch.parse(LAJOLLA, polygon("KELP:LA-JOLLA"))

    assert parsed.rows_in == 212
    assert parsed.max_rows_dropped == 42
    assert len(parsed.frame) == 170
    assert set(parsed.frame["quarter"]) == {1, 2, 3, 4}
    assert any("max" in warning for warning in parsed.warnings)


def test_the_max_row_never_reaches_the_frame_as_a_quarter():
    """1984's max row carries 112,905 m2 -- Q4's area -- beside Q1's cell count.
    Ingested as a quarter it would double-count the year's peak."""
    frame = kelpwatch.parse(LAJOLLA, polygon()).frame
    year = frame.loc[frame["year"] == 1984]

    assert len(year) == 4
    assert quarter(frame, 1984, 4)["kelp_area_m2"] == 112905.0
    assert float(year["kelp_area_m2"].sum()) == 3663.0 + 44812.0 + 0.0 + 112905.0


# --------------------------------------------------------------------------
# Shape and provenance
# --------------------------------------------------------------------------


def test_the_polygon_comes_from_the_caller_and_lands_on_every_row():
    """No export names the geometry it describes, so nothing here guesses one."""
    parsed = kelpwatch.parse(LAJOLLA, polygon("KELP:LA-JOLLA"))
    assert parsed.polygon_id == "KELP:LA-JOLLA"
    assert set(parsed.frame["polygon_id"]) == {"KELP:LA-JOLLA"}


def test_the_frame_is_the_documented_columns_in_order_and_typed():
    frame = kelpwatch.parse(LAJOLLA, polygon()).frame
    assert tuple(frame.columns) == kelpwatch.PARSED_COLUMNS
    assert str(frame["year"].dtype) == "int32"
    assert str(frame["quarter"].dtype) == "int8"
    # Nullable by being float, because a cloud-gapped quarter has no value.
    assert str(frame["kelp_area_m2"].dtype) == "float64"
    assert str(frame["n_cells_kelp"].dtype) == "float64"


def test_rows_come_back_in_calendar_order_and_span_the_record():
    parsed = kelpwatch.parse(LAJOLLA, polygon())
    assert (parsed.first_quarter, parsed.last_quarter) == ("1984Q1", "2026Q2")
    assert parsed.frame[["year", "quarter"]].apply(tuple, axis=1).is_monotonic_increasing


def test_two_parses_of_one_file_are_identical():
    """The table has to be a pure function of the export for `rebuild` to mean
    anything on this half too."""
    assert kelpwatch.parse(LAJOLLA, polygon()).frame.equals(
        kelpwatch.parse(LAJOLLA, polygon()).frame
    )


def test_sniff_recognises_an_export_by_its_header_and_nothing_else(tmp_path):
    assert kelpwatch.sniff(LAJOLLA)
    assert not kelpwatch.sniff(export(tmp_path, header="year,quarter,area"))
    assert not kelpwatch.sniff(tmp_path / "does-not-exist.csv")


# --------------------------------------------------------------------------
# Refusals -- a format surprise stops the parse (docs/02)
# --------------------------------------------------------------------------


def test_a_header_that_is_not_the_recorded_one_is_refused(tmp_path):
    extra = HEADER + ",count_cells_bull_kelp"
    message = refuses(tmp_path, "1984,1,0,0,10,10,0", header=extra)
    assert "docs/02 has not recorded" in message


def test_a_quarter_token_nobody_has_seen_is_refused(tmp_path):
    """Guessing whether it is a quarter would put an unknown aggregation into
    the response variable -- which is what the `max` row would have been."""
    message = refuses(tmp_path, "1984,1,0,0,10,10", "1984,annual,0,0,10,10")
    assert "annual" in message


def test_a_repeated_quarter_is_refused(tmp_path):
    message = refuses(tmp_path, "1984,1,0,0,10,10", "1984,1,5,1,10,10")
    assert "1984Q1" in message
    assert "more than once" in message


def test_a_footprint_that_moves_inside_one_file_is_refused(tmp_path):
    """It is the denominator of every coverage fraction, so a file where it
    moves is not one geometry's record."""
    message = refuses(tmp_path, "1984,1,0,0,10,10", "1984,2,0,0,10,99")
    assert "denominator" in message


def test_more_cloud_free_cells_than_the_footprint_holds_is_refused(tmp_path):
    assert "historic footprint" in refuses(tmp_path, "1984,1,0,0,11,10")


def test_more_kelp_cells_than_observed_cells_is_refused(tmp_path):
    """This is the check that would fire if `count_cells_no_clouds` really did
    mean the *unoccupied* habitat the field dictionary calls it."""
    message = refuses(tmp_path, "1984,1,900,5,3,10")
    assert "not the count this parser reads it as" in message


def test_more_area_than_the_kelp_cells_could_hold_is_refused(tmp_path):
    """A 30 m cell is 900 m2. More than that per kelp-bearing cell means the
    area column is not the fractional-cover sum this reads it as."""
    assert "900 m2" in refuses(tmp_path, "1984,1,1801,2,10,10")


def test_a_blank_in_a_column_that_decides_observation_is_refused(tmp_path):
    """The field dictionary says a cell can be blank. These four decide whether
    a quarter was observed at all, so a blank is a change to check rather than
    a value to read around."""
    assert "n_cells_observed" in refuses(tmp_path, "1984,1,0,0,,10")
    assert "n_cells" in refuses(tmp_path, "1984,1,0,0,10,")
    assert "year" in refuses(tmp_path, ",1,0,0,10,10")


def test_a_file_of_nothing_but_max_rows_is_refused(tmp_path):
    assert "every row was a derived" in refuses(tmp_path, "1984,max,0,0,10,10")


def test_a_blank_area_beside_a_real_observation_is_read_as_missing(tmp_path):
    """The one blank the dictionary describes that this parser can honour: the
    value is absent, which is what a cloud gap means anyway."""
    parsed = kelpwatch.parse(export(tmp_path, "1984,1,,0,10,10"), polygon())
    assert pd.isna(parsed.frame.iloc[0]["kelp_area_m2"])
    assert parsed.frame.iloc[0]["n_cells_observed"] == 10
