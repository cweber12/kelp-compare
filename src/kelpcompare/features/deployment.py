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

    for deployment in registry.deployments:
        if deployment.depth_m is None or not deployment.series_map:
            continue
        window = deployment_window(deployment)
        if window is None:
            warnings.append(
                f"{deployment.site_id} serial {deployment.serial}: no deployment window or "
                "timezone in the registry, so there is no window to measure coverage against"
            )
            continue
        for parameter in sorted(set(deployment.series_map.values())):
            entry = config.get(parameter)
            if entry is None:
                warnings.append(
                    f"{deployment.site_id} maps a series to {parameter!r}, which {config.path} "
                    "does not configure; that deployment is left unbuilt"
                )
                continue
            own = _deployment_rows(kept, deployment, parameter, window)
            if own.empty:
                warnings.append(
                    f"{deployment.site_id}: no {parameter} rows at {deployment.depth_m} m "
                    "within the deployment window, so it produces no deployment row"
                )
                continue
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
