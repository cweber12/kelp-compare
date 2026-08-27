"""The Kelp Watch quarter in UTC (docs/04 s2, hard rule 2).

The arithmetic is calendar quarters and would barely be worth testing. The
timezone is the part with consequences, so most of what is asserted here is that
UTC is what actually gets used -- including the case where using local time
would give a different answer, which is the only way to tell the two apart.
"""

from __future__ import annotations

import pandas as pd
import pytest

from kelpcompare.features.quarters import (
    QUARTERS,
    is_complete,
    quarter_bounds,
    quarter_label,
    quarter_of,
    quarter_seconds,
    shift_quarters,
    year_of,
)


def utc(*stamps) -> pd.Series:
    return pd.Series(pd.to_datetime(list(stamps), utc=True))


# --------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quarter", "start", "end"),
    [
        (1, "2007-01-01", "2007-04-01"),
        (2, "2007-04-01", "2007-07-01"),
        (3, "2007-07-01", "2007-10-01"),
        (4, "2007-10-01", "2008-01-01"),
    ],
)
def test_the_quarters_are_the_kelp_watch_calendar(quarter, start, end):
    assert quarter_bounds(2007, quarter) == (
        pd.Timestamp(start, tz="UTC"),
        pd.Timestamp(end, tz="UTC"),
    )


def test_quarters_are_not_all_the_same_length():
    """Which is why coverage divides by a computed duration, not a nominal one."""
    days = {q: quarter_seconds(2007, q) / 86400 for q in (1, 2, 3, 4)}
    assert days == {1: 90, 2: 91, 3: 92, 4: 92}


def test_a_leap_year_lengthens_q1():
    assert quarter_seconds(2008, 1) / 86400 == 91


def test_a_quarter_that_does_not_exist_is_refused():
    with pytest.raises(ValueError, match="not a Kelp Watch quarter"):
        quarter_bounds(2007, 5)


def test_the_label_is_the_compact_form():
    assert quarter_label(2007, 1) == "2007Q1"


# --------------------------------------------------------------------------
# UTC, and what that costs
# --------------------------------------------------------------------------


def test_an_instant_lands_in_exactly_one_quarter():
    """Bounds are half-open: the instant that opens Q2 belongs to Q2 alone."""
    boundary = utc("2007-03-31T23:59:59Z", "2007-04-01T00:00:00Z")
    assert list(quarter_of(boundary)) == [1, 2]
    assert list(year_of(boundary)) == [2007, 2007]


def test_a_new_year_boundary_moves_the_year_as_well_as_the_quarter():
    stamps = utc("2007-12-31T23:59:59Z", "2008-01-01T00:00:00Z")
    assert list(zip(year_of(stamps), quarter_of(stamps), strict=True)) == [(2007, 4), (2008, 1)]


def test_a_west_coast_new_years_eve_evening_falls_in_the_following_q1():
    """The stated consequence of hard rule 2, asserted rather than assumed.

    5pm on 31 December in Los Angeles is 01:00 on 1 January UTC. Under
    site-local quarters this reading would be Q4; under UTC it is the next Q1,
    and the whole project agrees on which because nothing downstream knows what
    zone the site is in.
    """
    local = pd.Series(pd.to_datetime(["2007-12-31 17:00"]).tz_localize("America/Los_Angeles"))
    assert list(quarter_of(local.dt.tz_convert("UTC"))) == [1]
    assert list(year_of(local.dt.tz_convert("UTC"))) == [2008]


def test_daylight_saving_is_irrelevant_rather_than_handled():
    """Spring forward is 2am PST -> 3am PDT on 2007-03-11.

    The local clock skips an hour, so a local-time quarter would have to decide
    what to do about a day that is 23 hours long. UTC just keeps counting, and
    nothing in the quarter arithmetic notices -- which is the claim, so it is
    asserted rather than assumed.
    """
    across = utc("2007-03-11T09:00:00Z", "2007-03-11T10:00:00Z", "2007-03-11T11:00:00Z")
    local = across.dt.tz_convert("America/Los_Angeles")
    assert [stamp.hour for stamp in local] == [1, 3, 4]  # 2am never happened locally
    assert list(quarter_of(across)) == [1, 1, 1]
    assert (across.diff().dropna() == pd.Timedelta(hours=1)).all()


def test_a_naive_timestamp_column_is_refused_rather_than_quartered():
    naive = pd.Series(pd.to_datetime(["2007-01-01 00:00"]))
    with pytest.raises(ValueError, match="hard rule 2"):
        quarter_of(naive)


def test_a_timestamp_column_in_another_zone_is_refused():
    local = pd.Series(pd.to_datetime(["2007-01-01 00:00"]).tz_localize("America/Los_Angeles"))
    with pytest.raises(ValueError, match="hard rule 2"):
        quarter_of(local)


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------


def test_a_quarter_is_complete_once_it_has_ended():
    assert is_complete(2007, 1, pd.Timestamp("2007-04-01T00:00:00Z"))
    assert is_complete(2007, 1, pd.Timestamp("2026-08-26T00:00:00Z"))


def test_the_quarter_a_run_happens_in_is_not_complete():
    """2026 Q3 measured 0.479 coverage because it had not finished, not because
    anything was missing. A genuine 2018 outage measured 0.539."""
    assert not is_complete(2026, 3, pd.Timestamp("2026-08-26T00:00:00Z"))


def test_completeness_needs_a_timezone_aware_run_timestamp():
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        is_complete(2007, 1, pd.Timestamp("2007-04-01"))


# --------------------------------------------------------------------------
# Shifting, for the comparison table's lags
# --------------------------------------------------------------------------


def test_shifting_back_crosses_the_year_boundary_rather_than_inventing_a_quarter_zero():
    assert shift_quarters(2020, 1, -1) == (2019, 4)
    assert shift_quarters(2020, 1, -4) == (2019, 1)
    assert shift_quarters(2020, 1, -5) == (2018, 4)


def test_shifting_by_nothing_is_the_quarter_itself():
    for quarter in QUARTERS:
        assert shift_quarters(2020, quarter, 0) == (2020, quarter)


def test_shifting_forward_works_too_even_though_lags_only_go_back():
    assert shift_quarters(2020, 4, 1) == (2021, 1)


def test_shifting_is_reversible():
    """The property the lag join depends on: t, shifted back and forward again,
    is t -- so a recorded `env_quarter` can be checked by hand."""
    for year in (1984, 2007, 2026):
        for quarter in QUARTERS:
            for lag in range(5):
                back = shift_quarters(year, quarter, -lag)
                assert shift_quarters(*back, lag) == (year, quarter)


def test_a_quarter_outside_the_calendar_is_refused():
    with pytest.raises(ValueError):
        shift_quarters(2020, 5, -1)
