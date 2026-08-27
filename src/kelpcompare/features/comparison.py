"""The analysis-ready join: kelp at *t* against the environment at *t − lag*.

This is the table docs/01 s4 has always ended at and the one notebooks and the
dashboard read almost exclusively (docs/03). One row per polygon x environmental
series x quarter x lag, for lags 0 to 4, carrying the kelp anomaly at quarter
*t* beside every environmental feature anomaly at *t − lag*.

**The lag has one direction and it is written down: the environment leads, kelp
responds.** Lag 2 on a 2015Q3 row means kelp in 2015Q3 against the water in
2015Q1. Getting the sign backwards would not raise anything -- it would produce
a correlation matrix that reads as kelp predicting temperature, which is a
result rather than an error, so the direction is stated here, asserted in the
tests on a hand-built pair, and documented in docs/04 s4.1.

**Which polygon pairs with which site comes from the registry.** `polygons.geojson`
names the `site_id`s each polygon is compared against, so nothing here
string-matches a polygon name against a station name -- the docs/03 integrity
rule, applied at the one place that would be tempted to break it.

**A row exists wherever kelp does.** The response variable defines the row, and
the environment is joined onto it. So a lag that reaches back before the
environmental record starts produces a row with nulls on the environmental side,
not a missing row -- which is what makes "the environmental record does not
reach this quarter" a queryable fact rather than an absence to be inferred.

**Both usability flags are carried, and nothing is filtered.** `usable` stays
the single gate, applied once by the analysis, rather than becoming a hidden
deletion here. A row where either side is unusable is still a row; a reader who
filters on both flags gets the same answer this stage would have given, and can
see what filtering cost.

The table is regenerated wholesale, so it is a pure function of the two
quarterly tables and the polygon registry and cannot accumulate stale rows.
"""

from __future__ import annotations

import pandas as pd

from kelpcompare.features.climatology import ENV_SERIES, join_key
from kelpcompare.features.kelp import KELP_SERIES
from kelpcompare.features.quarters import shift_quarters
from kelpcompare.polygons import Polygons

#: The lags the docs/04 s4.1 screen runs over, in quarters. Zero included: a
#: same-quarter association is a hypothesis like any other, and leaving it out
#: would make its absence look like a finding.
LAGS = (0, 1, 2, 3, 4)

#: What identifies one comparison row. The polygon, the environmental series it
#: is being compared against, the kelp quarter, and the lag.
COMPARISON_KEY = (
    "polygon_id",
    "env_source",
    "site_id",
    "parameter",
    "depth_m",
    "year",
    "quarter",
    "lag",
)

#: Columns that place and qualify the row, before any anomaly.
_CONTEXT_COLUMNS = (
    "env_year",
    "env_quarter",
    "kelp_usable",
    "env_usable",
    "kelp_watch_revision",
)

_DTYPES = {
    "polygon_id": "string",
    "env_source": "string",
    "site_id": "string",
    "parameter": "string",
    "depth_m": "float64",
    "year": "int32",
    "quarter": "int8",
    "lag": "int8",
    "env_year": "int32",
    "env_quarter": "int8",
    "kelp_usable": "bool",
    # Nullable: a lag reaching before the environmental record has no row to
    # take a verdict from, and False would claim one was reached.
    "env_usable": "boolean",
    "kelp_watch_revision": "int32",
}

#: The environmental series key as it is named on a comparison row. `source`
#: becomes `env_source`, because a row carries two sources' worth of provenance
#: and a bare `source` would not say which half it described.
_ENV_RENAMED = {"source": "env_source"}


def comparison_columns(
    kelp_anomalies: tuple[str, ...], env_anomalies: tuple[str, ...]
) -> tuple[str, ...]:
    """The `comparison` schema, in column order."""
    return (*COMPARISON_KEY, *_CONTEXT_COLUMNS, *kelp_anomalies, *env_anomalies)


def build_comparison(
    quarterly_kelp: pd.DataFrame,
    quarterly_env: pd.DataFrame,
    polygons: Polygons,
    *,
    kelp_anomalies: tuple[str, ...],
    env_anomalies: tuple[str, ...],
    lags: tuple[int, ...] = LAGS,
) -> pd.DataFrame:
    """Join the two quarterly tables at every lag, per registry-declared pair.

    A pure function: two frames and the registry in, one frame out. Nothing is
    read from disk and nothing is written, so a notebook can rebuild it at a
    different set of lags without producing a competing file of record.

    `kelp_anomalies` and `env_anomalies` are the `_anom` column names each half
    produced. They are passed rather than discovered, so a comparison table
    built from a stale frame cannot silently carry a narrower feature set than
    the run that built it thought it had.
    """
    columns = comparison_columns(kelp_anomalies, env_anomalies)
    pairs = _pairs(quarterly_env, polygons)
    if quarterly_kelp.empty or pairs.empty:
        return _typed(pd.DataFrame({name: pd.Series(dtype="object") for name in columns}), columns)

    kelp = _kelp_side(quarterly_kelp, kelp_anomalies)
    grid = pairs.merge(kelp, on="polygon_id", how="inner")
    if grid.empty:
        return _typed(pd.DataFrame({name: pd.Series(dtype="object") for name in columns}), columns)

    env = _env_side(quarterly_env, env_anomalies)
    lagged = [_at_lag(grid, env, lag=lag) for lag in lags]
    return _typed(pd.concat(lagged, ignore_index=True), columns)


