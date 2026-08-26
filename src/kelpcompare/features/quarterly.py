"""One row per QC series per quarter (docs/03 `quarterly_env`, docs/04 s2).

This is the stage that reconciles the timescale mismatch docs/01 says the system
exists to solve: Kelp Watch publishes one number per polygon per quarter, and a
TidbiT publishes about thirteen thousand. Everything here is the arithmetic of
that reduction, plus the bookkeeping that makes a reduced number interpretable.

Four decisions carry the weight.

**What one row is.** The QC series key -- source, site, parameter, depth -- plus
year and quarter. Depth is in the key for the reason it is in the QC key: a
shallow and a deep logger at one site are not one series, and averaging them
across a thermocline would corrupt precisely the quarterly minimum and cold-day
counts that docs/04 s2 makes the nitrate proxy.

**Coverage is measured against the series' own cadence.** `expected_obs` is the
quarter's duration divided by the median observed inter-sample interval, so an
hourly station and a 10-minute logger are judged on the same scale. The median
is robust to the thing being measured: gaps are the tail of the interval
distribution, not its middle, so an hourly series missing half a quarter still
has a median interval of an hour and correctly scores one half. `n_obs`,
`cadence_s` and `expected_obs` are all stored, so the fraction is auditable
rather than a bare number to be trusted.

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
    quarter_seconds,
    year_of,
)
from kelpcompare.storage import FLAG_NOT_EVALUATED, validate_frame

#: The docs/03 row key: the QC series key plus time. Every feature row therefore
#: traces to exactly one QC series, which is a checkable statement rather than
#: an assumption.
QUARTERLY_KEY = ("source", "site_id", "parameter", "depth_m", "year", "quarter")

#: What makes rows one series, matching `qc.qartod.SERIES_KEY`.
SERIES_KEY = ("source", "site_id", "parameter", "depth_m")

#: Columns that describe the row rather than measure the water. No `_anom` twin:
#: the table must not offer the anomaly of a row count.
BOOKKEEPING_COLUMNS = (
    "feature_set",
    "n_obs",
    "n_days_observed",
    "cadence_s",
    "expected_obs",
    "pct_coverage",
    "usable",
    "quarter_complete",
    "qc_max_flag",
)

#: The universal distribution features, applicable to any parameter. Not just the
#: centre: kelp responds to extremes that a quarterly mean erases (docs/01 s4).
STATISTICS = ("mean", "min", "max", "p05", "p95", "variance")

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

_ONE_DAY = pd.Timedelta(days=1)


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


def threshold_label(threshold: float) -> str:
    """`20.0` -> `20c`, `20.5` -> `20_5c`, `-1.0` -> `neg1c`.

    Column names are derived from the configured threshold rather than fixed, so
    retuning a threshold renames its column instead of silently changing what an
    existing column means (docs/03). The `c` is the `temperature` feature set's,
    whose thresholds are degrees Celsius by definition (docs/04 s2) -- it is not
    read off the parameter's unit, which docs/03 forbids inferring anything from.
    """
    return f"{threshold:g}".replace(".", "_").replace("-", "neg") + "c"


def measured_columns(entry: ParameterFeatures) -> tuple[str, ...]:
    """The feature columns one parameter measures, in table order."""
    return tuple(name for name, measured in _columns_for(entry) if measured)


def marker_columns(entry: ParameterFeatures) -> tuple[str, ...]:
    """The non-measured feature columns: today, the spell gap markers."""
    return tuple(name for name, measured in _columns_for(entry) if not measured)


def feature_columns(config: FeatureConfig) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Every measured column and every marker column the configuration can produce.

    The union across parameters, so the table's schema is a function of the
    configuration rather than of which sources a given run happened to build.
    A `--source` rerun therefore writes the same columns as a full run, and the
    two concatenate without alignment.
    """
    measured: list[str] = []
    markers: list[str] = []
    for name in sorted(config.parameters):
        for column, is_measured in _columns_for(config.parameters[name]):
            target = measured if is_measured else markers
            if column not in target:
                target.append(column)
    return tuple(measured), tuple(markers)


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
    kept = quarter_rows.loc[quarter_rows["_keep"]]
    values = kept["value"].astype("float64")
    timestamps = kept["timestamp"]
    label = f"{site_id}/{entry.parameter} {quarter_label(year, quarter)}"

    cadence = _cadence(timestamps, label=label, warnings=warnings)
    expected = quarter_seconds(year, quarter) / cadence if cadence else None
    coverage, clamped = _coverage(len(kept), expected)
    if clamped:
        warnings.append(
            f"{label}: coverage clamped to 1.0 -- {len(kept)} observations against "
            f"{expected:.0f} expected at a {cadence:.0f}s median cadence, so the series' "
            "cadence changed mid-quarter"
        )

    row = {
        "source": source,
        "site_id": site_id,
        "parameter": entry.parameter,
        "depth_m": depth_m,
        "year": year,
        "quarter": quarter,
        "feature_set": entry.feature_set,
        "n_obs": len(kept),
        "n_days_observed": timestamps.dt.floor("D").nunique(),
        "cadence_s": cadence,
        "expected_obs": expected,
        "pct_coverage": coverage,
        # Fewer than two observations has no interval to take a median of, so
        # there is no scale on which to judge the quarter at all. That is a
        # verdict the row states rather than one a reader has to infer from a
        # null cadence.
        "usable": len(kept) >= 2 and coverage >= config.coverage_floor,
        "quarter_complete": is_complete(year, quarter, now),
        "qc_max_flag": qc_max_flag,
    }
    row.update(_statistics(values))
    row.update(_temperature_features(values, timestamps, entry=entry, year=year, quarter=quarter))
    return row


