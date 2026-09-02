"""One row per QC series per quarter (docs/03 `quarterly_env`, docs/04 s2).

This is the stage that reconciles the timescale mismatch docs/01 says the system
exists to solve: Kelp Watch publishes one number per polygon per quarter, and a
TidbiT publishes about thirteen thousand.

**The arithmetic of that reduction lives in `windowed.py`; what lives here is the
choice of window.** A quarter is one window a series can be judged against and a
deployment is another, and only the first belongs to this table. Everything below
is therefore about which rows form a quarter, what a quarterly row is keyed on,
and the dtypes the table promises -- not about how a mean or a spell is computed.

Four decisions carry the weight.

**What one row is.** The QC series key -- source, site, parameter, depth -- plus
year and quarter. Depth is in the key for the reason it is in the QC key: a
shallow and a deep logger at one site are not one series, and averaging them
across a thermocline would corrupt precisely the quarterly minimum and cold-day
counts that docs/04 s2 makes the nitrate proxy.

**Coverage is measured against the quarter's own duration**, divided by the
series' median observed cadence, so an hourly station and a 10-minute logger are
judged on the same scale. `n_obs`, `cadence_s` and `expected_obs` are all stored,
so the fraction is auditable rather than a bare number to be trusted. Quarters
differ in length -- 90 or 91 days for Q1, 91 for Q2, 92 for Q3 and Q4 -- which is
why the denominator is computed from the bounds rather than nominal.

**Quarters are enumerated from stored rows, and computed from QC-filtered ones.**
A quarter that sampled perfectly and failed QC on every row scores zero
coverage, not full coverage -- so the row exists, says `n_obs = 0`, and is
unusable. A quarter with no stored rows at all gets no row: there is nothing to
say about it, and inventing one would be the imputation hard rule 3 forbids.

**Nothing is imputed and nothing is dropped.** A quarter below the coverage
floor is flagged `usable = false` and keeps its features, which is hard rule 4's
discipline -- flags, never deletions -- applied one layer up, to quarters. That
also leaves the floor a sensitivity knob rather than a filter already applied.

Measured features are stored as doubles, counts included. The table is wide and
sparse by design (docs/03), so every feature column must be able to hold null
for "this feature set does not define me" -- and a count's `_anom` twin is a
double regardless.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from kelpcompare.features.config import FeatureConfig, ParameterFeatures
from kelpcompare.features.quarters import (
    is_complete,
    quarter_bounds,
    quarter_label,
    quarter_of,
    year_of,
)
from kelpcompare.features.windowed import (
    STATISTICS,
    WINDOW_BOOKKEEPING,
    Window,
    feature_columns,
    marker_columns,
    measured_columns,
    reduce_window,
    threshold_label,
)
from kelpcompare.storage import FLAG_NOT_EVALUATED, validate_frame

__all__ = [
    "BOOKKEEPING_COLUMNS",
    "QUARTERLY_KEY",
    "SERIES_KEY",
    "STATISTICS",
    "QuarterlyOutcome",
    "SeriesQuarters",
    "build_quarterly",
    "feature_columns",
    "marker_columns",
    "measured_columns",
    "quarterly_columns",
    "threshold_label",
]

#: The docs/03 row key: the QC series key plus time. Every feature row therefore
#: traces to exactly one QC series, which is a checkable statement rather than
#: an assumption.
QUARTERLY_KEY = ("source", "site_id", "parameter", "depth_m", "year", "quarter")

#: What makes rows one series, matching `qc.qartod.SERIES_KEY`.
SERIES_KEY = ("source", "site_id", "parameter", "depth_m")

#: Columns that describe the row rather than measure the water. No `_anom` twin:
#: the table must not offer the anomaly of a row count. `quarter_complete` is
#: this table's answer to "is this window over yet", which every windowed table
#: needs and each names for its own window.
BOOKKEEPING_COLUMNS = (*WINDOW_BOOKKEEPING, "quarter_complete", "qc_max_flag")

_DTYPES = {
    "source": "string",
    "site_id": "string",
    "parameter": "string",
    "depth_m": "float64",
    "year": "int32",
    "quarter": "int8",
    "feature_set": "string",
    "n_obs": "int64",
    "n_days_observed": "int32",
    "cadence_s": "float64",
    "expected_obs": "float64",
    "pct_coverage": "float64",
    "usable": "bool",
    "quarter_complete": "bool",
    "qc_max_flag": "int8",
}


@dataclass(frozen=True)
class SeriesQuarters:
    """One built series, in the shape the run manifest records (docs/03).

    The quarter counts are what make coverage attrition visible without opening
    the Parquet: how many quarters a series produced, and how many of those
    survived the coverage floor.
    """

    source: str
    site_id: str
    parameter: str
    depth_m: float | None
    feature_set: str
    rows: int
    quarters: int
    quarters_usable: int
    first_quarter: str | None
    last_quarter: str | None


@dataclass(frozen=True)
class QuarterlyOutcome:
    """The feature rows plus what the run should report about them."""

    frame: pd.DataFrame
    series: tuple[SeriesQuarters, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def quarters(self) -> int:
        return len(self.frame)

    @property
    def usable(self) -> int:
        return int(self.frame["usable"].sum()) if len(self.frame) else 0


def quarterly_columns(config: FeatureConfig) -> tuple[str, ...]:
    """The `quarterly_env` column order, before anomalies are added."""
    measured, markers = feature_columns(config)
    return (*QUARTERLY_KEY, *BOOKKEEPING_COLUMNS, *measured, *markers)


def build_quarterly(
    frame: pd.DataFrame,
    config: FeatureConfig,
    *,
    qc_max_flag: int = FLAG_NOT_EVALUATED,
    now: pd.Timestamp | None = None,
) -> QuarterlyOutcome:
    """Turn a docs/03 observation frame into quarterly feature rows.

    A pure function -- frame in, frame out, nothing read from disk and nothing
    written -- so a sensitivity rerun at a different `qc_max_flag` can happen in
    a notebook without producing a competing file of record (hard rule 7 forbids
    bypassing the CLI to *write*, not to read).

    A parameter the configuration does not know is left unbuilt and reported as
    a warning. Skipping is a gap for a human to close, and guessing which
    ecological thresholds an unconfigured parameter should get is not available.
    """
    validate_frame(frame)
    stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    columns = quarterly_columns(config)
    if frame.empty:
        return QuarterlyOutcome(frame=_empty(columns, config))

    working = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    working = working.assign(
        _year=year_of(working["timestamp"]),
        _quarter=quarter_of(working["timestamp"]),
        _keep=(working["qc_flag"] <= qc_max_flag) & working["value"].notna(),
    )

    rows: list[dict] = []
    series: list[SeriesQuarters] = []
    warnings: list[str] = []

    for key, group in working.groupby(list(SERIES_KEY), dropna=False, sort=True):
        source, site_id, parameter, depth_m = key
        entry = config.get(parameter)
        if entry is None:
            warnings.append(
                f"{site_id} reports {parameter!r}, which {config.path} does not configure; "
                f"{len(group)} rows left unbuilt"
            )
            continue

        built = [
            _quarter_row(
                quarter_rows,
                entry=entry,
                config=config,
                source=source,
                site_id=site_id,
                depth_m=depth_m,
                year=int(year),
                quarter=int(quarter),
                qc_max_flag=qc_max_flag,
                now=stamp,
                warnings=warnings,
            )
            for (year, quarter), quarter_rows in group.groupby(
                ["_year", "_quarter"], dropna=False, sort=True
            )
        ]
        rows.extend(built)
        labels = [quarter_label(row["year"], row["quarter"]) for row in built]
        series.append(
            SeriesQuarters(
                source=source,
                site_id=site_id,
                parameter=parameter,
                depth_m=None if pd.isna(depth_m) else float(depth_m),
                feature_set=entry.feature_set,
                rows=len(group),
                quarters=len(built),
                quarters_usable=sum(1 for row in built if row["usable"]),
                first_quarter=labels[0] if labels else None,
                last_quarter=labels[-1] if labels else None,
            )
        )

    return QuarterlyOutcome(
        frame=_frame(rows, columns, config),
        series=tuple(series),
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# One quarter of one series
# --------------------------------------------------------------------------


def _quarter_row(
    quarter_rows: pd.DataFrame,
    *,
    entry: ParameterFeatures,
    config: FeatureConfig,
    source: str,
    site_id: str,
    depth_m: float,
    year: int,
    quarter: int,
    qc_max_flag: int,
    now: pd.Timestamp,
    warnings: list[str],
) -> dict:
    """This table's key and completeness flag, around a windowed reduction."""
    start, end = quarter_bounds(year, quarter)
    reduced = reduce_window(
        quarter_rows.loc[quarter_rows["_keep"]],
        window=Window(start=start, end=end),
        entry=entry,
        coverage_floor=config.coverage_floor,
        label=f"{site_id}/{entry.parameter} {quarter_label(year, quarter)}",
        warnings=warnings,
        noun="quarter",
    )
    return {
        "source": source,
        "site_id": site_id,
        "parameter": entry.parameter,
        "depth_m": depth_m,
        "year": year,
        "quarter": quarter,
        **reduced,
        # Without this an in-progress quarter is indistinguishable from a station
        # outage: both come out under-covered, for entirely different reasons.
        "quarter_complete": is_complete(year, quarter, now),
        "qc_max_flag": qc_max_flag,
    }


# --------------------------------------------------------------------------
# Frame shape
# --------------------------------------------------------------------------


def _frame(rows: list[dict], columns: tuple[str, ...], config: FeatureConfig) -> pd.DataFrame:
    if not rows:
        return _empty(columns, config)
    frame = pd.DataFrame(rows).reindex(columns=list(columns))
    ordered = frame.sort_values(list(QUARTERLY_KEY), kind="stable", na_position="last")
    return _typed(ordered.reset_index(drop=True), config)


def _empty(columns: tuple[str, ...], config: FeatureConfig) -> pd.DataFrame:
    return _typed(pd.DataFrame({name: pd.Series(dtype="object") for name in columns}), config)


def _typed(frame: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
    """Fixed dtypes, so two runs over unchanged inputs write the same bytes.

    Measured features are `float64` including the counts, because the table is
    wide and sparse: every column has to be able to hold null for a row whose
    feature set does not define it. Markers are the nullable `boolean` for the
    same reason.
    """
    measured, markers = feature_columns(config)
    types = {
        **_DTYPES,
        **dict.fromkeys(measured, "float64"),
        **dict.fromkeys(markers, "boolean"),
    }
    return frame.astype({name: types[name] for name in frame.columns if name in types})
