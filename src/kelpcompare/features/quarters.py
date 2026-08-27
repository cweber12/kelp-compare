"""The Kelp Watch quarter, in UTC (docs/04 s2, CLAUDE.md hard rule 2).

Kelp Watch publishes on the calendar quarter -- Q1 Jan-Mar, Q2 Apr-Jun, Q3
Jul-Sep, Q4 Oct-Dec -- so the arithmetic here is unremarkable. What is worth
stating is the timezone, because it is a choice with a visible consequence.

**Quarters are assigned in UTC**, carrying hard rule 2 to the last derived
table. The consequence: a reading taken at 5pm on 31 December on the US west
coast is 01:00 on 1 January UTC, and falls in the *following* Q1. Site-local
quarters would fix that at the cost of reintroducing local time past the
normalizer, and a hybrid would make the calendar depend on which site a row came
from -- two sites in different zones could then disagree about which quarter one
instant belongs to. A fixed offset that never moves is worth more here than an
alignment to daylight that is only ever approximate anyway; daylight saving
becomes irrelevant rather than handled.

Bounds are half-open, `[start, end)`, so the instant that opens Q2 belongs to Q2
and to nothing else. Quarter *durations* differ -- 90 or 91 days for Q1, 91 for
Q2, 92 for Q3 and Q4 -- which is why coverage divides by a computed duration
rather than a nominal one.
"""

from __future__ import annotations

import pandas as pd

#: The Kelp Watch calendar, in order.
QUARTERS = (1, 2, 3, 4)

_MONTHS_PER_QUARTER = 3


def quarter_bounds(year: int, quarter: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The half-open UTC window `[start, end)` of one quarter."""
    _require_quarter(quarter)
    start = pd.Timestamp(
        year=int(year), month=_MONTHS_PER_QUARTER * (quarter - 1) + 1, day=1, tz="UTC"
    )
    return start, start + pd.DateOffset(months=_MONTHS_PER_QUARTER)


def quarter_seconds(year: int, quarter: int) -> float:
    """How long the quarter is, in seconds. The coverage denominator's numerator."""
    start, end = quarter_bounds(year, quarter)
    return (end - start).total_seconds()


def shift_quarters(year: int, quarter: int, by: int) -> tuple[int, int]:
    """The quarter `by` quarters away, crossing year boundaries.

    Negative goes back, which is the direction the comparison table needs: the
    environment at `t - lag` against kelp at `t` (docs/04 s4.1). Arithmetic on a
    running quarter index rather than on the calendar, so 2007Q1 shifted back
    one is 2006Q4 and not a Q0 nobody has.
    """
    _require_quarter(quarter)
    index = int(year) * len(QUARTERS) + (int(quarter) - 1) + int(by)
    return divmod(index, len(QUARTERS))[0], divmod(index, len(QUARTERS))[1] + 1


def quarter_label(year: int, quarter: int) -> str:
    """`2007Q1` -- the compact form the run manifest reports."""
    _require_quarter(quarter)
    return f"{int(year)}Q{int(quarter)}"


def is_complete(year: int, quarter: int, now: pd.Timestamp) -> bool:
    """Whether the quarter had ended by `now`.

    Without this an in-progress quarter is indistinguishable from a station
    outage: both come out under-covered, for entirely different reasons. The
    coverage denominator stays the full quarter, so an unfinished quarter is
    honestly under-covered; this is what says *why* (docs/03).
    """
    _, end = quarter_bounds(year, quarter)
    return end <= _as_utc(now)


def year_of(timestamps: pd.Series) -> pd.Series:
    """The UTC year of each timestamp."""
    return _utc(timestamps).dt.year.astype("int32")


def quarter_of(timestamps: pd.Series) -> pd.Series:
    """The UTC Kelp Watch quarter of each timestamp."""
    return _utc(timestamps).dt.quarter.astype("int8")


def _utc(timestamps: pd.Series) -> pd.Series:
    """Refuse anything the UTC promise cannot be made about.

    The same guard `storage.validate_frame` applies on the way in, applied again
    here because this module is where the promise is *used*: a naive column
    would silently be quartered in whatever zone produced it.
    """
    tz = getattr(timestamps.dtype, "tz", None)
    if tz is None or str(tz) != "UTC":
        raise ValueError(
            f"quarters are assigned in UTC (hard rule 2); got a {timestamps.dtype} column"
        )
    return timestamps


def _as_utc(now: pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(now)
    if stamp.tz is None:
        raise ValueError(f"the run timestamp must be timezone-aware UTC, got {now!r}")
    return stamp.tz_convert("UTC")


def _require_quarter(quarter: int) -> None:
    if int(quarter) not in QUARTERS:
        raise ValueError(f"{quarter!r} is not a Kelp Watch quarter; expected one of {QUARTERS}")
