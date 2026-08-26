"""The QARTOD tests, run over stored observation rows (docs/04 s1, ADR-004).

ADR-004 chose `ioos_qc` over hand-rolled thresholds so that reviewers meet flag
semantics they already know. This module is the thin, auditable layer around it:
it decides what counts as one series, which tests a parameter has thresholds
for, and nothing else. The thresholds themselves live in `parameters.json`, and
the roll-up lives in `flags.py`.

Three decisions here are load-bearing.

**What one series is.** Rows are grouped by source, site, parameter, and depth,
and the whole group is tested in time order regardless of which year partition
each row lives in. Spike and rate-of-change read a row's neighbours, so a series
split at a partition boundary would invent a discontinuity at every new year.

**Every row is tested, including rows ingest already failed.** The install
transient in the reviewed deployment is out of its window and therefore already
condemned -- but docs/06 s5 check 6 asks for it to show up as a spike failure
too, and it cannot if it is excluded from the series that the spike test reads.
Two independent tests catching one reading is the intended redundancy, not
duplicated effort.

**A test with no thresholds does not run, and says so by omission.** There is no
default threshold anywhere in this module. A guessed threshold that flagged real
data would be indistinguishable, in the stored flags, from a real QC failure --
and because the default analysis filter is `qc_flag <= 2`, it would quietly
remove the cold upwelling excursions docs/04 s2 relies on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from ioos_qc import qartod

from kelpcompare.parameters import Parameter, Parameters
from kelpcompare.qc.flags import recorded_verdicts, summarize
from kelpcompare.storage import (
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    OBSERVATION_COLUMNS,
    validate_frame,
)

#: The tests this stage runs. A verdict recorded under one of these names is
#: owned by this stage: it is re-derived every run, so a stale result from a
#: threshold that has since been removed does not survive as evidence. Anything
#: else in `qc_tests` -- `deployment_window`, or a test added later -- is
#: preserved untouched.
IMPLEMENTED_TESTS = ("gross_range", "spike", "rate_of_change")

#: What makes rows one series. Depth is in the key because one site can carry a
#: shallow and a deep logger, and their readings are not each other's neighbours.
SERIES_KEY = ("source", "site_id", "parameter", "depth_m")


@dataclass(frozen=True)
class SeriesResult:
    """One evaluated series, in the shape the run manifest records (docs/03)."""

    source: str
    site_id: str
    parameter: str
    depth_m: float | None
    rows: int
    tests: tuple[str, ...]
    flag_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class QcOutcome:
    """The re-flagged rows plus what the run should report about them."""

    frame: pd.DataFrame
    series: tuple[SeriesResult, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def flag_counts(self) -> dict[str, int]:
        """The run-level QC flag histogram docs/03 requires of every manifest."""
        counts = self.frame["qc_flag"].value_counts().to_dict()
        return {str(flag): int(n) for flag, n in sorted(counts.items())}


def evaluate(frame: pd.DataFrame, parameters: Parameters) -> QcOutcome:
    """Re-derive `qc_flag` and `qc_tests` for every row of a docs/03 frame.

    Returns a new frame in the row order and under the index it was given --
    unique or not -- and only the two QC columns differ. Rows are never added,
    removed, or reordered (CLAUDE.md hard rule 4).

    A parameter the registry does not know is left exactly as it was found and
    reported as a warning -- skipping is a gap for a human to close, and guessing
    what an unknown parameter should measure is not available.
    """
    validate_frame(frame)
    if frame.empty:
        return QcOutcome(frame=frame.copy())

    # Each series is written back by position rather than by index label. Label
    # assignment quietly requires a unique index, which docs/03 does not ask for
    # and a caller who built this frame with `pd.concat` has no reason to have --
    # duplicate labels select more rows than the verdict vector covers, and the
    # whole source fails on a pandas message that names neither the index nor
    # this stage. The index is the caller's, so it goes back on at the end.
    given_index = frame.index
    flagged = frame.reset_index(drop=True)

    results: list[SeriesResult] = []
    warnings: list[str] = []

    for key, group in flagged.groupby(list(SERIES_KEY), dropna=False, sort=False):
        source, site_id, parameter_name, depth_m = key
        parameter = parameters.get(parameter_name)
        if parameter is None:
            warnings.append(
                f"{site_id} reports {parameter_name!r}, which is not in {parameters.path}; "
                f"{len(group)} rows left unevaluated"
            )
            continue

        ordered = group.sort_values("timestamp", kind="stable")
        verdicts, skipped = _run_tests(ordered, parameter)
        warnings.extend(f"{site_id}/{parameter_name}: {reason}" for reason in skipped)

        preserved = {
            name: vector
            for name, vector in recorded_verdicts(ordered["qc_tests"]).items()
            if name not in IMPLEMENTED_TESTS
        }
        qc_flag, qc_tests = summarize({**preserved, **verdicts}, rows=len(ordered))

        flagged.loc[ordered.index, "qc_flag"] = qc_flag
        flagged.loc[ordered.index, "qc_tests"] = qc_tests
        results.append(
            SeriesResult(
                source=source,
                site_id=site_id,
                parameter=parameter_name,
                depth_m=None if pd.isna(depth_m) else float(depth_m),
                rows=len(ordered),
                tests=tuple(verdicts),
                flag_counts=_histogram(qc_flag),
            )
        )

    return QcOutcome(
        frame=flagged[list(OBSERVATION_COLUMNS)].set_axis(given_index),
        series=tuple(results),
        warnings=tuple(warnings),
    )


def _run_tests(
    series: pd.DataFrame, parameter: Parameter
) -> tuple[dict[str, np.ndarray], list[str]]:
    """Every test this parameter has thresholds for, plus why any was skipped."""
    values = series["value"].to_numpy(dtype="float64")
    timestamps = series["timestamp"]

    verdicts: dict[str, np.ndarray] = {}
    skipped: list[str] = []

    if parameter.valid_range is None:
        skipped.append("gross_range not run: the parameter declares no valid_range")
    else:
        spans = {"fail_span": parameter.valid_range}
        if parameter.qc.gross_range is not None:
            spans["suspect_span"] = parameter.qc.gross_range.suspect_span
        verdicts["gross_range"] = _flags(qartod.gross_range_test(inp=values, **spans))

    spike = parameter.qc.spike
    if spike is not None:
        # Three points, because the test judges a sample against the midpoint of
        # its two neighbours. Below that `spike_test` returns UNKNOWN rather than
        # raising -- it raises only on an empty series, which `evaluate` cannot
        # hand it -- so the stored columns come out identical either way. What
        # the guard buys is the manifest: a skip reason a reader can act on,
        # instead of a series whose recorded `tests` claim spike ran.
        if len(values) < 3:
            skipped.append(f"spike not run: {len(values)} rows is fewer than the three it needs")
        else:
            verdicts["spike"] = _flags(
                qartod.spike_test(
                    inp=values,
                    suspect_threshold=spike.suspect,
                    fail_threshold=spike.fail,
                )
            )

    rate = parameter.qc.rate_of_change
    if rate is not None:
        if rate.suspect_per_second is None:
            # QARTOD's rate test has no fail-only form: the suspect rate is the
            # required argument. Report the gap rather than promote the fail
            # threshold into a role its number was not chosen for.
            skipped.append(
                "rate_of_change not run: the registry declares fail_per_hour but no "
                "suspect_per_hour, and the test has no fail-only form"
            )
        elif len(values) < 2:
            skipped.append("rate_of_change not run: a rate needs two rows")
        else:
            verdicts["rate_of_change"] = _without_unmeasured_rates(
                _flags(
                    qartod.rate_of_change_test(
                        inp=values,
                        tinp=timestamps,
                        threshold=rate.suspect_per_second,
                        fail_threshold=rate.fail_per_second,
                    )
                ),
                values,
            )

    return verdicts, skipped


def _without_unmeasured_rates(result: np.ndarray, values: np.ndarray) -> np.ndarray:
    """Withdraw the rate verdict from rows whose rate was never measured.

    `rate_of_change_test` builds its rates as `np.ma.zeros(inp.size)` and fills
    only `roc[1:]`, so index 0 holds a rate of zero by construction. A row whose
    predecessor is `NaN` fares no better: its first difference is masked, and
    `roc > threshold` is then False rather than unknown. Both classes come back
    GOOD without anything having been compared, and a pass nothing earned is
    worth less than no verdict at all -- so say nothing, exactly as the spike
    test already does at the ends of a series and either side of a gap (docs/03).

    This suppression is not redundant with `gross_range`: that test independently
    judges the same rows, which is why the stored flag stays plausible, but it is
    judging the value rather than the step, and the step is what a gap conceals.

    A row that is itself missing keeps its `missing` verdict. Inside a gap two or
    more samples wide the predecessor is `NaN` too, so suppressing on the
    predecessor alone would drop such a row to not-evaluated wherever the rate
    test is the only one with thresholds -- an absent value that stops reading as
    absent, which is the worse defect of the two.
    """
    unmeasured = np.empty(len(values), dtype=bool)
    unmeasured[0] = True  # nothing before it: the rate is zero by construction
    unmeasured[1:] = np.isnan(values[:-1])
    unmeasured &= result != FLAG_MISSING

    withdrawn = result.copy()
    withdrawn[unmeasured] = FLAG_NOT_EVALUATED
    return withdrawn


def _flags(result) -> np.ndarray:
    """`ioos_qc` returns masked uint8; storage and the roll-up want plain int8."""
    return np.asarray(result).astype("int8")


def _histogram(qc_flag: np.ndarray) -> dict[str, int]:
    values, counts = np.unique(qc_flag, return_counts=True)
    return {str(int(flag)): int(count) for flag, count in zip(values, counts, strict=True)}
