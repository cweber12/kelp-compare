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
import pytest

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


# --------------------------------------------------------------------------
# The two nearshore Waveriders (46254, 46266)
# --------------------------------------------------------------------------

#: The five columns both Waveriders carry data in. Everything else in the
#: stdmet layout is sentinel in every row of both files.
WAVERIDER_REPORTS = {"WVHT", "DPD", "APD", "MWD", "WTMP"}

WAVERIDERS = {
    "46254": (FIX / "46254h2015_excerpt.txt", FIX / "46254_realtime_excerpt.txt"),
    "46266": (FIX / "46266h2019_excerpt.txt", FIX / "46266_realtime_excerpt.txt"),
}

#: Where each station's stdmet archive actually begins -- the fact the archive
#: excerpts are cut from row 1 to preserve. Neither reaches back into the
#: 2007-2019 climatology baseline, which is what docs/04 s3 and ADR-007 are
#: about, and 46266's does not reach it at all.
ARCHIVE_STARTS = {"46254": (2015, 2, 12), "46266": (2019, 12, 6)}


def _sentinel_columns(path):
    """Which columns hold nothing but this layout's missing tokens."""
    names, _, frame = _read(path)
    missing = {"MM", "999.0", "99.0", "999", "99.00", "9999.0", "999.00", "99"}
    empty = set()
    for name in names:
        if name in ("YY", "MM", "DD", "hh", "mm"):
            continue
        if set(frame[name].unique()) <= missing:
            empty.add(name)
    return empty


@pytest.mark.parametrize("station", sorted(WAVERIDERS))
def test_a_waverider_reports_waves_and_water_temperature_and_nothing_else(station):
    """The opposite shape from LJAC1, and the reason these got their own fixtures.

    `sites.json` declares `measured_parameters` for these two from this fact
    rather than from the header, which lists every stdmet column whether the
    station has the sensor or not.
    """
    for path in WAVERIDERS[station]:
        names, _, _ = _read(path)
        reported = {n for n in names if n not in ("YY", "MM", "DD", "hh", "mm")}
        reported -= _sentinel_columns(path)
        assert reported == WAVERIDER_REPORTS, f"{path.name} reports {sorted(reported)}"


@pytest.mark.parametrize("station", sorted(WAVERIDERS))
def test_a_waverider_carries_no_air_temperature_or_wind(station):
    """The three parameters LJAC1 declares, none of which these two have."""
    for path in WAVERIDERS[station]:
        assert {"ATMP", "WSPD", "WDIR"} <= _sentinel_columns(path)


@pytest.mark.parametrize("station", sorted(WAVERIDERS))
def test_the_archive_excerpt_begins_where_the_station_record_begins(station):
    """Cut from row 1 on purpose: the first timestamp is the load-bearing fact.

    These two dates are why neither station can supply the 2007-2019 baseline
    (docs/04 s3). A window taken from the middle of the record would not show
    it, and 46266's start in particular is easy to misread -- its 2019 file
    covers 6-31 December, so "the record starts in 2019" is true and suggests a
    baseline year that does not exist.
    """
    archive, _ = WAVERIDERS[station]
    _, _, frame = _read(archive)
    first = _timestamps(frame).iloc[0]

    year, month, day = ARCHIVE_STARTS[station]
    assert (first.year, first.month, first.day) == (year, month, day)


@pytest.mark.parametrize("station", sorted(WAVERIDERS))
def test_the_waverider_water_temperature_is_never_a_sentinel(station):
    """No missing WTMP anywhere in either excerpt, so a parse that drops rows
    for the wrong reason has nowhere to hide behind a real gap."""
    for path in WAVERIDERS[station]:
        _, _, frame = _read(path)
        assert set(frame["WTMP"]) & {"MM", "999.0"} == set()
        assert frame["WTMP"].astype(float).between(5.0, 35.0).all()
