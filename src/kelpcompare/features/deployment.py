"""One row per project-sensor deployment (docs/03 `deployment`, docs/04 s1).

The same docs/04 s2 features `quarterly_env` computes, reduced over the window
the instrument was actually in the water rather than over the Kelp Watch
calendar. `quarterly_env` answers "what was the water like in 2026 Q3"; this
answers "what did this logger record while it was down there", and for a
deployment that does not fill a quarter those are different questions with
different right answers.

**Why the quarterly table cannot answer it.** Three of its columns are about the
quarter rather than about the logger, and all three mislead when a deployment is
read off them. `pct_coverage` divides by a quarter, so a logger that recorded
every sample of a three-week deployment reads 0.228 -- and `usable` falls out
false on the strength of it, marking a complete record unusable. Worse,
`max_spell_above_20c_gap_interrupted` comes out true, because Q3 opens on 1 July
and the logger went in on the 11th: the unobserved days before the deployment sit
inside the quarter, so a warm spell running to the start of the record is flagged
as *a floor that may have been longer*. It may not have been. The logger was not
in the water. **The quarterly calendar prints a deployment boundary as a data
gap**, and that is the defect this table exists to correct.

**What one row is.** The deployment -- site, serial, deployment number -- plus
the parameter and the depth it was measured at. That is `validation.parquet`'s
key without its two reference columns, so the two tables join on the deployment:
what the instrument recorded, beside how it compared to its neighbours.

**No anomaly columns, and their absence is the point.** An anomaly needs a
climatology, a climatology needs ten usable years (docs/04 s3), and ADR-007
makes that minimum non-overridable. A project sensor will not have one for a
decade. Shipping null `_anom` twins here would offer a column that can only ever
be empty and invite the reading ADR-007 refuses -- that a thin baseline is a
baseline. A table with no such column cannot be misread that way.

**Coverage still means something, and now it means something useful.** Measured
against the deployment's own window, a coverage below the floor is no longer a
calendar artifact: it is the instrument stopping early, flooding, or failing QC.
`usable` reads as instrument health rather than as a statement about a quarter.
"""

from __future__ import annotations

import pandas as pd

from kelpcompare.features.config import FeatureConfig
from kelpcompare.features.windowed import (
    WINDOW_BOOKKEEPING,
    Window,
    feature_columns,
    reduce_window,
    series_cadence,
    sub_window_columns,
    without_day_based,
)
from kelpcompare.registry import Deployment, Registry
from kelpcompare.storage import FLAG_NOT_EVALUATED, validate_frame

#: docs/03 `deployment.parquet`. `depth_m` is in the key for the reason it is in
#: the QC key: two loggers at one site and two depths are two series, and a
#: deployment that carried both is two rows.
DEPLOYMENT_KEY = ("site_id", "serial", "deployment_number", "parameter", "depth_m")

#: What identifies the deployment beyond its key, and the window it was judged
#: against. `deployment_complete` is this table's answer to "is this window over
#: yet" -- the counterpart of `quarter_complete`, and the difference between a
#: logger still on the seabed and one recovered.
DEPLOYMENT_DESCRIPTION = ("source", "instrument", "window_start", "window_end")

_DTYPES = {
    "site_id": "string",
    "serial": "string",
    "deployment_number": "Int64",
    "parameter": "string",
    "depth_m": "float64",
    "source": "string",
    "instrument": "string",
    "window_start": "datetime64[us]",
    "window_end": "datetime64[us]",
    "feature_set": "string",
    "n_obs": "int64",
    "n_days_observed": "int32",
    "cadence_s": "float64",
    "expected_obs": "float64",
    "pct_coverage": "float64",
    "usable": "bool",
    "deployment_complete": "bool",
    "qc_max_flag": "int8",
}


def deployment_columns(config: FeatureConfig) -> tuple[str, ...]:
    """The `deployment` column order. No anomaly columns, by design."""
    measured, markers = feature_columns(config)
    return (
        *DEPLOYMENT_KEY,
        *DEPLOYMENT_DESCRIPTION,
        *WINDOW_BOOKKEEPING,
        "deployment_complete",
        "qc_max_flag",
        *measured,
        *markers,
    )


def deployment_window(deployment: Deployment) -> Window | None:
    """The deployment's in-water window as a closed UTC `Window`, or None.

    **Closed, not half-open.** Both edges are events that happened -- the logger
    went in, and the logger came out -- and the registry means the sample at the
    closing edge to be included. `windowed.Window` carries the distinction because
    it changes the coverage denominator by one sample slot.

    None where the registry has not recorded a window or a timezone. That is the
    docs/06 s5 gate showing through: without both, the rows cannot be placed in
    time at all, and a coverage figure against a window nobody declared would be
    a guess wearing a measurement's clothes.
    """
    if not deployment.window_local or not deployment.tz:
        return None
    start, end = (
        pd.Timestamp(edge, tz=deployment.tz).tz_convert("UTC") for edge in deployment.window_local
    )
    return Window(start=start, end=end, inclusive_end=True)


