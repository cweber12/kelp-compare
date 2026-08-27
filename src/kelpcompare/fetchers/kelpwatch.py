"""Kelp Watch CSV exports -> one tidy row per polygon-quarter (docs/02).

The only module that knows what a Kelp Watch export looks like (docs/01 layer 1),
and the first source module with no `fetch`. The export is downloaded by hand
from kelpwatch.org and dropped in `raw/kelpwatch/incoming/`, because the
published data package it is a view of sits behind an authentication wall
(docs/02). So this is a parser and nothing else: there is no URL to build, no
outage to survive, and no `SourceUnavailable` path.

It lives here rather than in `adapters/` because that package is the
project-sensor vendor-file contract (docs/06) -- a serial, a deployment window,
a series map, none of which a public canopy product has. What makes a module
belong here is knowing a source's format, and this knows one.

Three things about the format carry the weight, all verified against real
exports on 2026-08-26 and recorded in `tests/fixtures/kelpwatch/`.

**A quarter nobody could see is written as a zero.** The published field
dictionary says an obstructed scene has "no numerical value"; it has a `0`, and
so do its cell counts. An unobserved quarter and a genuinely empty one are
identical in every column except `count_cells_no_clouds`. Read naively that
fabricates a zero-canopy measurement -- the exact thing hard rule 3 exists to
forbid -- and it fabricates them where they hide best: in winter, where the
cloud gaps are, and in marginal beds, where zero is the normal reading. So the
rule this module applies is that **zero observed cells means the quarter has no
value**, and the value becomes null.

**The per-year `max` row is not a quarter.** Every year but the last carries a
fifth row whose `quarter` is the token `max`. It is a *column-wise* maximum, so
its area and its cell counts can come from different quarters -- it is not even
"the best quarter". It is dropped, and the drop is reported.

**No export says which geometry it describes.** The polygon is supplied by the
caller, which got it from the registry by filename (docs/03). Nothing here
guesses one.

Anything else surprising -- a column that is not there, a quarter token nobody
has seen, a footprint that moves inside one file, a cell count larger than the
area it sits in -- stops the parse rather than entering the record. docs/02 puts
format surprises in front of a human rather than through a default, and this
source's headline finding is precisely that its own documentation is wrong about
what a number means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from kelpcompare.features.quarters import QUARTERS
from kelpcompare.polygons import Polygon

#: The docs/03 source vocabulary name for these rows.
SOURCE = "kelpwatch"

FETCHER_NAME = "kelpwatch"

#: The export's header, in order. Read from the file and checked, never assumed:
#: a column inserted upstream would silently shift every value one place.
EXPORT_COLUMNS = (
    "year",
    "quarter",
    "kelp_area_m2",
    "count_cells_kelp",
    "count_cells_no_clouds",
    "count_cells_historic_footprint",
)

#: Source column -> the name it carries in this project. `kelp_area_m2` is
#: already SI and already says what it is. The counts are renamed to the
#: `n_obs`/`expected_obs` shape `quarterly_env` uses, so coverage reads the same
#: way on both halves of the comparison.
_RENAMED = {
    "kelp_area_m2": "kelp_area_m2",
    "count_cells_kelp": "n_cells_kelp",
    "count_cells_no_clouds": "n_cells_observed",
    "count_cells_historic_footprint": "n_cells",
}

#: The tidy frame this module produces, in column order. Not the `quarterly_kelp`
#: table: the coverage fraction, the usability verdict and the provenance columns
#: belong to the feature stage, which is the only thing that knows the coverage
#: floor and when the run happened.
PARSED_COLUMNS = (
    "polygon_id",
    "year",
    "quarter",
    "kelp_area_m2",
    "n_cells_kelp",
    "n_cells_observed",
    "n_cells",
)

_DTYPES = {
    "polygon_id": "string",
    "year": "int32",
    "quarter": "int8",
    "kelp_area_m2": "float64",
    "n_cells_kelp": "float64",
    "n_cells_observed": "int32",
    "n_cells": "int32",
}

#: The token the derived per-year row carries in the `quarter` column.
MAX_ROW_TOKEN = "max"

#: A Landsat pixel is 30 m square. The area column is fractional-cover weighted,
#: so it is bounded above by this per kelp-bearing cell rather than equal to it.
_CELL_AREA_M2 = 900.0

_QUARTER_TOKENS = frozenset(str(q) for q in QUARTERS)


@dataclass(frozen=True)
class ParsedExport:
    """One export's rows plus what the run manifest should hear about them."""

    frame: pd.DataFrame
    polygon_id: str
    rows_in: int
    max_rows_dropped: int = 0
    quarters_unobserved: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def first_quarter(self) -> str | None:
        return _label(self.frame.iloc[0]) if len(self.frame) else None

    @property
    def last_quarter(self) -> str | None:
        return _label(self.frame.iloc[-1]) if len(self.frame) else None


