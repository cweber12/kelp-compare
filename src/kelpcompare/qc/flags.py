"""Per-test verdicts -> the docs/03 `qc_flag` and `qc_tests` columns.

CLAUDE.md hard rule 4 says QC flags and never deletes, which puts the whole
weight of a QC decision on these two columns. The default analysis filter is
`qc_flag <= 2`, so a row wrongly rolled up to suspect leaves the science exactly
as surely as a deleted row would -- the difference is that this one is
reversible and on the record.

`summarize` produces both columns together, on purpose. They are one statement
about a row, and a `qc_flag` that disagrees with the `qc_tests` beside it is
worse than either alone: it makes the audit trail lie.

**One deliberate divergence from `ioos_qc`.** `qartod_compare` ranks MISSING
*lowest*, so any other flag overrides it. Here it ranks highest. docs/03 gives 9
to a row whose value is absent, and there is nothing in an absence to judge: a
verdict about where the instrument was must not turn a missing reading into a
measurement that failed. Every other step of the precedence matches QARTOD.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd

from kelpcompare.storage import (
    FLAG_FAIL,
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    FLAG_PASS,
    FLAG_SUSPECT,
    WINDOW_TEST,
)

#: docs/03 `qc_tests` status words, one per storable flag. QARTOD UNKNOWN has no
#: word by design: a test that reached no verdict should say nothing, not record
#: that it said nothing.
STATUS_BY_FLAG = {
    FLAG_PASS: "pass",
    FLAG_SUSPECT: "suspect",
    FLAG_FAIL: "fail",
    FLAG_MISSING: "missing",
}
FLAG_BY_STATUS = {status: flag for flag, status in STATUS_BY_FLAG.items()}

#: Applied in order, each overriding the last. Missing comes last -- see above.
_PRECEDENCE = (FLAG_PASS, FLAG_SUSPECT, FLAG_FAIL, FLAG_MISSING)

#: Serialization order: the ingest-time verdict, then the QARTOD tests in the
#: order docs/04 s1 lists them. Fixed rather than alphabetical so re-running qc
#: on unchanged data produces byte-identical strings, and a partition diff shows
#: verdicts that changed rather than verdicts that moved.
_TEST_ORDER = (WINDOW_TEST, "gross_range", "spike", "rate_of_change")

SEPARATOR = ";"


def parse_tests(text: object) -> dict[str, str]:
    """Read one stored `qc_tests` string back into `{test: status}`.

    Refuses anything it cannot read rather than skipping it. A fragment silently
    dropped here is a verdict silently dropped from the roll-up, which is how an
    out-of-window reading would quietly become a passing one.
    """
    if text is None or pd.isna(text):
        return {}

    verdicts: dict[str, str] = {}
    for raw in str(text).split(SEPARATOR):
        fragment = raw.strip()
        if not fragment:
            continue
        name, separator, status = fragment.partition(":")
        if not separator or not name:
            raise ValueError(
                f"{fragment!r} is not a 'name:status' verdict; qc_tests holds "
                f"verdicts joined by {SEPARATOR!r} (docs/03)"
            )
        if status not in FLAG_BY_STATUS:
            raise ValueError(
                f"{fragment!r} records status {status!r}, which is not a docs/03 "
                f"qc_tests status; known: {', '.join(sorted(FLAG_BY_STATUS))}"
            )
        verdicts[name] = status
    return verdicts


def format_tests(verdicts: Mapping[str, str]) -> str:
    """Serialize `{test: status}` in the canonical order."""
    return SEPARATOR.join(f"{name}:{verdicts[name]}" for name in sorted(verdicts, key=_position))


def recorded_verdicts(column: Iterable[object]) -> dict[str, np.ndarray]:
    """Recover the verdicts already stored in a `qc_tests` column, as flags.

    This is how a `qc` run keeps the verdicts it did not produce: the ingest-time
    `deployment_window` result comes back out of storage and re-enters the
    roll-up alongside the fresh QARTOD results. A row that never recorded a given
    test reads back as no verdict, not as a pass.
    """
    parsed = [parse_tests(text) for text in column]
    names = sorted({name for row in parsed for name in row}, key=_position)
    return {
        name: np.array(
            [FLAG_BY_STATUS[row[name]] if name in row else FLAG_NOT_EVALUATED for row in parsed],
            dtype="int8",
        )
        for name in names
    }


def summarize(verdicts: Mapping[str, np.ndarray], *, rows: int) -> tuple[np.ndarray, np.ndarray]:
    """`(qc_flag, qc_tests)` for one series -- always in agreement.

    `rows` is explicit because a series with no verdicts at all still has a
    length, and because it is the one place the per-test vectors can be checked
    against the rows they are supposed to describe.
    """
    for name, vector in verdicts.items():
        if len(vector) != rows:
            raise ValueError(
                f"the {name!r} verdict covers {len(vector)} rows but the series has {rows}"
            )

    flags = np.full(rows, FLAG_NOT_EVALUATED, dtype="int8")
    for level in _PRECEDENCE:
        for vector in verdicts.values():
            flags[np.asarray(vector) == level] = level

    return flags, _describe(verdicts, rows=rows)


def _describe(verdicts: Mapping[str, np.ndarray], *, rows: int) -> np.ndarray:
    """One `qc_tests` string per row, omitting tests that reached no verdict."""
    ordered = {name: np.asarray(verdicts[name]) for name in sorted(verdicts, key=_position)}
    texts = np.empty(rows, dtype=object)

    for index in range(rows):
        row: dict[str, str] = {}
        for name, vector in ordered.items():
            flag = int(vector[index])
            if flag == FLAG_NOT_EVALUATED:
                continue
            if flag not in STATUS_BY_FLAG:
                raise ValueError(
                    f"the {name!r} test returned flag {flag}, which is not a docs/03 qc_flag"
                )
            row[name] = STATUS_BY_FLAG[flag]
        texts[index] = format_tests(row)
    return texts


def _position(name: str) -> tuple[int, int, str]:
    """Known tests in the documented order; anything newer, alphabetical after."""
    if name in _TEST_ORDER:
        return (0, _TEST_ORDER.index(name), name)
    return (1, 0, name)
