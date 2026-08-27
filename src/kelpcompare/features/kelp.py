"""One row per polygon per quarter (docs/03 `quarterly_kelp`, docs/04 s2-s3).

The response variable, in the same shape and on the same calendar as the
environmental half. There is no aggregation here of the kind `quarterly.py`
does: a Kelp Watch export is already one row per quarter, summed over the
geometry the operator selected (docs/02). What this stage adds is the
bookkeeping that makes such a row interpretable, and the anomalies that make it
comparable -- taken through the *same* climatology code the environmental half
uses, so the two sides of a correlation cannot have been treated differently.

Three decisions carry the weight.

**Coverage is the fraction of the bed that was seen.** `n_cells_observed` over
`n_cells` -- the cloud-free cells over the bed's whole historic footprint. Both
are stored beside the fraction, exactly as `n_obs` and `expected_obs` are on the
environmental side, so the number is auditable rather than trusted.

**A partially observed quarter is biased low, not merely noisy**, and that is
worse than the environmental analogue. `kelp_area_m2` is a *sum over the cells
that were seen*, so a quarter with two thirds of its bed under cloud reports
roughly two thirds of the canopy that was there. Nothing here corrects for it:
scaling by the observed fraction would be imputation wearing a feature's
clothes, and it would assume the unseen part of a bed looks like the seen part,
which is exactly what a patchy bed does not do. The quarter is flagged
`usable = false` below the floor and keeps its value, which is hard rule 4's
discipline applied one layer up. The bias is disclosed in docs/04 rather than
mitigated.

**Nothing is imputed and nothing is dropped.** A quarter with no cloud-free cell
at all arrives from the parser with a null value (docs/02), and it keeps that
null and its row: it is the record of a cloud gap, and dropping it would make a
hole in the series indistinguishable from a quarter the export never covered.

Both measured quantities get anomalies. `kelp_area_m2` is how much canopy there
was; `n_cells_kelp` is how far it spread. A bed can thin without shrinking and
shrink without thinning, so the notebook chooses which one answers its question
rather than this stage choosing for it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from kelpcompare.features.climatology import (
    anomaly_columns,
    build_climatology,
    climatology_key,
    with_anomalies,
)
from kelpcompare.features.config import FeatureConfig
from kelpcompare.features.quarters import is_complete, quarter_label

#: What makes rows one kelp series: the route the numbers took, and the area
#: they describe. A canopy value belongs to an area rather than to an instrument,
#: which is the whole reason this table exists beside `quarterly_env` instead of
#: inside it -- but `source` joins it for the reason it is in the environmental
#: key too. It is what the features zone scopes a replacement by, so a table
#: without it cannot be superseded and would double on every build; and if the
#: published data package ever becomes a second route to the same polygons, the
#: two must not merge into one baseline.
KELP_SERIES = ("source", "polygon_id")

#: The docs/03 `quarterly_kelp` row key: the series key plus time.
QUARTERLY_KELP_KEY = (*KELP_SERIES, "year", "quarter")

#: What one row of the *parser's* output is. `source` is not on it yet -- this
#: stage is what stamps it -- and a parsed frame is one source's rows by
#: construction, so the polygon and the quarter are the whole identity there.
_PARSED_KEY = ("polygon_id", "year", "quarter")

#: The `climatology_kelp` key and column order, from the shared builder.
CLIMATOLOGY_KELP_KEY = climatology_key(KELP_SERIES)

#: What the analysis measures, and therefore what gets an `_anom` twin. Area is
#: how much canopy there was; the cell count is how far it spread.
MEASURED = ("kelp_area_m2", "n_cells_kelp")

#: Columns that describe the row rather than measure the kelp. No `_anom` twin,
#: for the reason `quarterly_env` gives: the table must not offer the anomaly of
#: a cell count that is a denominator.
BOOKKEEPING_COLUMNS = (
    "n_cells_observed",
    "n_cells",
    "pct_cells_observed",
    "usable",
    "quarter_complete",
)

#: Provenance beyond the key. `kelp_watch_revision` is on every row because the
#: export carries no version of its own, so this is the only place a number can
#: be traced back to a citable dataset (docs/02).
#:
#: Deliberately no `fetch_run_id`. On an observation row that records which
#: *fetch* landed it and survives every later rewrite; on a derived table it
#: would be the build run, which changes on every build and would make two runs
#: over unchanged inputs write different bytes. `quarterly_env` carries none for
#: the same reason, and the manifest already records what each run produced.
PROVENANCE_COLUMNS = ("kelp_watch_revision",)

_DTYPES = {
    "polygon_id": "string",
    "year": "int32",
    "quarter": "int8",
    "kelp_area_m2": "float64",
    "n_cells_kelp": "float64",
    "n_cells_observed": "int32",
    "n_cells": "int32",
    "pct_cells_observed": "float64",
    "usable": "bool",
    "quarter_complete": "bool",
    "source": "string",
    "kelp_watch_revision": "int32",
    "baseline_years": "int32",
    "kelp_area_m2_anom": "float64",
    "n_cells_kelp_anom": "float64",
}


def quarterly_kelp_columns() -> tuple[str, ...]:
    """The full docs/03 `quarterly_kelp` schema, anomalies included."""
    return (
        *QUARTERLY_KELP_KEY,
        *MEASURED,
        *BOOKKEEPING_COLUMNS,
        *PROVENANCE_COLUMNS,
        *anomaly_columns(MEASURED),
    )


@dataclass(frozen=True)
class PolygonQuarters:
    """One built polygon, in the shape the run manifest records (docs/03)."""

    polygon_id: str
    rows: int
    quarters: int
    quarters_usable: int
    quarters_observed: int
    first_quarter: str | None
    last_quarter: str | None


@dataclass(frozen=True)
class KelpOutcome:
    """Both kelp tables, plus what the run should report about them."""

    quarterly: pd.DataFrame
    climatology: pd.DataFrame
    polygons: tuple[PolygonQuarters, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def quarters(self) -> int:
        return len(self.quarterly)

    @property
    def usable(self) -> int:
        return int(self.quarterly["usable"].sum()) if len(self.quarterly) else 0


def build_kelp(
    parsed: pd.DataFrame,
    config: FeatureConfig,
    *,
    source: str,
    revision: int,
    now: pd.Timestamp | None = None,
) -> KelpOutcome:
    """Turn parsed Kelp Watch rows into `quarterly_kelp` and `climatology_kelp`.

    A pure function -- frames in, frames out, nothing read from disk and nothing
    written -- so a sensitivity rerun at a different coverage floor can happen in
    a notebook without producing a competing file of record (hard rule 7 forbids
    bypassing the CLI to *write*, not to read).

    `parsed` is the concatenation of `fetchers.kelpwatch.parse` outputs, one per
    polygon. Two rows for one polygon-quarter raise rather than being averaged:
    that can only mean two exports were read as one series, and averaging them
    would produce a plausible number from an incoherent input.
    """
    stamp = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    columns = quarterly_kelp_columns()
    if parsed.empty:
        empty = _typed(pd.DataFrame({name: pd.Series(dtype="object") for name in columns}))
        return KelpOutcome(
            quarterly=empty,
            climatology=build_climatology(empty, config, series=KELP_SERIES, measured=MEASURED),
        )

    _refuse_duplicates(parsed)
    quarterly = _bookkeeping(parsed, config, source=source, revision=revision, now=stamp)

    climatology = build_climatology(quarterly, config, series=KELP_SERIES, measured=MEASURED)
    built = with_anomalies(quarterly, climatology, config, series=KELP_SERIES, measured=MEASURED)

    return KelpOutcome(
        quarterly=_typed(built.reindex(columns=list(columns))),
        climatology=climatology,
        polygons=_polygons(built),
        warnings=_warnings(built, config),
    )


# --------------------------------------------------------------------------
# The bookkeeping that makes a row interpretable
# --------------------------------------------------------------------------


def _bookkeeping(
    parsed: pd.DataFrame,
    config: FeatureConfig,
    *,
    source: str,
    revision: int,
    now: pd.Timestamp,
) -> pd.DataFrame:
    """Coverage, usability, completeness and provenance, on every row.

    The coverage floor is the environmental one, shared deliberately. It answers
    the same question on both halves -- how much of the thing was actually
    observed -- and a second knob with no separate evidence behind it would be a
    knob nobody could tune. It stays a sensitivity knob either way, since the
    value survives the flag.
    """
    frame = parsed.copy()
    observed = frame["n_cells_observed"].astype("float64")
    footprint = frame["n_cells"].astype("float64")

    # A polygon with no historic footprint has no denominator, so there is no
    # scale on which to judge any of its quarters. Zero rather than a division
    # by zero, and `usable` says the same thing without a reader inferring it.
    frame["pct_cells_observed"] = (observed / footprint).where(footprint > 0, 0.0)
    frame["usable"] = (frame["n_cells_observed"] > 0) & (
        frame["pct_cells_observed"] >= config.coverage_floor
    )
    frame["quarter_complete"] = [
        is_complete(int(year), int(quarter), now)
        for year, quarter in zip(frame["year"], frame["quarter"], strict=True)
    ]
    frame["source"] = source
    frame["kelp_watch_revision"] = revision
    return frame.sort_values(list(QUARTERLY_KELP_KEY), kind="stable").reset_index(drop=True)


def _refuse_duplicates(parsed: pd.DataFrame) -> None:
    """One row per polygon-quarter, or nothing.

    The case this catches is two exports of one polygon read together -- two
    dataset revisions, or the same bed selected twice under different filenames.
    Averaging them would produce a plausible series from an incoherent input,
    and a plausible wrong number is the one failure this project cannot afford.
    """
    duplicated = parsed.loc[parsed.duplicated(subset=list(_PARSED_KEY))]
    if len(duplicated):
        labels = sorted(
            {
                f"{row.polygon_id} {quarter_label(row.year, row.quarter)}"
                for row in duplicated.head(5).itertuples()
            }
        )
        raise ValueError(
            f"{len(duplicated)} polygon-quarter(s) appear more than once: {labels}. Two "
            "exports of one polygon cannot be read as one series -- check that each landing "
            "is a different polygon and that only one dataset revision is being read."
        )


def _polygons(built: pd.DataFrame) -> tuple[PolygonQuarters, ...]:
    """What the run manifest records per polygon.

    `quarters_observed` sits beside `quarters_usable` because they answer
    different questions: how many quarters the satellite saw at all, and how
    many of those were seen well enough to believe. A bed can be fully observed
    and mostly unusable, or the reverse, and one number cannot say which.
    """
    entries = []
    for polygon_id, group in built.groupby("polygon_id", sort=True):
        labels = [quarter_label(row.year, row.quarter) for row in group.itertuples()]
        entries.append(
            PolygonQuarters(
                polygon_id=str(polygon_id),
                rows=len(group),
                quarters=len(group),
                quarters_usable=int(group["usable"].sum()),
                quarters_observed=int((group["n_cells_observed"] > 0).sum()),
                first_quarter=labels[0] if labels else None,
                last_quarter=labels[-1] if labels else None,
            )
        )
    return tuple(entries)


def _warnings(built: pd.DataFrame, config: FeatureConfig) -> tuple[str, ...]:
    """What a reader should be told without opening the Parquet.

    Both are about coverage, and both are facts about the upstream product
    rather than about this run -- which is why they are reported once per
    polygon rather than once per quarter.
    """
    warnings = []
    for polygon_id, group in built.groupby("polygon_id", sort=True):
        blind = int((group["n_cells_observed"] == 0).sum())
        thin = int(((group["n_cells_observed"] > 0) & ~group["usable"]).sum())
        if blind:
            warnings.append(
                f"{polygon_id}: {blind} of {len(group)} quarters had no cloud-free "
                f"observation and carry no value"
            )
        if thin:
            warnings.append(
                f"{polygon_id}: {thin} quarter(s) were observed below the "
                f"{config.coverage_floor:.0%} coverage floor and are flagged unusable -- "
                "canopy area is a sum over observed cells, so those values run low"
            )
    return tuple(warnings)


def _typed(frame: pd.DataFrame) -> pd.DataFrame:
    """Fixed dtypes, so two runs over unchanged inputs write the same bytes."""
    return frame.astype({name: kind for name, kind in _DTYPES.items() if name in frame.columns})


__all__ = [
    "BOOKKEEPING_COLUMNS",
    "CLIMATOLOGY_KELP_KEY",
    "KELP_SERIES",
    "MEASURED",
    "QUARTERLY_KELP_KEY",
    "KelpOutcome",
    "PolygonQuarters",
    "build_kelp",
    "quarterly_kelp_columns",
]
