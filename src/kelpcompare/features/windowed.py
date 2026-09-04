"""Reducing one QC series over one arbitrary window (docs/04 s2).

Everything here is the arithmetic of turning many observations into one row, and
none of it knows what the window *means*. That separation is the point: the
Kelp Watch quarter is one window and a deployment is another, and the features
docs/04 s2 defines -- the distribution, the threshold days, the degree days, the
longest warm spell -- are the same arithmetic either way.

**The window is the caller's, and it is half-open `[start, end)`.** Coverage
divides by the window's own duration, so a 21-day deployment and a 92-day
quarter are each judged against themselves rather than against a calendar
neither chose. That is the whole reason this module exists as its own seam:
`quarterly.py` measured coverage against `quarter_seconds` and bounded spells at
`quarter_bounds`, which is right for a quarter and prints a deployment boundary
as a data gap when it is not one.

**What a caller supplies and what it gets back.** The caller filters its own rows
-- by QC flag, by window, by whatever makes them one series -- and passes the
window it wants them judged against. What comes back is the shared bookkeeping
block and the configured features, and nothing that identifies the row: the key
columns belong to the table, not to the arithmetic, and so does whatever that
table calls its "is this window over yet" flag.

Nothing here imputes and nothing here drops. A window below the coverage floor
is flagged `usable = false` and keeps its features, which is hard rule 4's
discipline -- flags, never deletions -- applied to windows.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pandas as pd

from kelpcompare.features.config import FeatureConfig, ParameterFeatures

#: The bookkeeping every windowed reduction produces, in table order. A table
#: appends its own key columns before these and its own completeness flag after.
#: No `_anom` twin for any of them: the table must not offer the anomaly of a
#: row count.
WINDOW_BOOKKEEPING = (
    "feature_set",
    "n_obs",
    "n_days_observed",
    "cadence_s",
    "expected_obs",
    "pct_coverage",
    "usable",
)

#: The universal distribution features, applicable to any parameter. Not just the
#: centre: kelp responds to extremes that a mean erases (docs/01 s4).
STATISTICS = ("mean", "min", "max", "p05", "p95", "variance")

#: The threshold kinds whose arithmetic aggregates to whole days before it counts
#: anything, so a window shorter than a day cannot carry them: `days_above_20c`
#: over one day is 0 or 1, and a spell is at most one day long.
#:
#: `time_in_band` is deliberately absent. Occupancy is defined over any window and
#: means the same thing on an hour as on a quarter, so it is the one ecological
#: feature the sub-deployment tables can carry -- and the reason they take this
#: list rather than dropping every threshold they have.
DAY_BASED_KINDS = ("days_above", "days_below", "degree_days_above", "max_spell_above")

_ONE_DAY = pd.Timedelta(days=1)


@dataclass(frozen=True)
class Window:
    """The UTC window a reduction is judged against, and whether it ends closed.

    **A quarter is half-open and a deployment is closed, and the difference is one
    sample slot.** A quarter is `[start, end)` for the reason `quarters.py` gives:
    the instant that opens the next quarter belongs to it and to nothing else, so
    consecutive quarters tile the record without an instant landing in two. A
    deployment window is `[start, end]`, because both edges are events that
    happened -- the logger went in, and the logger came out -- and the sample at
    the closing edge is a real reading the registry means to include.

    That is why `expected_obs` is a method rather than a division a caller does
    for itself. Sampling every `c` seconds across a closed span of `D` seconds
    yields `D/c + 1` readings, not `D/c`: a 10-minute logger down for exactly one
    hour returns seven samples. Dividing without the `+1` makes every healthy
    deployment come out fractionally over full coverage, which would clamp to 1.0
    and emit the "cadence changed mid-deployment" warning on precisely the
    deployments where nothing went wrong.
    """

    start: pd.Timestamp
    end: pd.Timestamp
    inclusive_end: bool = False

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def bounds(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        return self.start, self.end

    def expected_obs(self, cadence: float) -> float:
        """How many observations a series at this cadence should have produced."""
        slots = self.seconds / cadence
        return slots + 1 if self.inclusive_end else slots

    def contains(self, timestamps: pd.Series) -> pd.Series:
        """Which timestamps fall inside, respecting whether the end is closed."""
        after = timestamps >= self.start
        return after & (timestamps <= self.end if self.inclusive_end else timestamps < self.end)


def threshold_label(threshold: float) -> str:
    """`20.0` -> `20c`, `20.5` -> `20_5c`, `-1.0` -> `neg1c`.

    Column names are derived from the configured threshold rather than fixed, so
    retuning a threshold renames its column instead of silently changing what an
    existing column means (docs/03). The `c` is the `temperature` feature set's,
    whose thresholds are degrees Celsius by definition (docs/04 s2) -- it is not
    read off the parameter's unit, which docs/03 forbids inferring anything from.
    """
    return f"{threshold:g}".replace(".", "_").replace("-", "neg") + "c"


def band_label(low: float, high: float) -> str:
    """`(14.0, 20.0)` -> `14c_20c`. Both edges, for the reason one is not enough.

    A band's meaning is its two edges, so both belong in the column name -- and
    ADR-006's rule that retuning a threshold renames its column has to hold when
    either edge moves, not only when the lower one does.
    """
    return f"{threshold_label(low)}_{threshold_label(high)}"


def measured_columns(entry: ParameterFeatures) -> tuple[str, ...]:
    """The feature columns one parameter measures, in table order."""
    return tuple(name for name, measured in columns_for(entry) if measured)


def marker_columns(entry: ParameterFeatures) -> tuple[str, ...]:
    """The non-measured feature columns: today, the spell gap markers."""
    return tuple(name for name, measured in columns_for(entry) if not measured)


def feature_columns(config: FeatureConfig) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Every measured column and every marker column the configuration can produce.

    The union across parameters, so a table's schema is a function of the
    configuration rather than of which sources a given run happened to build.
    A `--source` rerun therefore writes the same columns as a full run, and the
    two concatenate without alignment.
    """
    measured: list[str] = []
    markers: list[str] = []
    for name in sorted(config.parameters):
        for column, is_measured in columns_for(config.parameters[name]):
            target = measured if is_measured else markers
            if column not in target:
                target.append(column)
    return tuple(measured), tuple(markers)


