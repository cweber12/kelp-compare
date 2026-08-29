"""Sanity checks on the recorded SIO Shore Stations excerpts, pinning docs/02.

These pass before any parser exists; they define what the adapter must cope
with. Every assertion here is about bytes the archive actually emits, so a
format change upstream breaks a test rather than a result.

Four findings carry the weight, and all four are things a parser written
against one snapshot would get wrong:

* **The preamble is not a fixed length.** 46 lines in the pinned 2026-06-30
  archive, 45 in the 2022-07-07 one. A constant skip reads the column header as
  a data row on five of the fourteen snapshots.
* **The encoding is not fixed either.** The newer archives are UTF-8 with a
  byte-order mark; the older ones are Mac Roman with none, and a strict UTF-8
  decode raises on the degree sign in the position line.
* **The absent bottom flag is the series' start marker.** For the first ten
  years the file carries a row a day with `BOT_TEMP_C` *and* `BOT_FLAG` empty.
  That is the source saying the bottom series did not exist, not that a sample
  was missed, and it is the evidence the parser drops those rows on.
* **`TIME_FLAG` says nothing about whether a time exists.** A day with no
  `TIME_PST` still carries `TIME_FLAG = 0`, "good data", about a time that is
  not there.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import pandas as pd
import pytest

FIX = Path(__file__).parent / "fixtures" / "sio_shore_stations"
PINNED = FIX / "lajolla_temp_excerpt.csv"
OLDER = FIX / "lajolla_temp_2020_archive_excerpt.csv"
EDGES = FIX / "lajolla_temp_edge-cases.csv"

ALL = (PINNED, OLDER, EDGES)

#: The columns the archive writes, in order. Read rather than assumed by the
#: parser, but pinned here so a column inserted upstream is a failing test.
COLUMNS = [
    "YEAR",
    "MONTH",
    "DAY",
    "TIME_PST",
    "TIME_FLAG",
    "SURF_TEMP_C",
    "SURF_FLAG",
    "BOT_TEMP_C",
    "BOT_FLAG",
]

#: How the parser finds the column header, since it cannot count preamble lines.
HEADER_PREFIX = "YEAR,MONTH,DAY,TIME_PST"


def _text(path: Path) -> str:
    """Decode the way the parser has to: UTF-8 if it can, latin-1 if it cannot.

    latin-1 cannot raise, which is the point -- the only non-ASCII byte in any
    of these files is the degree sign in the position line, and nothing reads it.
    """
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _split(path: Path) -> tuple[list[str], str, list[str]]:
    """`(preamble, column header, data lines)` -- filler rows dropped.

    The preamble comes back as the *first CSV field* of each line rather than
    the raw line. Every preamble line is one quoted field followed by thirteen
    empty ones, and the quoting matters: the title carries a comma and the
    position carries doubled quote marks around its N and W. Reading them raw
    is how a parser comes to think the position line has no position on it.
    """
    lines = _text(path).splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(HEADER_PREFIX))
    preamble = [row[0] if row else "" for row in csv.reader(lines[:index])]
    data = [line for line in lines[index + 1 :] if line.strip(",")]
    return preamble, lines[index], data


def _frame(path: Path) -> pd.DataFrame:
    _, header, data = _split(path)
    frame = pd.read_csv(io.StringIO("\n".join([header, *data])))
    frame["date"] = pd.to_datetime({"year": frame.YEAR, "month": frame.MONTH, "day": frame.DAY})
    return frame


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def test_every_file_carries_a_preamble_then_the_recorded_column_header():
    for path in ALL:
        preamble, header, data = _split(path)
        assert preamble, f"{path.name} has no preamble"
        assert data, f"{path.name} has no data rows"
        assert header.split(",")[: len(COLUMNS)] == COLUMNS, path.name


def test_the_preamble_length_differs_between_archives():
    """The finding a fixed skip would get wrong on five of fourteen snapshots."""
    assert len(_split(PINNED)[0]) == 46
    assert len(_split(OLDER)[0]) == 45


def test_the_encoding_differs_between_archives():
    """UTF-8 with a BOM in the newer archives, Mac Roman with none in the older."""
    newer, older = PINNED.read_bytes(), OLDER.read_bytes()

    assert newer.startswith(b"\xef\xbb\xbf")
    assert not older.startswith(b"\xef\xbb\xbf")

    newer.decode("utf-8-sig")  # must not raise
    with pytest.raises(UnicodeDecodeError):
        older.decode("utf-8")

    # The one byte that differs, and the only reason the fallback is needed: the
    # degree sign of the position line.
    assert 0xA1 in older
    assert b"\xc2\xb0" in newer


def test_every_row_carries_five_empty_trailing_columns():
    """Present in every row of every snapshot, empty in all of them."""
    for path in ALL:
        _, header, data = _split(path)
        assert header.split(",")[len(COLUMNS) :] == [""] * 5, path.name
        for line in data:
            assert line.split(",")[len(COLUMNS) :] == [""] * 5, (path.name, line)


def test_line_endings_are_crlf():
    for path in ALL:
        raw = path.read_bytes()
        assert raw.count(b"\r\n") == raw.count(b"\n"), path.name


def test_the_edge_case_file_carries_a_trailing_filler_row():
    """Five of the fourteen snapshots end with rows that are nothing but commas."""
    lines = _text(EDGES).splitlines()
    assert not lines[-1].strip(",")


# --------------------------------------------------------------------------
# What the preamble declares
# --------------------------------------------------------------------------


def test_the_preamble_declares_the_archive_date_the_pin_is_checked_against():
    dates = {}
    for path in (PINNED, OLDER):
        preamble = "\n".join(_split(path)[0])
        found = re.search(r"archived (\d{4}-\d{2}-\d{2})", preamble)
        assert found, path.name
        dates[path.name] = found.group(1)

    assert dates[PINNED.name] == "2026-06-30"
    # A different archive, which is exactly what the pin has to reject.
    assert dates[OLDER.name] == "2022-07-07"


def test_the_preamble_declares_the_two_nominal_depths():
    """Surface and bottom come out of the title line, not out of this repository."""
    for path in ALL:
        title = _split(path)[0][1]
        assert "Surface (~0.5m)" in title, path.name
        assert "Bottom (~5m)" in title, path.name


def test_the_preamble_declares_the_position_and_it_is_the_same_in_both_archives():
    positions = []
    for path in (PINNED, OLDER):
        line = _split(path)[0][2]
        # Any non-digit run separates the fields, so the degree sign -- the one
        # byte that differs between encodings -- is never read.
        found = re.search(r"(\d+)\D+(\d+)'([\d.]+)\"N\s+(\d+)\D+(\d+)'([\d.]+)\"W", line)
        assert found, (path.name, line)
        positions.append(found.groups())

    assert positions[0] == positions[1]
    assert positions[0] == ("32", "52", "01.0", "117", "15", "25.7")


def test_the_preamble_declares_the_flag_legend_and_the_funding_award():
    pinned = "\n".join(_split(PINNED)[0])
    for code, meaning in (
        ("NaN", "data not collected"),
        ("0", "good data"),
        ("1", "illegible entry"),
        ("2", "data differs from other sources"),
        ("3", "data uncertain"),
        ("4", "leaky bottle"),
        ("5", "sample collected as part of SIO Pier Chlorophyll Program"),
    ):
        assert f"{code} = {meaning}" in pinned, code

    # The award changes between archives, which is why the citation is pinned
    # per archive rather than per program (docs/03).
    assert "Award# C22820005" in pinned
    assert "Award# C1670003" in "\n".join(_split(OLDER)[0])


def test_the_preamble_declares_the_timezone_and_when_times_exist():
    pinned = "\n".join(_split(PINNED)[0])
    assert "Time records in Pacific Standard time zone (PST)." in pinned
    assert "Time of sample collection available for 1990-current data only." in pinned
    assert "may vary by up to 1 hour" in pinned


# --------------------------------------------------------------------------
# What the rows say
# --------------------------------------------------------------------------


def test_the_recorded_excerpt_spans_the_whole_pinned_record():
    frame = _frame(PINNED)
    assert frame.date.min() == pd.Timestamp("1916-08-22")
    assert frame.date.max() == pd.Timestamp("2026-03-31")
    assert frame.date.is_monotonic_increasing
    assert not frame.date.duplicated().any()


def test_an_absent_bottom_flag_marks_the_years_before_the_bottom_series_started():
    """The evidence the parser drops pre-start rows on (docs/02).

    Not merely that the value is null: the *flag* column is empty too, and only
    there. A null under a flag is a sample nobody took; a null under no flag is
    a series that did not exist.
    """
    frame = _frame(PINNED)
    started = pd.Timestamp("1926-07-21")

    assert (frame.BOT_FLAG.isna() == (frame.date < started)).all()
    assert frame.loc[frame.date < started, "BOT_TEMP_C"].isna().all()
    assert frame.SURF_FLAG.notna().all(), "the surface series runs from the first row"


def test_an_absent_reading_can_still_carry_a_good_flag():
    """1,330 surface and 2,256 bottom readings do this in the pinned archive."""
    frame = _frame(PINNED)
    surface = frame.loc[frame.date == pd.Timestamp("1930-01-07")].iloc[0]
    assert pd.isna(surface.SURF_TEMP_C) and surface.SURF_FLAG == 0

    bottom = frame.loc[frame.date == pd.Timestamp("1926-08-15")].iloc[0]
    assert pd.isna(bottom.BOT_TEMP_C) and bottom.BOT_FLAG == 0


def test_times_exist_only_from_1990_and_are_hhmm_without_a_leading_zero():
    frame = _frame(PINNED)
    timed = frame.loc[frame.TIME_PST.notna()]

    assert timed.YEAR.min() >= 1990
    assert frame.loc[frame.YEAR < 1990, "TIME_PST"].isna().all()

    hhmm = timed.TIME_PST.astype(int)
    assert (hhmm % 100 <= 59).all(), "minutes"
    assert (hhmm // 100 <= 23).all(), "hours"
    # 05:24 is the earliest sample in the whole record, and it has three digits.
    assert hhmm.min() == 524


def test_a_day_with_no_time_still_carries_a_good_time_flag():
    """So TIME_FLAG cannot be used to detect a missing time -- TIME_PST can."""
    frame = _frame(PINNED)
    row = frame.loc[frame.date == pd.Timestamp("1990-08-06")].iloc[0]
    assert pd.isna(row.TIME_PST)
    assert row.TIME_FLAG == 0


def test_a_measured_time_can_cross_the_utc_day_boundary():
    """PST is -08:00, so any sample from 16:00 rolls into the next UTC date."""
    frame = _frame(PINNED)
    row = frame.loc[frame.date == pd.Timestamp("1990-09-11")].iloc[0]
    assert row.TIME_PST >= 1600


def test_the_real_archive_only_ever_writes_flags_zero_through_three():
    """Measured across all fourteen snapshots; 4 and 5 are legend-only.

    Which is why the flag-5 rule lives in the hand-built file: it is a rule for
    a case the source has not produced in 110 years.
    """
    frame = _frame(PINNED)
    for column in ("SURF_FLAG", "BOT_FLAG", "TIME_FLAG"):
        seen = {int(v) for v in frame[column].dropna().unique()}
        assert seen <= {0, 1, 2, 3}, (column, seen)
        assert seen == {0, 1, 2, 3}, f"the excerpt should carry every flag {column} emits"


def test_the_edge_case_file_carries_the_flags_the_source_never_has():
    frame = _frame(EDGES)
    assert set(frame.SURF_FLAG.dropna().astype(int)) == {0, 4, 5}
    assert set(frame.BOT_FLAG.dropna().astype(int)) == {0, 5}

    # A reading with no flag beside it, which the real archive never emits.
    orphan = frame.loc[frame.SURF_FLAG.isna()].iloc[0]
    assert pd.notna(orphan.SURF_TEMP_C)

    # Midnight: the one time-of-day HHMM cannot spell with four digits.
    assert (frame.TIME_PST == 0).sum() == 1