# --------------------------------------------------------------------------
# The two sides, and the pairs between them
# --------------------------------------------------------------------------


def _pairs(quarterly_env: pd.DataFrame, polygons: Polygons) -> pd.DataFrame:
    """Every (polygon, environmental series) pair the registry declares.

    The series come from the environmental table rather than from the registry,
    because the registry names *sites* and a site carries several series -- one
    per parameter and depth. Which parameters a site actually produced is a fact
    about the data, and inventing a pair for a parameter nobody measured would
    fill the table with rows that can never be anything but null.
    """
    if quarterly_env.empty:
        return pd.DataFrame(columns=["polygon_id", *ENV_SERIES])

    series = (
        quarterly_env[list(ENV_SERIES)]
        .drop_duplicates()
        .sort_values(list(ENV_SERIES), kind="stable", na_position="last")
        .reset_index(drop=True)
    )
    rows = [
        {"polygon_id": polygon.polygon_id, **row}
        for polygon in polygons
        for row in series.to_dict("records")
        if row["site_id"] in polygon.site_ids
    ]
    return pd.DataFrame(rows, columns=["polygon_id", *ENV_SERIES])


def _kelp_side(quarterly_kelp: pd.DataFrame, anomalies: tuple[str, ...]) -> pd.DataFrame:
    """The response variable's half of a row: when, how anomalous, and how good."""
    wanted = [*KELP_SERIES, "year", "quarter", "usable", "kelp_watch_revision", *anomalies]
    side = quarterly_kelp.reindex(columns=wanted).copy()
    return side.rename(columns={"usable": "kelp_usable"})


def _env_side(quarterly_env: pd.DataFrame, anomalies: tuple[str, ...]) -> pd.DataFrame:
    """The predictor half, keyed for lookup at whatever quarter a lag lands on."""
    wanted = [*ENV_SERIES, "year", "quarter", "usable", *anomalies]
    side = quarterly_env.reindex(columns=wanted).copy()
    side["_key"] = join_key(side, (*ENV_SERIES, "year", "quarter"))
    return side.rename(columns={"usable": "env_usable"}).drop(columns=["year", "quarter"])


def _at_lag(grid: pd.DataFrame, env: pd.DataFrame, *, lag: int) -> pd.DataFrame:
    """One lag's rows: the kelp quarter fixed, the environmental quarter moved back.

    Joined on an explicit text key rather than merged on the columns, because
    `depth_m` is null for every met parameter and a merge on a key column
    holding nulls is the kind of thing that works until it does not -- the same
    reasoning, and the same helper, the climatology uses.
    """
    rows = grid.copy()
    rows["lag"] = lag
    shifted = [
        shift_quarters(int(year), int(quarter), -lag)
        for year, quarter in zip(rows["year"], rows["quarter"], strict=True)
    ]
    rows["env_year"] = [year for year, _ in shifted]
    rows["env_quarter"] = [quarter for _, quarter in shifted]

    # Built as its own frame rather than by renaming `env_year` to `year` in
    # place: the row already carries the *kelp* year, and a rename would leave
    # two columns of that name and silently key the join off the wrong one.
    wanted = rows[list(ENV_SERIES)].copy()
    wanted["year"] = rows["env_year"]
    wanted["quarter"] = rows["env_quarter"]

    lookup = env.set_index("_key")
    # `reindex` rather than a merge: it returns exactly one row per kelp row by
    # construction, so a duplicated environmental row cannot multiply the
    # comparison table. Only the *value* columns are taken across -- reading the
    # series columns back from the match would blank them wherever the lag found
    # nothing, turning a null environment into a null pair.
    values = [column for column in env.columns if column not in {*ENV_SERIES, "_key"}]
    matched = lookup.reindex(join_key(wanted, (*ENV_SERIES, "year", "quarter")))
    for column in values:
        rows[column] = matched[column].to_numpy()
    return rows.rename(columns=_ENV_RENAMED)


def _typed(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    """Ordered, sorted and typed, so two runs over unchanged inputs match.

    Anomaly columns are `float64` throughout, on both halves: every one of them
    is null wherever a baseline was too thin or a lag reached past the record,
    and a column that cannot hold null would have to invent a value there.
    """
    ordered = frame.reindex(columns=list(columns))
    ordered = ordered.sort_values(list(COMPARISON_KEY), kind="stable", na_position="last")
    types = {
        **_DTYPES,
        **dict.fromkeys((c for c in columns if c.endswith("_anom")), "float64"),
    }
    return ordered.reset_index(drop=True).astype(
        {name: kind for name, kind in types.items() if name in ordered.columns}
    )


__all__ = [
    "COMPARISON_KEY",
    "LAGS",
    "build_comparison",
    "comparison_columns",
]