def without_day_based(entry: ParameterFeatures) -> ParameterFeatures:
    """The same parameter, carrying only thresholds a sub-day window can hold.

    What a table reduced below a day may compute, in one place, so the daily and
    hourly tables cannot come to disagree about it -- and so that adding a
    threshold kind forces a decision about which side of `DAY_BASED_KINDS` it
    falls on rather than silently landing on one.
    """
    kept = {
        kind: values for kind, values in entry.thresholds.items() if kind not in DAY_BASED_KINDS
    }
    return replace(entry, thresholds=kept)


def columns_for(entry: ParameterFeatures) -> tuple[tuple[str, bool], ...]:
    """One parameter's feature columns, each with whether it is *measured*.

    Measured columns may get an `_anom` twin where the table offers one; markers
    never do. A boolean saying a spell touched a gap has no meaningful
    climatology.
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
    for low, high in entry.of("time_in_band"):
        columns.append((f"frac_in_band_{band_label(low, high)}", True))
    return tuple(columns)


def reduce_window(
    kept: pd.DataFrame,
    *,
    window: Window,
    entry: ParameterFeatures,
    coverage_floor: float,
    label: str,
    warnings: list[str],
    noun: str = "window",
    cadence: float | None = None,
) -> dict:
    """The bookkeeping and features for one series over one window.

    `kept` is already filtered -- by QC flag, by window, by series -- because
    which rows belong is the caller's question and this module's answer would
    have to guess at it. What is returned carries no key columns for the same
    reason.

    `noun` is what the caller calls its window, and it reaches the operator in a
    warning. Generic here would be a small loss twice over: a manifest saying a
    cadence changed "mid-quarter" or "mid-deployment" says which table to go and
    look at, where "mid-window" would leave that to be worked out.

    `cadence` lets a caller supply the interval it already knows, instead of
    having it re-measured from this window's rows. The median is robust over a
    quarter, where gaps are the tail of the interval distribution; over a window
    holding a handful of rows it is not, because with two observations the
    "median interval" *is* the gap. A day holding two samples three hours apart
    would measure its own cadence as 10800 s and score 0.25 coverage, when what
    is true is that a 600 s logger observed 1.4% of that day. A caller reducing
    one series over many short windows therefore measures the cadence once, over
    the whole series, and passes it down.
    """
    values = kept["value"].astype("float64")
    timestamps = kept["timestamp"]

    cadence = cadence or series_cadence(timestamps, label=label, warnings=warnings)
    expected = window.expected_obs(cadence) if cadence else None
    coverage, clamped = _coverage(len(kept), expected)
    if clamped:
        warnings.append(
            f"{label}: coverage clamped to 1.0 -- {len(kept)} observations against "
            f"{expected:.0f} expected at a {cadence:.0f}s median cadence, so the series' "
            f"cadence changed mid-{noun}"
        )

    row = {
        "feature_set": entry.feature_set,
        "n_obs": len(kept),
        "n_days_observed": timestamps.dt.floor("D").nunique(),
        "cadence_s": cadence,
        "expected_obs": expected,
        "pct_coverage": coverage,
        # Fewer than two observations has no interval to take a median of, so
        # there is no scale on which to judge the window at all. That is a
        # verdict the row states rather than one a reader has to infer from a
        # null cadence.
        "usable": len(kept) >= 2 and coverage >= coverage_floor,
    }
    row.update(_statistics(values))
    row.update(_temperature_features(values, timestamps, entry=entry, window=window))
    return row


def series_cadence(timestamps: pd.Series, *, label: str, warnings: list[str]) -> float | None:
    """The median observed inter-sample interval, in seconds, or None.

    None means "no scale to judge this window on", which coverage reads as
    zero. A non-positive median can only come from repeated timestamps, which
    the storage key forbids -- so it is reported rather than divided by.

    The median is robust to the thing being measured: gaps are the tail of the
    interval distribution, not its middle, so an hourly series missing half a
    window still has a median interval of an hour and correctly scores one half.
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
    """The fraction of the window observed, clamped, and whether it was clamped.

    A series whose cadence genuinely changed mid-window can compute above full
    coverage -- the median interval is then wrong for part of it. The value is
    clamped so the column stays a fraction, and the clamp is reported so a
    coverage number that had to be corrected is visible rather than quietly
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
    single-observation window therefore yields a null variance rather than a
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
    window: Window,
) -> dict:
    """The docs/04 s2 ecological features: the threshold counts, and the band.

    **The counts are day-based**, because the ecology is: a day that reached
    24 degC is a day of heat stress whether it did so for one hour or six, and
    because aggregating to days first is what makes them robust to irregular
    sampling. Every day with at least one observation counts, and
    `n_days_observed` records how many that was -- so a count reads as a floor
    rather than as a census. The bias direction is documented (docs/04): a day
    observed only overnight cannot show its daytime maximum, so these counts run
    low under partial coverage.

    **The band occupancy is not**, and that departure is the point of it. How
    much of a window the water spent between two temperatures is a question a
    day-based count cannot answer, and it is the one feature here that still
    means something over a window shorter than a day (`DAY_BASED_KINDS`).
    """
    features: dict = {}
    if not entry.thresholds:
        return features

    # A window whose every row failed QC still gets its row and its columns;
    # what it does not get is a zero, which would read as "no day was warm".
    if values.empty:
        return {name: None for name, _ in columns_for(entry) if name not in STATISTICS}

    days = timestamps.dt.floor("D")
    daily_max = values.groupby(days).max()
    daily_min = values.groupby(days).min()
    daily_mean = values.groupby(days).mean()
    observed = pd.DatetimeIndex(daily_max.index)

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
            pd.DatetimeIndex(daily_max.index[daily_max > threshold]), observed, window.bounds
        )
        features[f"max_spell_above_{label}_days"] = float(spell)
        features[f"max_spell_above_{label}_gap_interrupted"] = interrupted

    # Closed at both edges, so that `< low`, `[low, high]` and `> high` partition
    # the value axis exactly against the strict `days_below` and `days_above`
    # tests above. Half-open would leave a reading of exactly `high` belonging to
    # neither the band nor the tail beyond it.
    #
    # A fraction of the observations rather than of the clock: the two agree at a
    # regular cadence, which is what `cadence_s` and `pct_coverage` are on the
    # row to let a reader check (docs/04 s2).
    for low, high in entry.of("time_in_band"):
        inside = values.between(low, high, inclusive="both")
        features[f"frac_in_band_{band_label(low, high)}"] = float(inside.mean())
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
    marked. Neither is one ended by the window boundary, which is a limitation of
    the window rather than a hole in the record -- and which window is asked
    about therefore decides the answer. A deployment that began mid-quarter has
    unobserved days before it inside the quarter and none inside itself, so the
    same spell is a floor against the calendar and a measurement against the
    deployment. The deployment is the honest one; the logger was not in the
    water.

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
    window_start, window_end = bounds
    seen = set(observed)
    interrupted = any(
        (start - _ONE_DAY >= window_start and start - _ONE_DAY not in seen)
        or (end + _ONE_DAY < window_end and end + _ONE_DAY not in seen)
        for start, end, length in runs
        if length == longest
    )
    return longest, interrupted
