"""Quarterly climatology and the anomalies taken against it (docs/04 s3).

Every raw correlation between kelp and the environment is dominated by the
seasonal cycle; without this stage the first kelp-vs-temperature scatter plot is
a picture of summer rather than a picture of a relationship. So every measured
feature gets an `_anom` twin -- the same quantity with that series' own
quarterly climatology subtracted -- and the analysis works in those.

Four rules make an anomaly mean something.

**The baseline window is fixed and recorded**, not "all the years we happen to
have". A window that grew with the data would silently move every anomaly ever
computed each time a backfill landed, and a figure published last year would
stop being reproducible. The window is written onto every climatology row, so
"the anomalies did not shift" is checkable by diffing two runs rather than a
promise.

**A series that cannot cover the canonical window may be given its own**, from
`policy.baseline_overrides` (docs/04 s3). Still fixed, still per-row -- an
override is a window an operator wrote down, never one derived from the years
that happen to have landed, because a derived window would grow with the record
and break the paragraph above. Resolution is by `site_id` and only when
`site_id` is in the series key, so the kelp half takes the canonical window
without knowing overrides exist. `min_years` is not overridable: two windows in
one screen is a reporting cost the analyst can see on the row, but a per-station
minimum would hide a thin baseline behind the same column name as a thick one.

**Only usable, complete quarters contribute.** A half-observed quarter cannot
drag the baseline it is later compared against, and an in-progress quarter
cannot bias the baseline toward whatever part of the year the run happened in.

**A baseline too thin to be one produces no anomaly.** Below the configured
minimum of contributing years the anomaly is null, because a difference against
a one-year mean is not an anomaly. This mirrors the reasoning docs/04 s1 already
gives for deferring the climatology *QC* test: running a baseline against a
record too short to contain one tests the data against itself.

**Anomalies are computed for unusable quarters too.** `usable` is already the
gate on this table, and two mechanisms expressing one warning is worse than one
-- the same "flag, never delete" discipline hard rule 4 applies to rows.

The climatology is written to its own table rather than recomputed and thrown
away, and it carries the standard deviation as well as the mean, so a
standardised anomaly is a join rather than a second recomputation that could
drift from the one that produced the anomalies.

## One implementation, two halves of the comparison

Nothing here is specific to the environment. Both callers pass **which columns
identify one series** and **which columns are measured features**, and get the
same arithmetic:

|          | series key                                 | measured features            |
|----------|--------------------------------------------|------------------------------|
| environment | `source, site_id, parameter, depth_m`   | from `features.json`         |
| kelp        | `polygon_id`                            | the canopy quantities        |

That is not a convenience. Kelp and environmental anomalies sit on the two sides
of every correlation the project exists to compute, and if they were produced by
two implementations, a divergence between them would present as a *result*.
Sharing the code is what makes "both sides were treated the same way" a fact
about the program rather than a claim about two of them.

What the two tables must agree on, and what this module therefore requires of
either: the series key columns, `year`, `quarter`, and the `usable` and
`quarter_complete` bookkeeping that decides who contributes to a baseline. A
series key naming a column the table does not have raises rather than producing
an empty climatology -- passing the environmental key against a kelp table is
the mistake worth catching loudly.

`CLIMATOLOGY_COLUMNS` and `CLIMATOLOGY_KEY` below are the *environmental*
specialisation, kept under their old names because that is what the storage
layer and the CLI already write `climatology_env` with.
"""

from __future__ import annotations

import pandas as pd

from kelpcompare.features.config import FeatureConfig
from kelpcompare.features.quarterly import SERIES_KEY, feature_columns, quarterly_columns

#: The environmental series key: what makes rows one QC series (docs/04 s1).
ENV_SERIES = SERIES_KEY

#: The bookkeeping every quarterly table carries, and the only columns this
#: module reads besides the series key and the features themselves.
_REQUIRED_COLUMNS = ("year", "quarter", "usable", "quarter_complete")

#: The climatology bookkeeping, in column order, after the key. Long on features
#: rather than wide: the table is a lookup keyed by feature, and a wide form
#: would need a `_mean` and a `_std` column for every feature in the table it
#: summarises.
_BOOKKEEPING = (
    "baseline_start_year",
    "baseline_end_year",
    "n_years",
    "baseline_mean",
    "baseline_std",
)

