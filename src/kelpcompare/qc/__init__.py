"""kelpcompare.qc -- QARTOD tests as flags, never deletions (docs/04 s1, ADR-004).

Layer 1's last stage (docs/01). It reads observations that ingest has already
normalized and re-derives their `qc_flag` / `qc_tests`; it never writes a row it
did not read, and never removes one (CLAUDE.md hard rule 4).

`qartod` decides what a series is and which tests it has thresholds for;
`flags` turns the resulting verdicts into the two stored columns.
"""

from kelpcompare.qc.flags import (
    FLAG_BY_STATUS,
    STATUS_BY_FLAG,
    format_tests,
    parse_tests,
    recorded_verdicts,
    summarize,
)
from kelpcompare.qc.qartod import IMPLEMENTED_TESTS, QcOutcome, SeriesResult, evaluate

__all__ = [
    "FLAG_BY_STATUS",
    "IMPLEMENTED_TESTS",
    "STATUS_BY_FLAG",
    "QcOutcome",
    "SeriesResult",
    "evaluate",
    "format_tests",
    "parse_tests",
    "recorded_verdicts",
    "summarize",
]
