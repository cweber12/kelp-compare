"""Sanity checks on the reference NDBC payloads, pinning the docs/02 findings.

These pass before any fetcher code exists; they define what `fetchers/ndbc.py`
must reproduce. Both files are verbatim excerpts of real LJAC1 payloads --
contiguous row windows with the two header lines kept -- so every quirk asserted
here is one the station actually emits, not one invented for a test.

What the two files are for:

* `ljac1h2023_excerpt.txt` -- the 2023 standard meteorological archive, around
  the largest water-temperature step of that year. Numeric all-nines sentinels,
  ascending time, no `PTDY` column.
* `LJAC1_realtime_excerpt.txt` -- the realtime feed, around a run of missing
  water temperatures. `MM` sentinels, **descending** time, an extra `PTDY`
  column, and `VIS` in different units than the archive reports it.

The layouts differ in three ways at once, which is why docs/02 says to parse
them separately rather than treat realtime as a short archive file.
"""

from pathlib import Path

import pandas as pd

FIX = Path(__file__).parent / "fixtures" / "ndbc"
ARCHIVE = FIX / "ljac1h2023_excerpt.txt"
REALTIME = FIX / "LJAC1_realtime_excerpt.txt"

#: NDBC writes the column's field width and precision as all nines. The token
#: therefore differs per column, and reading one as a measurement puts a 999 °C
#: water temperature into the record (docs/02 "NDBC" quirks).
ARCHIVE_SENTINELS = {"WTMP": "999.0", "ATMP": "999.0", "WVHT": "99.00", "MWD": "999"}


def _read(path: Path) -> tuple[list[str], list[str], pd.DataFrame]:
    lines = path.read_text(encoding="utf-8").splitlines()
    names = lines[0].lstrip("#").split()
    units = lines[1].lstrip("#").split()
    frame = pd.read_csv(path, sep=r"\s+", skiprows=2, names=names, dtype=str)
    return names, units, frame


def _timestamps(frame: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(
        {
            "year": frame["YY"].astype(int),
            "month": frame["MM"].astype(int),
            "day": frame["DD"].astype(int),
            "hour": frame["hh"].astype(int),
            "minute": frame["mm"].astype(int),
        },
        utc=True,
    )


def test_archive_layout_and_units():
    names, units, frame = _read(ARCHIVE)
    assert names[:5] == ["YY", "MM", "DD", "hh", "mm"]
    assert "PTDY" not in names  # the archive layout has no pressure tendency
    assert len(frame) == 400

    # The columns this project maps to parameters, with the units the file
    # itself declares. Read, never assumed: NDBC is not SI throughout.
    declared = dict(zip(names, units))
    assert declared["WTMP"] == "degC"
    assert declared["ATMP"] == "degC"
    assert declared["WVHT"] == "m"
    assert declared["DPD"] == "sec"
    assert declared["WSPD"] == "m/s"


def test_archive_is_ascending_and_carries_numeric_sentinels():
    _, _, frame = _read(ARCHIVE)
    stamps = _timestamps(frame)
    assert stamps.is_monotonic_increasing
    assert str(stamps.iloc[0]) == "2023-06-20 17:06:00+00:00"
    assert str(stamps.iloc[-1]) == "2023-06-22 11:24:00+00:00"

    for column, sentinel in ARCHIVE_SENTINELS.items():
        assert (frame[column] == sentinel).any(), f"{column} should show its {sentinel} sentinel"

    water = pd.to_numeric(frame["WTMP"])
    assert (water == 999.0).sum() == 5  # missing, and 999 °C if read as a value
    valid = water[water != 999.0]
    assert (valid.min(), valid.max()) == (13.0, 18.7)


def test_archive_holds_the_step_the_qc_thresholds_have_to_survive():
    """The reason this window was chosen (issue #4, docs/04 §1).

    A 2.9 °C move between samples 13 minutes apart. The same 2.9 °C step occurs
    across a strictly 6-minute pair elsewhere in the 2023 record, which is
    29 °C/h against an 18 °C/h suspect threshold.
    """
    _, _, frame = _read(ARCHIVE)
    water = pd.to_numeric(frame["WTMP"])
    series = pd.Series(water.values, index=_timestamps(frame)).mask(water.values == 999.0).dropna()

    step = series.diff().abs()
    assert round(step.max(), 2) == 2.9
    assert str(step.idxmax()) == "2023-06-21 15:30:00+00:00"


def test_realtime_layout_differs_from_the_archive():
    names, units, frame = _read(REALTIME)
    assert "PTDY" in names  # present in realtime, absent from the archive
    assert len(frame) == 300

    realtime_units = dict(zip(names, units))
    archive_names, archive_units, _ = _read(ARCHIVE)
    archive_declared = dict(zip(archive_names, archive_units))

    assert realtime_units["WTMP"] == archive_declared["WTMP"] == "degC"
    # Same measurement, different unit token in the two layouts -- the proof
    # that the units row has to be read rather than assumed from the column.
    assert (realtime_units["VIS"], archive_declared["VIS"]) == ("nmi", "mi")


def test_realtime_is_newest_first_and_uses_mm():
    _, _, frame = _read(REALTIME)
    stamps = _timestamps(frame)
    assert stamps.is_monotonic_decreasing  # newest row first, oldest last
    assert str(stamps.iloc[0]) == "2026-08-23 05:48:00+00:00"
    assert str(stamps.iloc[-1]) == "2026-08-21 23:54:00+00:00"

    assert (frame["WTMP"] == "MM").sum() == 3
    assert not (frame["WTMP"] == "999.0").any()  # realtime never uses the numeric form

    water = pd.to_numeric(frame["WTMP"], errors="coerce")
    assert (water.min(), water.max()) == (16.0, 21.8)
    assert frame["PTDY"].str.startswith(("+", "-")).any()  # signed, unlike every other column