#: Types that do not depend on which series key is in play. The key columns are
#: typed from the quarterly table they summarise, so the two cannot disagree.
_DTYPES = {
    "feature": "string",
    "quarter": "int8",
    "baseline_start_year": "int32",
    "baseline_end_year": "int32",
    "n_years": "int32",
    "baseline_mean": "float64",
    "baseline_std": "float64",
}


def climatology_columns(series: tuple[str, ...]) -> tuple[str, ...]:
    """The climatology table's schema for one series key, in column order."""
    return (*series, "quarter", "feature", *_BOOKKEEPING)


def climatology_key(series: tuple[str, ...]) -> tuple[str, ...]:
    """What one climatology cell is: a series, a quarter of the year, and a feature."""
    return (*series, "quarter", "feature")


#: The docs/03 `climatology_env` table -- the environmental specialisation.
CLIMATOLOGY_COLUMNS = climatology_columns(ENV_SERIES)
CLIMATOLOGY_KEY = climatology_key(ENV_SERIES)


def quarterly_env_columns(config: FeatureConfig) -> tuple[str, ...]:
    """The full docs/03 `quarterly_env` schema, anomalies included.

    `baseline_years` opens the anomaly section rather than sitting with the other
    bookkeeping, because that is what it is for: how much weight the anomalies to
    its right carry, without a second table having to be opened.
    """
    measured, _ = feature_columns(config)
    return (*quarterly_columns(config), *anomaly_columns(measured))


def anomaly_columns(measured: tuple[str, ...]) -> tuple[str, ...]:
    """What `with_anomalies` appends, in the order it appends it.

    Stated once so a table's declared schema and what this module actually adds
    cannot drift apart -- either half of the comparison builds its column order
    out of this rather than out of a second list that has to be kept in step.
    """
    return ("baseline_years", *(f"{name}_anom" for name in measured))