def build_deployment(
    observations: pd.DataFrame,
    registry: Registry,
    config: FeatureConfig,
    *,
    qc_max_flag: int = FLAG_NOT_EVALUATED,
    now: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Reduce every registered deployment over its own window.

    Returns the table and the warnings worth putting in a manifest. A deployment
    that produced no row is a gap a reader has to be told about: silently absent
    and recorded-but-empty are different states, and only one of them is a
    registry problem.

    A pure function, like `build_quarterly` -- frame in, frame out -- so a
    sensitivity rerun at another strictness can happen in a notebook without
    writing a competing file of record.
    """
    validate_frame(observations)
    stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    columns = deployment_columns(config)

    warnings: list[str] = []
    rows: list[dict] = []
    kept = observations[(observations["qc_flag"] <= qc_max_flag) & observations["value"].notna()]

    for deployment, entry, window, own in _resolved(kept, registry, config, warnings):
        rows.append(
            _row(
                own,
                deployment=deployment,
                entry=entry,
                config=config,
                window=window,
                qc_max_flag=qc_max_flag,
                now=stamp,
                warnings=warnings,
            )
        )

    return _frame(rows, columns, config), tuple(warnings)


def empty_deployment(config: FeatureConfig) -> pd.DataFrame:
    """The table's shape with no rows, so a build with nothing to do still writes
    a readable file rather than one whose columns depend on what it found."""
    return _frame([], deployment_columns(config), config)


def _deployment_rows(
    kept: pd.DataFrame, deployment: Deployment, parameter: str, window: Window
) -> pd.DataFrame:
    """This deployment's own rows: its site, its depth, inside its window.

    Restricted by window as well as by site and depth, because `site_id` and
    `depth_m` do not distinguish two deployments of one logger at one place --
    `deployment_number` does, and the window is how that reaches the rows. Rows
    outside it already carry `qc_flag = 4` and are usually filtered before this,
    but a caller passing a permissive `--qc-max-flag` must not silently merge two
    deployments into one row.
    """
    frame = kept[
        (kept["site_id"] == deployment.site_id)
        & (kept["parameter"] == parameter)
        & (kept["depth_m"] == deployment.depth_m)
    ]
    return frame[window.contains(frame["timestamp"])]


def _row(
    own: pd.DataFrame,
    *,
    deployment: Deployment,
    entry,
    config: FeatureConfig,
    window: Window,
    qc_max_flag: int,
    now: pd.Timestamp,
    warnings: list[str],
) -> dict:
    """This table's key and description, around a windowed reduction."""
    reduced = reduce_window(
        own,
        window=window,
        entry=entry,
        coverage_floor=config.coverage_floor,
        label=f"{deployment.site_id}/{entry.parameter} deployment {deployment.deployment_number}",
        warnings=warnings,
        noun="deployment",
    )
    return {
        "site_id": deployment.site_id,
        "serial": deployment.serial,
        "deployment_number": deployment.deployment_number,
        "parameter": entry.parameter,
        "depth_m": deployment.depth_m,
        "source": str(own["source"].iloc[0]),
        "instrument": deployment.instrument,
        "window_start": window.start.tz_localize(None),
        "window_end": window.end.tz_localize(None),
        **reduced,
        # A logger still on the seabed is under-covered for a reason that is not
        # a fault, the same distinction `quarter_complete` draws for a quarter
        # still in progress.
        "deployment_complete": bool(window.end <= now),
        "qc_max_flag": qc_max_flag,
    }


def _frame(rows: list[dict], columns: tuple[str, ...], config: FeatureConfig) -> pd.DataFrame:
    if not rows:
        empty = pd.DataFrame({name: pd.Series(dtype="object") for name in columns})
        return _typed(empty, config)
    frame = pd.DataFrame(rows).reindex(columns=list(columns))
    ordered = frame.sort_values(list(DEPLOYMENT_KEY), kind="stable", na_position="last")
    return _typed(ordered.reset_index(drop=True), config)


def _typed(frame: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
    """Fixed dtypes, so two runs over unchanged inputs write the same bytes."""
    measured, markers = feature_columns(config)
    types = {
        **_DTYPES,
        **dict.fromkeys(measured, "float64"),
        **dict.fromkeys(markers, "boolean"),
    }
    return frame.astype({name: types[name] for name in frame.columns if name in types})


#: docs/03 `deployment_daily.parquet`. The deployment's key plus the UTC day, so
#: a daily row traces to exactly one row of `deployment.parquet`.
DEPLOYMENT_DAILY_KEY = (*DEPLOYMENT_KEY, "day")

#: One row per *observed* day. Deliberately narrower than `WINDOW_BOOKKEEPING`:
#: `feature_set` belongs to the parent row, `n_days_observed` is 1 by
#: construction, and `usable` is left off for a reason of its own -- see
#: `build_deployment_daily`.
#: The fixed prefix. The feature columns that follow come from the configuration
#: (`deployment_daily_columns`) rather than from a list here, so a band declared
#: in the registry reaches this table without a second list being edited.
DEPLOYMENT_DAILY_COLUMNS = (
    *DEPLOYMENT_DAILY_KEY,
    "n_obs",
    "cadence_s",
    "expected_obs",
    "pct_coverage",
    "partial_day",
)

#: What a daily row takes from a windowed reduction besides its features.
_DAILY_FROM_REDUCTION = ("n_obs", "cadence_s", "expected_obs", "pct_coverage")

_ONE_DAY = pd.Timedelta(days=1)

_DAILY_DTYPES = {
    "site_id": "string",
    "serial": "string",
    "deployment_number": "Int64",
    "parameter": "string",
    "depth_m": "float64",
    "day": "datetime64[us]",
    "n_obs": "int64",
    "cadence_s": "float64",
    "expected_obs": "float64",
    "pct_coverage": "float64",
    "partial_day": "bool",
}


def deployment_daily_columns(config: FeatureConfig) -> tuple[str, ...]:
    """The daily table's columns: the fixed prefix, then what a day may carry.

    A day carries the distribution and any feature that is not day-based -- see
    `windowed.DAY_BASED_KINDS` for why that is a real distinction rather than a
    convenience, and `build_deployment_daily` for what it excludes.
    """
    return (*DEPLOYMENT_DAILY_COLUMNS, *sub_window_columns(config))


def build_deployment_daily(
    observations: pd.DataFrame,
    registry: Registry,
    config: FeatureConfig,
    *,
    qc_max_flag: int = FLAG_NOT_EVALUATED,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """The same series, reduced a day at a time instead of a deployment at a time.

    `deployment.parquet` says a logger recorded 78.93 degC-days above 18 degC. It
    cannot say whether that accumulated steadily or arrived in one week, and no
    table in the zone can: the finest grain the features zone exposes is the
    deployment window, while the instrument samples every 600 s. This is the
    first step down (https://github.com/cweber12/kelp-compare/issues/158).

    **It computes nothing new.** `windowed._temperature_features` already forms a
    daily maximum, minimum and mean to build the docs/04 s2 day-based features on
    top of; this exposes that intermediate rather than inventing one. So the
    scalar table becomes checkable against it -- `days_above_20c` is the count of
    rows here whose `max` exceeds 20 -- which is the "computed rather than
    restated" posture `notebooks/README.md` already asks of a notebook.

    **Days are clipped to the deployment window, and that is the whole trick.** A
    logger that went in at 15:00 observed nine hours of its first day and every
    one of them. Judged against a full 24 hours it reads 0.375 covered, which is
    the identical mistake the quarterly table makes on a deployment and which
    this module's own docstring exists to describe. Clipped, it reads 1.000 and
    carries `partial_day` to say the day was cut by the deployment rather than by
    a gap. The clipped days tile the window exactly: on `PROJ:TIDBIT-1` they sum
    to 54 + 20*144 + 88 = 3022 expected observations, which is the parent row's
    `expected_obs` to the sample.

    **One row per observed day, and no row for a day nobody measured.** An absent
    day is a gap, the same way `_longest_spell` reads one, and a row of nulls
    would be an invitation to fill it.

    **No `usable` column, deliberately.** docs/04 s2 considered a minimum per-day
    coverage and rejected it: it invents a second coverage threshold, and it
    would discard the hottest day of a window if that day happened to be
    short-sampled. A `usable` flag here would be that threshold arriving through
    the back door, so the coverage is reported and the filtering is not done.

    Registry problems are left to `build_deployment`, which walks the same
    deployments and reports them; repeating them here would double every such
    line in one run's manifest.
    """
    validate_frame(observations)
    warnings: list[str] = []
    rows: list[dict] = []
    kept = observations[(observations["qc_flag"] <= qc_max_flag) & observations["value"].notna()]

    for deployment, entry, window, own in _resolved(kept, registry, config):
        label = f"{deployment.site_id}/{entry.parameter} deployment {deployment.deployment_number}"
        # Measured once over the whole deployment and passed down: a day holding
        # two samples cannot measure its own cadence -- see `reduce_window`.
        cadence = series_cadence(own["timestamp"], label=label, warnings=warnings)
        observed = dict(tuple(own.groupby(own["timestamp"].dt.floor("D"))))
        for day, span, partial in _day_windows(window):
            frame = observed.get(day)
            if frame is None or frame.empty:
                continue
            reduced = reduce_window(
                frame,
                window=span,
                entry=without_day_based(entry),
                coverage_floor=config.coverage_floor,
                label=f"{label} {day:%Y-%m-%d}",
                warnings=warnings,
                noun="day",
                cadence=cadence,
            )
            rows.append(
                {
                    "site_id": deployment.site_id,
                    "serial": deployment.serial,
                    "deployment_number": deployment.deployment_number,
                    "parameter": entry.parameter,
                    "depth_m": deployment.depth_m,
                    # Naive UTC on the way out, as `window_start` and every
                    # stored timestamp are (docs/03).
                    "day": day.tz_localize(None) if day.tzinfo else day,
                    "partial_day": partial,
                    **{name: reduced[name] for name in _DAILY_FROM_REDUCTION},
                    **{
                        name: reduced[name]
                        for name in sub_window_columns(config)
                        if name in reduced
                    },
                }
            )

    return _daily_frame(rows, config), tuple(warnings)


def empty_deployment_daily(config: FeatureConfig) -> pd.DataFrame:
    """The daily table's shape with no rows."""
    return _daily_frame([], config)


def _day_windows(window: Window) -> list[tuple[pd.Timestamp, Window, bool]]:
    """The window cut into UTC days, each clipped to it.

    Only the final day inherits the closed upper edge. Giving it to every day
    whose upper edge happens to equal the window's would count the closing sample
    twice where a deployment ends exactly at midnight, because `dt.floor("D")`
    puts a midnight reading on its own day rather than on the one before it.
    """
    last = window.end.floor("D")
    spans: list[tuple[pd.Timestamp, Window, bool]] = []
    day = window.start.floor("D")
    while day <= last:
        following = day + _ONE_DAY
        low, high = max(day, window.start), min(following, window.end)
        spans.append(
            (
                day,
                Window(start=low, end=high, inclusive_end=window.inclusive_end and day == last),
                bool(low > day or high < following),
            )
        )
        day = following
    return spans


def _resolved(
    kept: pd.DataFrame,
    registry: Registry,
    config: FeatureConfig,
    warnings: list[str] | None = None,
):
    """Every deployment that has rows, with its entry, its window and those rows.

    Shared by both builders so the two tables cannot disagree about which
    deployments exist. `warnings` is optional because only one of them reports
    the registry gaps this walk finds; collecting them twice would double every
    such line in a single run's manifest.
    """
    for deployment in registry.deployments:
        if deployment.depth_m is None or not deployment.series_map:
            continue
        window = deployment_window(deployment)
        if window is None:
            if warnings is not None:
                warnings.append(
                    f"{deployment.site_id} serial {deployment.serial}: no deployment window or "
                    "timezone in the registry, so there is no window to measure coverage against"
                )
            continue
        for parameter in sorted(set(deployment.series_map.values())):
            entry = config.get(parameter)
            if entry is None:
                if warnings is not None:
                    warnings.append(
                        f"{deployment.site_id} maps a series to {parameter!r}, which "
                        f"{config.path} does not configure; that deployment is left unbuilt"
                    )
                continue
            own = _deployment_rows(kept, deployment, parameter, window)
            if own.empty:
                if warnings is not None:
                    warnings.append(
                        f"{deployment.site_id}: no {parameter} rows at {deployment.depth_m} m "
                        "within the deployment window, so it produces no deployment row"
                    )
                continue
            yield deployment, entry, window, own


def _daily_frame(rows: list[dict], config: FeatureConfig) -> pd.DataFrame:
    """Fixed dtypes and a stable order, so two runs write the same bytes."""
    columns = list(deployment_daily_columns(config))
    types = {**_DAILY_DTYPES, **dict.fromkeys(sub_window_columns(config), "float64")}
    if not rows:
        empty = pd.DataFrame({name: pd.Series(dtype="object") for name in columns})
        return empty.astype(types)
    frame = pd.DataFrame(rows).reindex(columns=columns)
    ordered = frame.sort_values(list(DEPLOYMENT_DAILY_KEY), kind="stable", na_position="last")
    return ordered.reset_index(drop=True).astype(types)