def _cadence(timestamps: pd.Series, *, label: str, warnings: list[str]) -> float | None:
    """The median observed inter-sample interval, in seconds, or None.

    None means "no scale to judge this quarter on", which coverage reads as
    zero. A non-positive median can only come from repeated timestamps, which
    the storage key forbids -- so it is reported rather than divided by.
    """
    if len(timestamps) < 2:
        return None
    median = timestamps.diff().dropna().dt.total_seconds().median()
    if not median > 0:
        warnings.append(
            f"{label}: median sample interval is {median}s, which cannot be a cadence; "
            "coverage reported as zero"
        )
        return None
    return float(median)


def _coverage(n_obs: int, expected: float | None) -> tuple[float, bool]:
    """The fraction of the quarter observed, clamped, and whether it was clamped.

    A series whose cadence genuinely changed mid-quarter can compute above full
    coverage -- the median interval is then wrong for part of the quarter. The
    value is clamped so the column stays a fraction, and the clamp is reported
    so a coverage number that had to be corrected is visible rather than quietly
    plausible.
    """
    if not expected:
        return 0.0, False
    raw = n_obs / expected
    return min(raw, 1.0), raw > 1.0


def _statistics(values: pd.Series) -> dict:
    """The distribution, not just its centre.

    Percentiles interpolate linearly and the variance is the sample convention
    (`ddof=1`), both stated so a reviewer can reproduce a number by hand. A
    single-observation quarter therefore yields a null variance rather than a
    zero, which would claim the water did not vary.
    """
    if values.empty:
        return dict.fromkeys(STATISTICS)
    return {
        "mean": float(values.mean()),
        "min": float(values.min()),
        "max": float(values.max()),
        "p05": float(values.quantile(0.05, interpolation="linear")),
        "p95": float(values.quantile(0.95, interpolation="linear")),
        "variance": float(values.var(ddof=1)) if len(values) > 1 else None,
    }