def sniff(path: Path) -> bool:
    """Whether this file looks like a Kelp Watch export.

    The header line and nothing else. There is no vendor magic number, no
    signature block and no filename convention worth trusting -- the operator
    names the file, and the registry is what maps a name to a polygon.
    """
    try:
        with path.open(encoding="utf-8-sig") as handle:
            header = handle.readline()
    except OSError:
        return False
    return tuple(field.strip() for field in header.strip().split(",")) == EXPORT_COLUMNS


def parse(path: Path, polygon: Polygon) -> ParsedExport:
    """One export -> one tidy row per observed and unobserved quarter.

    Raises `ValueError` on anything this module has not verified: a header that
    is not the recorded one, a quarter token nobody has seen, a repeated
    quarter, a footprint that moves inside one file, or a cell count that cannot
    be what it claims. Every one is a case where the honest answer is that we do
    not know what the numbers mean.

    Rows are returned for unobserved quarters too, carrying a null value. They
    are the record of a cloud gap, and dropping them would make a hole in the
    series indistinguishable from a quarter the export never covered.
    """
    table = _read(path)
    warnings: list[str] = []
    rows_in = len(table)

    real, dropped = _drop_derived_rows(table, warnings=warnings, path=path)
    if real.empty:
        raise ValueError(f"{path}: no quarterly rows -- every row was a derived `max` row")

    frame = _typed(real, polygon)
    _check_counts(frame, path=path)
    unobserved = _apply_missing_rule(frame)

    if unobserved:
        warnings.append(
            f"{polygon.polygon_id}: {unobserved} quarter(s) had no cloud-free observation and "
            f"are stored as null, not zero -- the export writes 0 for both (docs/02)"
        )

    return ParsedExport(
        frame=frame.reindex(columns=list(PARSED_COLUMNS)).astype(_DTYPES),
        polygon_id=polygon.polygon_id,
        rows_in=rows_in,
        max_rows_dropped=dropped,
        quarters_unobserved=unobserved,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _read(path: Path) -> pd.DataFrame:
    """Every cell as text, with pandas' own NA tokens left alone.

    `keep_default_na=False` is load-bearing rather than tidiness. pandas would
    otherwise convert a list of tokens of its own -- `NA`, `null`, `NaN`, `N/A`
    -- to NaN before this module sees them, which would make a token nobody has
    verified indistinguishable from a blank the source actually wrote. The same
    reasoning the NDBC parser applies, for the same reason.
    """
    table = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    columns = tuple(table.columns)
    if columns != EXPORT_COLUMNS:
        raise ValueError(
            f"{path}: expected the Kelp Watch export columns {list(EXPORT_COLUMNS)}, got "
            f"{list(columns)}. This is a layout docs/02 has not recorded -- do not store it "
            "until the new column has been checked."
        )
    return table


def _drop_derived_rows(
    table: pd.DataFrame, *, warnings: list[str], path: Path
) -> tuple[pd.DataFrame, int]:
    """Keep the four real quarters; drop the per-year `max` row and say so.

    Reported rather than silent. A row silently discarded and a row the export
    never wrote are indistinguishable afterwards, and this one is discarded on a
    judgement -- that it is derived -- which a reader should be able to audit.

    A token that is neither a quarter nor `max` stops the parse. The export has
    exactly two kinds of row today; a third is a format change, and guessing
    whether it is a quarter would put an unknown aggregation into the response
    variable.
    """
    tokens = table["quarter"].str.strip()
    unknown = sorted(set(tokens) - _QUARTER_TOKENS - {MAX_ROW_TOKEN})
    if unknown:
        raise ValueError(
            f"{path}: unrecognised quarter token(s) {unknown}; this parser knows "
            f"{sorted(_QUARTER_TOKENS)} and the derived {MAX_ROW_TOKEN!r} row. A new kind of "
            "row is a format change docs/02 has not recorded."
        )

    derived = int((tokens == MAX_ROW_TOKEN).sum())
    if derived:
        warnings.append(
            f"dropped {derived} derived `max` row(s): a column-wise growing-season maximum, "
            "not a quarter and not the peak quarter's row (docs/02)"
        )
    return table.loc[tokens.isin(_QUARTER_TOKENS)].copy(), derived


def _typed(real: pd.DataFrame, polygon: Polygon) -> pd.DataFrame:
    frame = real.rename(columns=_RENAMED)
    frame["polygon_id"] = polygon.polygon_id
    for column in ("year", "quarter", *_RENAMED.values()):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.sort_values(["year", "quarter"], kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------
# What the numbers have to be
# --------------------------------------------------------------------------


def _check_counts(frame: pd.DataFrame, *, path: Path) -> None:
    """Refuse a file whose columns cannot mean what this module reads them as.

    Each check is one of the readings the missing rule depends on, and getting
    any of them wrong changes values rather than raising later.

    The footprint is the denominator of every coverage fraction, so a footprint
    that moves inside one file means the rows are not one geometry's record. The
    ordering checks are the evidence that `count_cells_no_clouds` counts the
    whole footprint rather than the "unoccupied kelp habitat" the published
    field dictionary calls it -- if the dictionary were right, the cloud-free
    count would sometimes fall below the kelp-bearing one.
    """
    for column in ("year", "quarter", "n_cells_observed", "n_cells"):
        unreadable = frame.loc[frame[column].isna()]
        if len(unreadable):
            raise ValueError(
                f"{path}: {len(unreadable)} row(s) carry a {column!r} that is not a number. "
                "The published field dictionary says a cell can be blank; these four columns "
                "are what decide whether a quarter was observed at all, so a blank in one of "
                "them is a format change to check rather than a value to read around."
            )

    duplicated = frame.loc[frame.duplicated(subset=["year", "quarter"]), ["year", "quarter"]]
    if len(duplicated):
        labels = [f"{int(r.year)}Q{int(r.quarter)}" for r in duplicated.itertuples()]
        raise ValueError(f"{path}: quarter(s) {labels} appear more than once")

    footprints = set(frame["n_cells"].dropna().astype("int64"))
    if len(footprints) > 1:
        raise ValueError(
            f"{path}: count_cells_historic_footprint takes {len(footprints)} different values "
            f"{sorted(footprints)[:5]}; it is the denominator of every coverage fraction here, "
            "so a file where it moves is not one geometry's record"
        )

    _refuse_rows(
        frame,
        frame["n_cells_observed"] > frame["n_cells"],
        path=path,
        why="report more cloud-free cells than the historic footprint holds",
    )
    _refuse_rows(
        frame,
        frame["n_cells_kelp"] > frame["n_cells_observed"],
        path=path,
        why="report more kelp-bearing cells than were observed at all -- "
        "count_cells_no_clouds is not the count this parser reads it as",
    )
    _refuse_rows(
        frame,
        frame["kelp_area_m2"] > frame["n_cells_kelp"] * _CELL_AREA_M2,
        path=path,
        why=f"report more canopy area than {_CELL_AREA_M2:g} m2 per kelp-bearing cell allows",
    )


def _refuse_rows(frame: pd.DataFrame, bad: pd.Series, *, path: Path, why: str) -> None:
    offending = frame.loc[bad.fillna(False)]
    if len(offending):
        labels = [_label(row) for row in offending.head(5).itertuples()]
        raise ValueError(
            f"{path}: {len(offending)} row(s) {why}; first: {labels}. Do not store the file "
            "until the format has been checked against docs/02."
        )


def _apply_missing_rule(frame: pd.DataFrame) -> int:
    """Null the value of every quarter with no cloud-free observation.

    The one rule this whole module exists for (hard rule 3). `n_cells_kelp` goes
    with it: it is zero for the same fabricated reason the area is, and leaving
    it at zero would let a downstream reader reconstruct the very measurement
    this removes.

    The counts themselves stay as they are. `n_cells_observed = 0` is a fact
    about the quarter and is what makes the null auditable rather than mysterious.
    """
    blind = frame["n_cells_observed"] == 0
    frame.loc[blind, ["kelp_area_m2", "n_cells_kelp"]] = pd.NA
    return int(blind.sum())


def _label(row) -> str:
    return f"{int(row.year)}Q{int(row.quarter)}"


__all__ = [
    "EXPORT_COLUMNS",
    "FETCHER_NAME",
    "MAX_ROW_TOKEN",
    "PARSED_COLUMNS",
    "SOURCE",
    "ParsedExport",
    "parse",
    "sniff",
]