def build_climatology(
    quarterly: pd.DataFrame,
    config: FeatureConfig,
    *,
    series: tuple[str, ...] = ENV_SERIES,
    measured: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """The per-series, per-quarter baseline for every measured feature.

    One row per series x quarter x feature, carrying the window it was built
    from, how many years contributed, and the mean and standard deviation of
    those years. A cell with no contributing year gets no row; the anomaly it
    would have produced is null either way, and an empty row would claim a
    baseline exists.

    `measured` defaults to the feature columns `config` declares, which is the
    environmental set. The kelp half passes its own.
    """
    columns = climatology_columns(series)
    _require_columns(quarterly, series, what="build a climatology")

    wanted = _measured_present(quarterly, config, measured)
    contributors = _contributors(quarterly, config, series)
    if contributors.empty or not wanted:
        return _typed(_empty(columns), quarterly, series)

    long = contributors.melt(
        id_vars=[*series, "quarter", "year"],
        value_vars=list(wanted),
        var_name="feature",
        value_name="value",
    ).dropna(subset=["value"])
    if long.empty:
        return _typed(_empty(columns), quarterly, series)

    grouped = long.groupby(list(climatology_key(series)), dropna=False, sort=True).agg(
        n_years=("year", "nunique"),
        baseline_mean=("value", "mean"),
        baseline_std=("value", "std"),  # sample convention; null for a single year
    )
    frame = grouped.reset_index()
    start, end = _window_bounds(frame, config, series)
    frame["baseline_start_year"] = start
    frame["baseline_end_year"] = end
    return _typed(frame.reindex(columns=list(columns)), quarterly, series)


def with_anomalies(
    quarterly: pd.DataFrame,
    climatology: pd.DataFrame,
    config: FeatureConfig,
    *,
    series: tuple[str, ...] = ENV_SERIES,
    measured: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Add `baseline_years` and an `_anom` twin for every measured feature.

    Appended in `anomaly_columns` order onto the table as given, so a caller's
    column order is preserved rather than replaced -- which is what lets the two
    halves of the comparison keep different schemas either side of this call.

    Bookkeeping columns get no twin: the table must not offer the anomaly of a
    row count. Markers get none either -- a boolean saying a spell touched a gap
    has no climatology.
    """
    _require_columns(quarterly, series, what="take anomalies")
    wanted = measured if measured is not None else feature_columns(config)[0]
    appended = anomaly_columns(wanted)

    if quarterly.empty:
        empty = quarterly.reindex(columns=[*quarterly.columns, *appended])
        return empty.astype({"baseline_years": "int32", **dict.fromkeys(appended[1:], "float64")})

    keys = _lookup_key(quarterly, series)
    built = quarterly.copy()
    contributing = _numeric(keys.map(_contributing_years(quarterly, config, series)))
    built["baseline_years"] = contributing.fillna(0).astype("int32")

    for feature in wanted:
        anomaly = f"{feature}_anom"
        if feature not in built.columns:
            built[anomaly] = pd.Series(pd.NA, index=built.index, dtype="float64")
            continue
        cells = climatology.loc[climatology["feature"] == feature]
        cell_keys = _lookup_key(cells, series)
        baseline_mean = _numeric(
            keys.map(dict(zip(cell_keys, cells["baseline_mean"], strict=True)))
        )
        n_years = _numeric(keys.map(dict(zip(cell_keys, cells["n_years"], strict=True))))
        # `where` leaves null wherever the baseline is missing or too thin, which
        # is one verdict reached by two routes: nothing to compare against.
        difference = built[feature] - baseline_mean
        built[anomaly] = difference.where(n_years >= config.baseline.min_years).astype("float64")

    return built


# --------------------------------------------------------------------------
# Who gets to be a baseline
# --------------------------------------------------------------------------


def override_warnings(
    quarterly: pd.DataFrame, config: FeatureConfig, *, series: tuple[str, ...] = ENV_SERIES
) -> tuple[str, ...]:
    """Overrides applied to a series that did not need one (docs/04 s3).

    An override exists to rescue a series whose record post-dates the canonical
    window. Applied to one that already covers it, it silently moves anomalies
    that were fine -- which is the failure the fixed window exists to prevent,
    arriving through the mechanism meant to work around it. Nothing else would
    say so: the row would carry a window, and a reader has no way to know it was
    not the one intended.

    Warned rather than refused, because the condition is data-dependent. A
    backfill that finally carried a station across the minimum would start
    failing every rebuild from then on, and a run that cannot rebuild is worse
    than one that says what it noticed -- docs/01 s5 already makes the manifest
    where a run records that.
    """
    if "site_id" not in series or not config.baseline_overrides or quarterly.empty:
        return ()

    canonical = config.baseline
    overridden = quarterly.loc[quarterly["site_id"].isin(config.baseline_overrides)]
    if overridden.empty:
        return ()

    # What each overridden series *would* have had under the canonical window.
    inside = overridden["year"].between(canonical.start_year, canonical.end_year)
    eligible = overridden.loc[inside & overridden["usable"] & overridden["quarter_complete"]]
    if eligible.empty:
        return ()

    counted = eligible.groupby(["site_id", "quarter"], dropna=False, sort=True)["year"].nunique()
    warnings = []
    for site_id, years in counted.groupby(level="site_id").max().items():
        if years < canonical.min_years:
            continue
        window = config.baseline_for(str(site_id))
        warnings.append(
            f"{site_id}: declared baseline {window.label} overrides the canonical "
            f"{canonical.label}, but this series has {years} usable complete year(s) inside "
            f"{canonical.label} against a min_years of {canonical.min_years} -- it did not need "
            "an override, and its anomalies have moved off the canonical window"
        )
    return tuple(warnings)


def _window_bounds(
    frame: pd.DataFrame, config: FeatureConfig, series: tuple[str, ...]
) -> tuple[pd.Series, pd.Series]:
    """Each row's baseline window, which is the canonical one unless declared otherwise.

    Resolved by `site_id`, and only when `site_id` is part of the series key.
    That is what keeps the kelp half out of this: it is keyed on `polygon_id`,
    so no override can match it and every kelp row takes the canonical window,
    without the kelp side needing to know overrides exist.
    """
    if "site_id" not in series or not config.baseline_overrides:
        start = pd.Series(config.baseline.start_year, index=frame.index, dtype="int64")
        return start, pd.Series(config.baseline.end_year, index=frame.index, dtype="int64")

    windows = frame["site_id"].map(lambda site: config.baseline_for(_site_or_none(site)))
    return (
        windows.map(lambda window: window.start_year).astype("int64"),
        windows.map(lambda window: window.end_year).astype("int64"),
    )


def _site_or_none(site) -> str | None:
    """A null `site_id` names no site, so it takes the canonical window."""
    return None if pd.isna(site) else str(site)


def _contributors(
    quarterly: pd.DataFrame, config: FeatureConfig, series: tuple[str, ...]
) -> pd.DataFrame:
    """Usable, complete, and inside that series' window. All three, deliberately."""
    if quarterly.empty:
        return quarterly
    start, end = _window_bounds(quarterly, config, series)
    inside = (quarterly["year"] >= start) & (quarterly["year"] <= end)
    return quarterly.loc[inside & quarterly["usable"] & quarterly["quarter_complete"]]


def _contributing_years(
    quarterly: pd.DataFrame, config: FeatureConfig, series: tuple[str, ...]
) -> dict[str, int]:
    """How many years stand behind each series-quarter's baseline.

    Counted per series rather than per feature, so one number can be carried on
    the feature row. The per-feature count in the climatology table is what
    actually gates each anomaly; the two differ only where a feature is null in
    an otherwise-usable quarter, which nothing in the current feature set does.
    """
    contributors = _contributors(quarterly, config, series)
    if contributors.empty:
        return {}
    grouping = [*series, "quarter"]
    counted = contributors.groupby(grouping, dropna=False, sort=False)["year"].nunique()
    return dict(zip(_lookup_key(counted.reset_index(), series), counted.to_numpy(), strict=True))


def _measured_present(
    quarterly: pd.DataFrame, config: FeatureConfig, measured: tuple[str, ...] | None
) -> tuple[str, ...]:
    declared = measured if measured is not None else feature_columns(config)[0]
    return tuple(name for name in declared if name in quarterly.columns)


# --------------------------------------------------------------------------
# Frame shape
# --------------------------------------------------------------------------


def _require_columns(quarterly: pd.DataFrame, series: tuple[str, ...], *, what: str) -> None:
    """Refuse a table that is not keyed the way the caller says it is.

    The mistake this catches is passing one half's series key against the other
    half's table -- which would otherwise raise somewhere inside a groupby, or
    worse, produce an empty climatology that reads as "no baseline yet".
    """
    missing = [name for name in (*series, *_REQUIRED_COLUMNS) if name not in quarterly.columns]
    if missing:
        raise ValueError(
            f"cannot {what} keyed on {list(series)}: the quarterly table has no {missing} "
            f"column(s). It carries {list(quarterly.columns)[:8]}..."
        )


def join_key(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    """A joinable text key over any set of columns, because one may hold nulls.

    A merge on a key column holding nulls -- `depth_m` is null for every met
    parameter -- is exactly the kind of thing that works until it does not, so
    the key is built explicitly instead. `repr` of a float round-trips exactly,
    and an absent field is empty rather than a value that might compare equal to
    something.

    Public because the comparison table joins on the same series key across two
    tables and must do it the same way this module does, or the two could
    disagree about which rows are the same series.
    """
    fields = [frame[name].map(_field_text).astype("string") for name in columns]
    return fields[0].str.cat(fields[1:], sep="|", na_rep="") if len(fields) > 1 else fields[0]


def _lookup_key(frame: pd.DataFrame, series: tuple[str, ...]) -> pd.Series:
    """The series-and-quarter key an anomaly is looked up by."""
    return join_key(frame, (*series, "quarter"))


def _field_text(value) -> str:
    """One key field as text. Typed rather than named, so any key column works."""
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        return repr(float(value))
    return str(value)


def _numeric(mapped: pd.Series) -> pd.Series:
    """`Series.map` over a plain dict comes back as `object`; comparisons need floats."""
    return pd.to_numeric(mapped, errors="coerce")


def _empty(columns: tuple[str, ...]) -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series(dtype="object") for name in columns})


def _typed(frame: pd.DataFrame, quarterly: pd.DataFrame, series: tuple[str, ...]) -> pd.DataFrame:
    """Sorted and typed, so two runs over unchanged inputs write the same bytes.

    Key columns take the dtype they have on the table being summarised, rather
    than a second declaration here that could drift from it.
    """
    types = {**_DTYPES, **{name: quarterly[name].dtype for name in series}}
    ordered = frame.sort_values(list(climatology_key(series)), kind="stable", na_position="last")
    return ordered.reset_index(drop=True).astype(types)


__all__ = [
    "CLIMATOLOGY_COLUMNS",
    "CLIMATOLOGY_KEY",
    "ENV_SERIES",
    "anomaly_columns",
    "build_climatology",
    "climatology_columns",
    "climatology_key",
    "join_key",
    "quarterly_env_columns",
    "with_anomalies",
]