def _temperature_features(
    values: pd.Series,
    timestamps: pd.Series,
    *,
    entry: ParameterFeatures,
    year: int,
    quarter: int,
) -> dict:
    """The docs/04 s2 ecological features, all of them day-based.

    Day-based because the ecology is: a day that reached 24 degC is a day of
    heat stress whether it did so for one hour or six, and because aggregating
    to days first is what makes the features robust to irregular sampling. Every
    day with at least one observation counts, and `n_days_observed` records how
    many that was -- so a count reads as a floor rather than as a census. The
    bias direction is documented (docs/04): a day observed only overnight cannot
    show its daytime maximum, so these counts run low under partial coverage.
    """
    features: dict = {}
    if not entry.thresholds:
        return features

    # A quarter whose every row failed QC still gets its row and its columns;
    # what it does not get is a zero, which would read as "no day was warm".
    if values.empty:
        return {name: None for name, _ in _columns_for(entry) if name not in STATISTICS}

    days = timestamps.dt.floor("D")
    daily_max = values.groupby(days).max()
    daily_min = values.groupby(days).min()
    daily_mean = values.groupby(days).mean()
    observed = pd.DatetimeIndex(daily_max.index)
    bounds = quarter_bounds(year, quarter)

    for threshold in entry.of("days_above"):
        features[f"days_above_{threshold_label(threshold)}"] = float((daily_max > threshold).sum())
    for threshold in entry.of("days_below"):
        features[f"days_below_{threshold_label(threshold)}"] = float((daily_min < threshold).sum())
    for threshold in entry.of("degree_days_above"):
        excess = (daily_mean - threshold).clip(lower=0.0)
        features[f"degree_days_above_{threshold_label(threshold)}"] = float(excess.sum())
    for threshold in entry.of("max_spell_above"):
        label = threshold_label(threshold)
        spell, interrupted = _longest_spell(
            pd.DatetimeIndex(daily_max.index[daily_max > threshold]), observed, bounds
        )
        features[f"max_spell_above_{label}_days"] = float(spell)
        features[f"max_spell_above_{label}_gap_interrupted"] = interrupted
    return features


def _longest_spell(
    qualifying: pd.DatetimeIndex,
    observed: pd.DatetimeIndex,
    bounds: tuple[pd.Timestamp, pd.Timestamp],
) -> tuple[int, bool]:
    """The longest run of consecutive qualifying days, and whether a gap ended it.

    **A spell is broken by an unobserved day, never bridged across one.** Two
    qualifying days either side of a day nobody measured are two spells, because
    joining them would assert something about a day with no data -- the
    imputation hard rule 3 forbids, wearing a feature's clothes.

    Breaking silently would be its own defect: it reports a floor as though it
    were a measurement. So the return says whether the longest run ended at a
    gap, meaning the true spell may have been longer. A run ended by an observed
    day that simply did not qualify is a measurement, not a floor, and is not
    marked. Neither is one ended by the quarter boundary, which is a limitation
    of quarterly features rather than a hole in the record.

    Where several runs tie for longest, the marker is set if *any* of them
    touched a gap -- the honest reading, since the reported number is then a
    floor whichever of them the true longest spell was.
    """
    if qualifying.empty:
        return 0, False

    days = qualifying.sort_values()
    starts = [0, *(i for i in range(1, len(days)) if days[i] - days[i - 1] != _ONE_DAY)]
    runs = [
        (days[a], days[b - 1], b - a) for a, b in zip(starts, [*starts[1:], len(days)], strict=True)
    ]

    longest = max(length for _, _, length in runs)
    quarter_start, quarter_end = bounds
    seen = set(observed)
    interrupted = any(
        (start - _ONE_DAY >= quarter_start and start - _ONE_DAY not in seen)
        or (end + _ONE_DAY < quarter_end and end + _ONE_DAY not in seen)
        for start, end, length in runs
        if length == longest
    )
    return longest, interrupted


# --------------------------------------------------------------------------
# Frame shape
# --------------------------------------------------------------------------


def _columns_for(entry: ParameterFeatures) -> tuple[tuple[str, bool], ...]:
    """One parameter's feature columns, each with whether it is *measured*.

    Measured columns get an `_anom` twin; markers do not. A boolean saying a
    spell touched a gap has no meaningful climatology.
    """
    columns: list[tuple[str, bool]] = [(name, True) for name in STATISTICS]
    for threshold in entry.of("days_above"):
        columns.append((f"days_above_{threshold_label(threshold)}", True))
    for threshold in entry.of("days_below"):
        columns.append((f"days_below_{threshold_label(threshold)}", True))
    for threshold in entry.of("degree_days_above"):
        columns.append((f"degree_days_above_{threshold_label(threshold)}", True))
    for threshold in entry.of("max_spell_above"):
        label = threshold_label(threshold)
        columns.append((f"max_spell_above_{label}_days", True))
        columns.append((f"max_spell_above_{label}_gap_interrupted", False))
    return tuple(columns)


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
