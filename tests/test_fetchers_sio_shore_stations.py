"""The SIO Shore Stations parser (docs/02 "SIO Shore Stations").

Three fixtures, and the split between them is the point. Two are real archives
and pin the *format* -- and there are two of them because the format differs
between snapshots, which is the thing a parser written against one file gets
wrong. The hand-built one pins the *edge cases* the source has not produced in
110 years: the two flag codes its own legend declares but has never written, a
reading with no flag beside it, and midnight.

That mirrors the HOBO and RTOMS pairs and exists for the same reason: a fixture
edited to contain an edge case can no longer prove what the source actually
sends, so it must not be the only one.

There is no network seam to stub here. The archive cannot be pulled at all, so
`parse` reads a file exactly the way an operator's ingest does.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.cli import SOURCE_NAMES
from kelpcompare.fetchers import sio_shore_stations as sio
from kelpcompare.parameters import load_parameters
from kelpcompare.qc.flags import parse_tests
from kelpcompare.storage import (
    FLAG_FAIL,
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    FLAG_PASS,
    FLAG_SUSPECT,
    OBSERVATION_COLUMNS,
    validate_frame,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "sio_shore_stations"
PINNED = FIXTURES / "lajolla_temp_excerpt.csv"
OLDER = FIXTURES / "lajolla_temp_2020_archive_excerpt.csv"
EDGES = FIXTURES / "lajolla_temp_edge-cases.csv"

SITE = "SIO:LAJOLLA-PIER"
RUN = "20260828T000000000Z-ingest"

#: What `sites.json` declares for SIO:LAJOLLA-PIER.
DECLARED = (0.5, 5.0)
MEASURED = ("sea_water_temperature",)


@pytest.fixture
def parameters():
    return load_parameters(
        REPO_ROOT / "data" / "registry" / "parameters.json", sources=SOURCE_NAMES
    )


def parse(parameters, path: Path = PINNED, *, declared=DECLARED, measured=MEASURED):
    return sio.parse(
        path,
        parameters,
        site_id=SITE,
        declared_depths=declared,
        measured_parameters=measured,
        run_id=RUN,
    )


def edited(tmp_path: Path, old: str, new: str, *, source: Path = PINNED) -> Path:
    """The fixture with one substitution -- so a test says what it changed."""
    text = source.read_bytes().decode("utf-8-sig")
    assert old in text, f"{old!r} is not in {source.name}; the fixture has moved"
    target = tmp_path / source.name
    target.write_bytes(text.replace(old, new, 1).encode("utf-8-sig"))
    return target


def at(frame: pd.DataFrame, day: str, depth: float) -> pd.Series:
    """The one row for a local calendar day at one depth."""
    same_day = frame.timestamp.dt.tz_convert(None) - pd.Timedelta(hours=8)
    rows = frame.loc[(same_day.dt.date == pd.Timestamp(day).date()) & (frame.depth_m == depth)]
    assert len(rows) == 1, f"{day} at {depth} m: {len(rows)} rows"
    return rows.iloc[0]


# --------------------------------------------------------------------------
# Recognising the file
# --------------------------------------------------------------------------


def test_both_recorded_archives_are_recognised_despite_their_differences():
    """45- and 46-line preambles, Mac Roman and UTF-8, one `sniff`."""
    assert sio.sniff(PINNED)
    assert sio.sniff(OLDER)
    assert sio.sniff(EDGES)


def test_the_salinity_twin_in_the_same_download_is_rejected(tmp_path):
    """It shares the preamble, the legend and seven of the nine columns."""
    salt = edited(
        tmp_path,
        "SURF_TEMP_C,SURF_FLAG,BOT_TEMP_C,BOT_FLAG",
        "SURF_SAL_PSU,SURF_FLAG,BOT_SAL_PSU,BOT_FLAG",
    )
    assert not sio.sniff(salt)


def test_a_file_with_no_column_header_is_not_ours(tmp_path):
    other = tmp_path / "something.csv"
    other.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    assert not sio.sniff(other)


def test_sniff_says_no_rather_than_raising_on_a_file_it_cannot_open(tmp_path):
    assert not sio.sniff(tmp_path / "does-not-exist.csv")


# --------------------------------------------------------------------------
# The preamble, which is what the registry is checked against
# --------------------------------------------------------------------------


def test_the_header_carries_everything_the_registry_is_compared_with():
    header = sio.read_header(PINNED)

    assert header.archived == "2026-06-30"
    assert header.station == "La Jolla, Scripps Pier"
    assert header.depths_m == {"surface": 0.5, "bottom": 5.0}
    assert header.declared_depths == (0.5, 5.0)
    assert header.flag_codes == (0, 1, 2, 3, 4, 5)
    assert header.award == "C22820005"
    assert header.doi == "10.6075/J06T0K0M"


def test_the_position_is_read_through_the_degree_sign_in_either_encoding():
    """The one byte that differs between the two encodings, and it is skipped."""
    pinned, older = sio.read_header(PINNED), sio.read_header(OLDER)

    assert pinned.lat == pytest.approx(32.866944, abs=1e-6)
    assert pinned.lon == pytest.approx(-117.257139, abs=1e-6)
    assert (older.lat, older.lon) == (pinned.lat, pinned.lon)


def test_the_older_archive_declares_its_own_date_and_award():
    """Which is what makes the pin checkable rather than merely asserted."""
    header = sio.read_header(OLDER)
    assert header.archived == "2022-07-07"
    assert header.award == "C1670003"


def test_a_position_matches_only_the_site_that_is_actually_there():
    header = sio.read_header(PINNED)

    assert sio.position_matches(header, 32.866944, -117.257139)
    assert sio.position_matches(header, 32.867, -117.2571)  # rounded, still this pier

    # NDBC:LJAC1's published position, about 10 m away but rounded to 3 places.
    assert sio.position_matches(header, 32.867, -117.257)
    # The next Shore Stations station up the coast is tens of kilometres away.
    assert not sio.position_matches(header, 33.6, -117.9)
    assert not sio.position_matches(header, None, None)
    assert not sio.position_matches(header, 32.866944, None)


@pytest.mark.parametrize(
    ("old", "new", "why"),
    [
        ("were archived 2026-06-30", "were archived recently", "archived"),
        ("Shore Stations Program - La Jolla", "Shore Stations Programme", "title"),
        ('32°52\'01.0""N 117°15\'25.7""W', "somewhere off California", "position"),
        ("Surface (~0.5m)", "Surface", "depth"),
    ],
    ids=["archive date", "title line", "position", "nominal depth"],
)
def test_a_preamble_field_this_module_cannot_read_stops_the_parse(tmp_path, old, new, why):
    """Each one is either compared against the registry or written into the
    record, so a default would be a fact invented rather than read."""
    with pytest.raises(ValueError):
        sio.read_header(edited(tmp_path, old, new))


def test_a_legend_code_this_project_cannot_map_stops_the_parse(tmp_path, parameters):
    """The RTOMS rule -- a provider's flag vocabulary changing needs a human --
    applied at the header, where this source declares it. A code first met at
    row 30,000 is a run that has already done its work."""
    with pytest.raises(ValueError, match=r"legend declares code\(s\) \[6\]"):
        parse(parameters, edited(tmp_path, "5 = sample collected", "6 = sample collected"))


# --------------------------------------------------------------------------
# The recorded archives
# --------------------------------------------------------------------------


def test_one_day_becomes_two_observations_at_two_depths(parameters):
    parsed = parse(parameters)
    frame = parsed.frame

    assert list(frame.columns) == list(OBSERVATION_COLUMNS)
    validate_frame(frame)
    assert set(frame.depth_m) == {0.5, 5.0}
    assert set(frame.site_id) == {SITE}
    assert set(frame.source) == {sio.SOURCE}
    assert set(frame.parameter) == {"sea_water_temperature"}
    assert set(frame.fetch_run_id) == {RUN}


def test_the_manifest_hears_the_archive_and_the_station(parameters):
    parsed = parse(parameters)
    assert parsed.layout == "2026-06-30"
    assert parsed.station == "La Jolla, Scripps Pier"
    assert parsed.rows_in == 33


def test_rows_are_sorted_by_time_then_depth(parameters):
    frame = parse(parameters).frame
    assert frame[["timestamp", "depth_m"]].equals(
        frame[["timestamp", "depth_m"]].sort_values(["timestamp", "depth_m"])
    )


def test_the_years_before_the_bottom_series_started_are_dropped_not_flagged(parameters):
    """A null before a series began is a series that did not exist; a null after
    it began is an outage and stays in the record (docs/02)."""
    frame = parse(parameters).frame
    bottom = frame.loc[frame.depth_m == 5.0]
    surface = frame.loc[frame.depth_m == 0.5]

    # The excerpt carries twelve days before 1926-07-21 and the bottom series
    # has none of them; the surface series has every day in the file.
    assert len(surface) == 33
    assert len(bottom) == 21
    assert bottom.timestamp.min().date() == pd.Timestamp("1926-07-21").date()

    # And the outage that comes after it does stay, flagged missing.
    outage = at(frame, "1926-08-15", 5.0)
    assert pd.isna(outage.value)
    assert outage.qc_flag == FLAG_MISSING


def test_an_absent_reading_is_missing_whatever_the_flag_column_says(parameters):
    """The archive writes 0 -- good data -- beside 1,330 absent surface readings.
    docs/03 gives 9 to a row with no value, and the verdict says so too, so the
    roll-up and the record agree rather than one being patched onto the other."""
    row = at(parse(parameters).frame, "1930-01-07", 0.5)

    assert pd.isna(row.value)
    assert row.qc_flag == FLAG_MISSING
    assert parse_tests(row.qc_tests)[sio.SOURCE_FLAG_TEST] == "missing"


@pytest.mark.parametrize(
    ("day", "depth", "flag"),
    [
        ("2005-08-28", 0.5, FLAG_SUSPECT),  # SURF_FLAG 1, illegible entry
        ("1996-01-15", 0.5, FLAG_SUSPECT),  # SURF_FLAG 2, differs from other sources
        ("1996-02-04", 0.5, FLAG_SUSPECT),  # SURF_FLAG 3, data uncertain
        ("1997-07-19", 5.0, FLAG_SUSPECT),  # BOT_FLAG 1
        ("2026-03-31", 0.5, FLAG_PASS),  # flag 0
    ],
)
def test_the_programs_own_flags_map_into_the_docs_03_vocabulary(parameters, day, depth, flag):
    row = at(parse(parameters).frame, day, depth)
    assert row.qc_flag == flag
    assert pd.notna(row.value)


def test_the_landed_flag_histogram_is_the_one_docs_02_records(parameters):
    """The excerpt in miniature; docs/02 carries the whole-archive figures."""
    assert parse(parameters).flag_counts == {"1": 35, "3": 12, "9": 7}


# --------------------------------------------------------------------------
# Time: PST, the convention, and how an imputed timestamp is identified
# --------------------------------------------------------------------------


def test_a_recorded_time_is_read_as_pst_and_stored_as_utc(parameters):
    """1343 PST on the last day of the pinned archive -> 21:43 UTC."""
    row = at(parse(parameters).frame, "2026-03-31", 0.5)
    assert row.timestamp == pd.Timestamp("2026-03-31 21:43", tz="UTC")


def test_pst_is_a_fixed_offset_and_not_a_daylight_saving_zone(parameters):
    """A summer day. `America/Los_Angeles` would put this an hour earlier, which
    on a daily series is indistinguishable from a diurnal signal."""
    row = at(parse(parameters).frame, "1990-09-11", 0.5)
    local = row.timestamp - pd.Timedelta(hours=8)
    assert local.strftime("%H%M") == "1630"


def test_a_time_of_day_with_three_digits_is_hhmm_not_a_number(parameters):
    """`524` is 05:24, the earliest sample in the whole record -- and would be
    85:8 or 5.24 hours to anything that read it another way."""
    row = at(parse(parameters).frame, "2020-05-07", 0.5)
    assert (row.timestamp - pd.Timedelta(hours=8)).strftime("%H%M") == "0524"


def test_midnight_is_a_time_rather_than_a_missing_one(parameters):
    """`TIME_PST = 0`. Reading it as absent would silently impute over a real
    time, and 00:00 PST is the one local time that lands on the same UTC day."""
    row = at(parse(parameters, EDGES).frame, "2024-01-07", 0.5)
    assert row.timestamp == pd.Timestamp("2024-01-07 08:00", tz="UTC")
    assert sio.SAMPLE_TIME_TEST in parse_tests(row.qc_tests)


def test_a_day_with_no_time_takes_the_documented_convention(parameters):
    """10:38 PST -- the median of the days that carry one (docs/02)."""
    row = at(parse(parameters).frame, "1916-08-22", 0.5)
    assert row.timestamp == pd.Timestamp("1916-08-23 00:00", tz="UTC") - pd.Timedelta(
        hours=5, minutes=22
    )
    assert (row.timestamp - pd.Timedelta(hours=8)).time() == sio.NOMINAL_LOCAL_TIME


def test_the_convention_keeps_the_utc_date_equal_to_the_local_date():
    """PST is -08:00, so a nominal time from 16:00 would roll every imputed
    reading onto the next UTC day -- and a 31 December one into the next
    quarter. Measured times do that legitimately; an assigned one must not."""
    assert sio.NOMINAL_LOCAL_TIME < time(16, 0)


def test_an_imputed_timestamp_is_identified_by_carrying_no_sample_time_verdict(parameters):
    """The whole mechanism, and it needs no column docs/03 does not have: a test
    that reached no verdict records nothing, and there was no time to check."""
    frame = parse(parameters).frame

    imputed = at(frame, "1916-08-22", 0.5)
    assert sio.SAMPLE_TIME_TEST not in parse_tests(imputed.qc_tests)

    measured = at(frame, "1990-01-01", 0.5)
    assert parse_tests(measured.qc_tests)[sio.SAMPLE_TIME_TEST] == "pass"


def test_a_post_1990_day_with_no_time_is_imputed_despite_its_good_time_flag(parameters):
    """All 766 of these carry `TIME_FLAG = 0`, "good data", about a time that is
    not there -- so the time column decides, not the flag column."""
    row = at(parse(parameters).frame, "1990-08-06", 0.5)
    assert (row.timestamp - pd.Timedelta(hours=8)).time() == sio.NOMINAL_LOCAL_TIME
    assert sio.SAMPLE_TIME_TEST not in parse_tests(row.qc_tests)


def test_a_disputed_time_makes_the_observation_suspect(parameters):
    """An observation is a reading *and* a time, so the source telling us the
    time is illegible is a verdict about the row. 133 days of 40,034."""
    row = at(parse(parameters).frame, "1992-11-05", 0.5)  # TIME_FLAG 1
    assert row.qc_flag == FLAG_SUSPECT
    assert parse_tests(row.qc_tests)[sio.SAMPLE_TIME_TEST] == "suspect"
    assert parse_tests(row.qc_tests)[sio.SOURCE_FLAG_TEST] == "pass"


def test_the_imputation_is_reported_rather_than_silent(parameters):
    parsed = parse(parameters)
    assert any("10:38 PST" in w and "18 of 33" in w for w in parsed.warnings), parsed.warnings


# --------------------------------------------------------------------------
# The cases the source has never emitted
# --------------------------------------------------------------------------


def test_flag_five_lands_failed_rather_than_dropped_or_refused(parameters):
    """docs/02: the code cannot tell a sample taken here for another program
    from one taken somewhere else, so the row stays on the record and out of the
    default `qc_flag <= 2` filter -- what docs/06 s3 does to a reading taken
    outside its deployment window, and for the same reason."""
    frame = parse(parameters, EDGES).frame

    surface = at(frame, "2024-01-03", 0.5)
    assert surface.qc_flag == FLAG_FAIL
    assert parse_tests(surface.qc_tests)[sio.SOURCE_FLAG_TEST] == "fail"
    assert surface.value == 16.1, "the reading is kept, not deleted (hard rule 4)"

    # And only that series: the other depth on the same day is untouched.
    assert at(frame, "2024-01-03", 5.0).qc_flag == FLAG_PASS


def test_landing_a_flag_five_reading_says_so_by_date(parameters):
    """Zero rows in all fourteen recorded snapshots, which is exactly why the
    warning exists: the first run that ever exercises this decision should put
    it in front of the operator rather than apply it silently."""
    warnings = parse(parameters, EDGES).warnings

    surface = next(w for w in warnings if "surface reading(s) carry source flag 5" in w)
    assert "2024-01-03" in surface
    assert "First time this has ever fired" in surface
    assert any("bottom reading(s) carry source flag 5" in w for w in warnings)

    # The real archive never triggers it.
    assert not any("source flag 5" in w for w in parse(parameters).warnings)


def test_the_leaky_bottle_flag_is_suspect_even_though_it_is_a_salinity_condition(parameters):
    row = at(parse(parameters, EDGES).frame, "2024-01-05", 0.5)
    assert row.qc_flag == FLAG_SUSPECT
    assert parse_tests(row.qc_tests)[sio.SOURCE_FLAG_TEST] == "suspect"


def test_a_reading_with_no_flag_records_no_verdict_about_the_reading(parameters):
    """Never observed in 110 years. The reading is kept -- an unflagged value is
    still a value -- and nothing is invented about it.

    The row still rolls up to `pass` here, because the `sample_time` verdict
    beside it is the only one that reached a conclusion. That is docs/03's
    roll-up applied literally, and it is harmless: both 1 and 2 pass the default
    `qc_flag <= 2` filter, so the label differs and the analysis does not.
    """
    row = at(parse(parameters, EDGES).frame, "2024-01-06", 0.5)

    assert row.value == 15.6
    assert sio.SOURCE_FLAG_TEST not in parse_tests(row.qc_tests)
    assert parse_tests(row.qc_tests)[sio.SAMPLE_TIME_TEST] == "pass"


def test_a_flag_code_that_arrives_without_being_declared_stops_the_parse(tmp_path, parameters):
    """`_check_legend` catches a vocabulary change the preamble announces; this
    catches one that arrives in the data without being announced, which is worse
    because nothing in the file admits to it."""
    with pytest.raises(ValueError, match=r"SURF_FLAG carries code\(s\) \[7\]"):
        parse(
            parameters, edited(tmp_path, "1916,8,22,NaN,NaN,19.5,0,", "1916,8,22,NaN,NaN,19.5,7,")
        )


# --------------------------------------------------------------------------
# Depths, which are permanent
# --------------------------------------------------------------------------


def test_the_depths_come_from_the_file_and_are_checked_against_the_registry(parameters):
    frame = parse(parameters).frame
    assert sorted(set(frame.depth_m)) == [0.5, 5.0]


def test_a_depth_the_registry_has_not_declared_stops_the_parse(tmp_path, parameters):
    """Refused rather than warned, unlike the RTOMS equivalent: there are exactly
    two series here, so an undeclared depth is the file disagreeing with the
    registry about where this station measures -- and `depth_m` is part of
    OBSERVATION_KEY, so landing it would be permanent."""
    resounded = edited(tmp_path, "Bottom (~5m)", "Bottom (~6m)")
    with pytest.raises(ValueError, match="sensor_depths_m does not declare"):
        parse(parameters, resounded)


def test_an_undeclared_registry_uses_the_files_depths_and_says_so(parameters):
    """An unrecorded fact must not quietly become missing data -- the rule
    `measured_parameters` follows."""
    parsed = parse(parameters, declared=())
    assert sorted(set(parsed.frame.depth_m)) == [0.5, 5.0]
    assert any("declares no depths" in w for w in parsed.warnings)


# --------------------------------------------------------------------------
# Layout surprises
# --------------------------------------------------------------------------


def test_trailing_filler_rows_are_dropped(parameters):
    """Five of the fourteen snapshots end with rows that are nothing but commas."""
    parsed = parse(parameters, EDGES)
    assert parsed.rows_in == 6, "the filler row is not a day"
    assert len(parsed.frame) == 12


def test_a_half_written_date_stops_the_parse(tmp_path, parameters):
    """A row with no date at all is padding; a row with part of one is a format
    change, and reading around it would invent a day."""
    with pytest.raises(ValueError, match="date this parser cannot read"):
        parse(parameters, edited(tmp_path, "1916,8,22,NaN", "1916,,22,NaN"))


def test_a_repeated_day_stops_the_parse(tmp_path, parameters):
    """One row per calendar day; two readings this parser cannot tell apart."""
    with pytest.raises(ValueError, match="appear more than once"):
        parse(parameters, edited(tmp_path, "1916,8,23,", "1916,8,22,"))


def test_a_time_that_is_not_an_hhmm_stops_the_parse(tmp_path, parameters):
    with pytest.raises(ValueError, match="not an HHMM time"):
        parse(parameters, edited(tmp_path, "2026,3,31,1343,", "2026,3,31,2575,"))


def test_a_column_inserted_upstream_stops_the_parse(tmp_path, parameters):
    with pytest.raises(ValueError, match="expected the archive columns"):
        parse(
            parameters, edited(tmp_path, "TIME_PST,TIME_FLAG,SURF", "TIME_PST,WEEK,TIME_FLAG,SURF")
        )


def test_a_trailing_column_that_carries_a_value_stops_the_parse(tmp_path, parameters):
    """They are padding in every row of every snapshot; a populated one is data
    arriving in a column docs/02 has not recorded."""
    with pytest.raises(ValueError, match="trailing column"):
        parse(
            parameters,
            edited(
                tmp_path,
                "1916,8,22,NaN,NaN,19.5,0,NaN,NaN,,,,,",
                "1916,8,22,NaN,NaN,19.5,0,NaN,NaN,7,,,,",
            ),
        )


# --------------------------------------------------------------------------
# What the registry can turn off
# --------------------------------------------------------------------------


def test_a_station_that_does_not_declare_temperature_lands_nothing(parameters):
    parsed = parse(parameters, measured=("water_level",))
    assert parsed.frame.empty
    assert list(parsed.frame.columns) == list(OBSERVATION_COLUMNS)
    assert any("measured_parameters" in w for w in parsed.warnings)


def test_an_undeclared_station_lands_its_readings(parameters):
    """Undeclared means nobody has checked, never "measures nothing"."""
    assert len(parse(parameters, measured=()).frame) == 54


def test_a_parameter_the_registry_does_not_define_stops_the_parse(tmp_path, parameters):
    empty = tmp_path / "parameters.json"
    empty.write_text('{"parameters": {"water_level": {"unit": "m"}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="sea_water_temperature"):
        parse(load_parameters(empty))


# --------------------------------------------------------------------------
# The flag mapping docs/02 publishes
# --------------------------------------------------------------------------


def test_every_legend_code_the_source_declares_has_a_mapping():
    """The table in docs/02, checked against the file's own legend rather than
    copied. A code the program declares and this project cannot place is the
    thing `_check_legend` refuses, so the two must not drift."""
    assert set(sio.read_header(PINNED).flag_codes) == set(sio.FLAG_MEANING)
    assert set(sio.STATUS_BY_SOURCE_FLAG) == set(sio.FLAG_MEANING)
    assert sio.STATUS_BY_SOURCE_FLAG == {
        0: FLAG_PASS,
        1: FLAG_SUSPECT,
        2: FLAG_SUSPECT,
        3: FLAG_SUSPECT,
        4: FLAG_SUSPECT,
        5: FLAG_FAIL,
    }
    assert FLAG_NOT_EVALUATED not in sio.STATUS_BY_SOURCE_FLAG.values()
