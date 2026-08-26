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
"""

from __future__ import annotations

import pandas as pd

from kelpcompare.features.config import Baseline, FeatureConfig
from kelpcompare.features.quarterly import feature_columns, quarterly_columns

#: The docs/03 `climatology_env` table, in column order. Long on features rather
#: than wide: the table is a lookup keyed by feature, and a wide form would need
#: a `_mean` and a `_std` column for every feature in `quarterly_env`.
CLIMATOLOGY_COLUMNS = (
    "source",
    "site_id",
    "parameter",
    "depth_m",
    "quarter",
    "feature",
    "baseline_start_year",
    "baseline_end_year",
    "n_years",
    "baseline_mean",
    "baseline_std",
)

#: What one climatology cell is: a series, a quarter of the year, and a feature.
CLIMATOLOGY_KEY = ("source", "site_id", "parameter", "depth_m", "quarter", "feature")

#: The series-and-quarter key an anomaly is looked up by.
_SERIES_QUARTER = ("source", "site_id", "parameter", "depth_m", "quarter")

_DTYPES = {
    "source": "string",
    "site_id": "string",
    "parameter": "string",
    "feature": "string",
    "depth_m": "float64",
    "quarter": "int8",
    "baseline_start_year": "int32",
    "baseline_end_year": "int32",
    "n_years": "int32",
    "baseline_mean": "float64",
    "baseline_std": "float64",
}


def quarterly_env_columns(config: FeatureConfig) -> tuple[str, ...]:
    """The full docs/03 `quarterly_env` schema, anomalies included.

    `baseline_years` opens the anomaly section rather than sitting with the other
    bookkeeping, because that is what it is for: how much weight the anomalies to
    its right carry, without a second table having to be opened.
    """
    measured, _ = feature_columns(config)
    return (
        *quarterly_columns(config),
        "baseline_years",
        *(f"{name}_anom" for name in measured),
    )


def build_climatology(quarterly: pd.DataFrame, config: FeatureConfig) -> pd.DataFrame:
    """The per-series, per-quarter baseline for every measured feature.

    One row per series x quarter x feature, carrying the window it was built
    from, how many years contributed, and the mean and standard deviation of
    those years. A cell with no contributing year gets no row; the anomaly it
    would have produced is null either way, and an empty row would claim a
    baseline exists.
    """
    measured = _measured_present(quarterly, config)
    contributors = _contributors(quarterly, config.baseline)
    if contributors.empty or not measured:
        return _typed(
            pd.DataFrame({name: pd.Series(dtype="object") for name in CLIMATOLOGY_COLUMNS})
        )

    long = contributors.melt(
        id_vars=[*_SERIES_QUARTER, "year"],
        value_vars=list(measured),
        var_name="feature",
        value_name="value",
    ).dropna(subset=["value"])
    if long.empty:
        return _typed(
            pd.DataFrame({name: pd.Series(dtype="object") for name in CLIMATOLOGY_COLUMNS})
        )

    grouped = long.groupby(list(CLIMATOLOGY_KEY), dropna=False, sort=True).agg(
        n_years=("year", "nunique"),
        baseline_mean=("value", "mean"),
        baseline_std=("value", "std"),  # sample convention; null for a single year
    )
    frame = grouped.reset_index()
    frame["baseline_start_year"] = config.baseline.start_year
    frame["baseline_end_year"] = config.baseline.end_year
    return _typed(frame.reindex(columns=list(CLIMATOLOGY_COLUMNS)))


def with_anomalies(
    quarterly: pd.DataFrame, climatology: pd.DataFrame, config: FeatureConfig
) -> pd.DataFrame:
    """Add `baseline_years` and an `_anom` twin for every measured feature.

    Bookkeeping columns get no twin: the table must not offer the anomaly of a
    row count. Markers get none either -- a boolean saying a spell touched a gap
    has no climatology.
    """
    measured, _ = feature_columns(config)
    columns = quarterly_env_columns(config)
    if quarterly.empty:
        empty = quarterly.reindex(columns=list(columns))
        return empty.astype(
            {
                "baseline_years": "int32",
                **dict.fromkeys((f"{name}_anom" for name in measured), "float64"),
            }
        )

    keys = _lookup_key(quarterly)
    built = quarterly.copy()
    contributing = _numeric(keys.map(_contributing_years(quarterly, config.baseline)))
    built["baseline_years"] = contributing.fillna(0).astype("int32")

    for feature in measured:
        anomaly = f"{feature}_anom"
        if feature not in built.columns:
            built[anomaly] = pd.Series(pd.NA, index=built.index, dtype="float64")
            continue
        cells = climatology.loc[climatology["feature"] == feature]
        cell_keys = _lookup_key(cells)
        baseline_mean = _numeric(
            keys.map(dict(zip(cell_keys, cells["baseline_mean"], strict=True)))
        )
        n_years = _numeric(keys.map(dict(zip(cell_keys, cells["n_years"], strict=True))))
        # `where` leaves null wherever the baseline is missing or too thin, which
        # is one verdict reached by two routes: nothing to compare against.
        difference = built[feature] - baseline_mean
        built[anomaly] = difference.where(n_years >= config.baseline.min_years).astype("float64")

    return built.reindex(columns=list(columns))


# --------------------------------------------------------------------------
# Who gets to be a baseline
# --------------------------------------------------------------------------


def _contributors(quarterly: pd.DataFrame, baseline: Baseline) -> pd.DataFrame:
    """Usable, complete, and inside the fixed window. All three, deliberately."""
    if quarterly.empty:
        return quarterly
    inside = quarterly["year"].between(baseline.start_year, baseline.end_year)
    return quarterly.loc[inside & quarterly["usable"] & quarterly["quarter_complete"]]


def _contributing_years(quarterly: pd.DataFrame, baseline: Baseline) -> dict[str, int]:
    """How many years stand behind each series-quarter's baseline.

    Counted per series rather than per feature, so one number can be carried on
    the feature row. The per-feature count in the climatology table is what
    actually gates each anomaly; the two differ only where a feature is null in
    an otherwise-usable quarter, which nothing in the current feature set does.
    """
    contributors = _contributors(quarterly, baseline)
    if contributors.empty:
        return {}
    counted = contributors.groupby(list(_SERIES_QUARTER), dropna=False, sort=False)[
        "year"
    ].nunique()
    return dict(zip(_lookup_key(counted.reset_index()), counted.to_numpy(), strict=True))


def _measured_present(quarterly: pd.DataFrame, config: FeatureConfig) -> tuple[str, ...]:
    measured, _ = feature_columns(config)
    return tuple(name for name in measured if name in quarterly.columns)


def _lookup_key(frame: pd.DataFrame) -> pd.Series:
    """A joinable text key, because `depth_m` is null for every met parameter.

    A merge on a key column holding nulls is exactly the kind of thing that works
    until it does not, so the key is built explicitly instead: `repr` of the
    float round-trips exactly, and an absent depth is an empty field rather than
    a value that might compare equal to something.
    """
    fields = [
        frame["source"].astype("string"),
        frame["site_id"].astype("string"),
        frame["parameter"].astype("string"),
        frame["depth_m"].map(_depth_text).astype("string"),
        frame["quarter"].astype("string"),
    ]
    return fields[0].str.cat(fields[1:], sep="|", na_rep="")


def _depth_text(value) -> str:
    return "" if pd.isna(value) else repr(float(value))


def _numeric(mapped: pd.Series) -> pd.Series:
    """`Series.map` over a plain dict comes back as `object`; comparisons need floats."""
    return pd.to_numeric(mapped, errors="coerce")


def _typed(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = frame.sort_values(list(CLIMATOLOGY_KEY), kind="stable", na_position="last")
    return ordered.reset_index(drop=True).astype(_DTYPES)


__all__ = [
    "CLIMATOLOGY_COLUMNS",
    "CLIMATOLOGY_KEY",
    "build_climatology",
    "quarterly_env_columns",
    "with_anomalies",
]
