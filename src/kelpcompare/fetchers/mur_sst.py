"""JPL MUR L4 SST over an analysis polygon -> docs/03 observation rows (docs/02).

The only module that knows what a NOAA CoastWatch ERDDAP `griddap` CSV looks
like (docs/01 layer 1), and the only one whose source is a *grid* rather than a
station. It supplies the satellite leg of the docs/04 s4.5 three-way comparison
-- project sensor against public neighbour against satellite SST, for the same
kelp series -- which was the one leg of that comparison the system had never
built (https://github.com/cweber12/kelp-compare/issues/106).

**A cell is not a station, so this fetcher reduces before it stores.** One row
per bed per day, the area-weighted mean of the cells the bed's outline covers.
The alternative -- landing every cell as its own series and reducing in the
feature builder -- was rejected because each cell would still need a `site_id`,
so it does not avoid the question of what a satellite row is keyed on, it
multiplies it by the number of cells. Doing it here is what docs/01 layer 1 is
for: a grid becoming a comparable series is exactly the source-specific
knowledge a fetcher exists to hold. The registry says which polygon each
derived site reduces (docs/03 "A site may be derived from a polygon"); nothing
here reads a polygon out of a site's name.

**The reduction is area-weighted, and that is a measured choice, not a
refinement.** The obvious rule -- average the cells whose *centre* falls inside
the bed -- produces no series at all for `KELP:DEL-MAR`, whose outline is
narrower than the 1 km grid along its whole length: zero cell centres fall
inside it. Measured on 2020-07-01, centres-inside against cells-intersecting
runs 9/22, 0/5, 2/5, 2/7, 15/34 and 6/17 across the six beds. Weighting each
intersecting cell by the area of it that lies inside the outline is the
ordinary zonal mean, it needs no fallback rule for the small beds, and where
both rules produce an answer they agree to within 0.03 degC -- so the choice
decides whether a bed has a series at all, and barely moves the beds that
already did.

**Land is `NaN`, and a bbox is not a bed.** The request has to be a rectangle,
and every bed's rectangle contains shoreline. A mean over the bbox would
average the land mask's holes into the water; the polygon mask is what stops
it. Cells carrying no value drop out of the weighted mean, and the fraction of
the bed still backed by a value is reported per payload rather than assumed --
it runs 0.83 to 1.00 across the six beds, so it is never negligible and never
close to failing.

**Conditional requests do not work here, and are not pretended.** Measured
2026-08-31 against this host: no `ETag` is served at all, the `Last-Modified`
is the moment the response was generated rather than a version of the data --
two requests one minute apart returned two different values -- and
`If-Modified-Since` is answered `200` with the whole body. Storing that header
would put "when we asked" into the validator cache wearing the costume of "what
version this is", so this fetcher records neither validator. Re-runs are made
cheap by asking for a narrower window instead, as the RTOMS module does for the
same reason on a different ERDDAP.

**`now` is not available on a griddap time axis.** The tabledap form the RTOMS
module uses, `time>=now-45days`, is rejected here with `Start=NaN` (measured,
same session). The rolling window is therefore expressed as an index offset
from the end of the record, `[last-44:1:last]`, which is 45 daily steps, cannot
run off the start of the record, and gives a stable URL.
"""

from __future__ import annotations

import io
import time

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

from kelpcompare import __version__
from kelpcompare.fetchers.base import (
    ParsedPayload,
    Payload,
    SourceUnavailable,
    new_payload,
)
from kelpcompare.normalize import convert_unit
from kelpcompare.parameters import Parameters
from kelpcompare.polygons import WGS84
from kelpcompare.storage import (
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    OBSERVATION_COLUMNS,
    empty_observations,
)

#: The docs/03 source vocabulary name for this fetcher's rows.
SOURCE = "mur_sst"

FETCHER_NAME = "mur_sst"

#: The ERDDAP dataset, pinned. Every derived site's `station_code` repeats it,
#: which is what makes the identifier a registry fact rather than a constant
#: only this module knows.
DATASET_ID = "jplMURSST41"

#: NOAA CoastWatch West Coast Node rather than PO.DAAC's own ERDDAP: it serves
#: this dataset as `griddap` with server-side subsetting, so a bed-sized request
#: is a bed-sized download instead of a global daily file (docs/02).
BASE_URL = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/{dataset_id}.csv"

#: The one variable read. MUR also serves `analysis_error`, `mask` and
#: `sea_ice_fraction`; none has a `parameters.json` entry, and adding one is a
#: registry decision about SI units and QC bounds rather than a parsing
#: convenience (docs/02).
VARIABLE = "analysed_sst"

#: The header line this module verified, in order. A griddap CSV names its axes
#: before its variable, so a reordering would silently swap latitude for
#: longitude -- which puts every bed in the wrong ocean and raises nothing.
COLUMNS = ("time", "latitude", "longitude", VARIABLE)

#: The controlled parameter these rows carry.
PARAMETER = "sea_water_temperature"

#: What the file's own units line has to declare. Checked rather than assumed,
#: for the reason the NDBC module gives at greater length: a temperature in the
#: wrong unit stored as degC survives into a publication. MUR serves degC where
#: the underlying L4 product is Kelvin, so this is a real conversion someone
#: upstream is doing on our behalf and it is worth confirming they still are.
EXPECTED_UNIT = "degree_C"

#: The grid step, from the dataset's own `geospatial_lat_resolution`. Checked
#: against the payload rather than trusted: it sizes every cell footprint, so a
#: regridded product would silently reweight the whole record.
CELL_DEGREES = 0.01

#: `analysed_sst:_FillValue`. ERDDAP writes `NaN` in CSV, so this should never
#: appear -- it is mapped to missing anyway, because a -7.768 degC reading that
#: reached the weighted mean would drag a bed's daily value by the fill's whole
#: distance from the water, and QC runs after the reduction rather than before.
FILL_VALUE = -7.768

#: Where the cell footprints are measured. A geographic CRS is refused rather
#: than silenced: geopandas is right that degrees-squared is not an area. The
#: choice between projections is immaterial here -- the weights are relative
#: within one bed spanning at most 12 km -- so this is the metric CRS the rest
#: of the project already measures distance in (`tests/test_polygons.py`).
WEIGHT_CRS = "EPSG:32611"

#: The rolling window, in daily steps back from the end of the record. Matches
#: the NDBC realtime span so that "realtime" means the same thing across
#: sources.
REALTIME_DAYS = 45

#: The first year the record covers, from `time_coverage_start`
#: (2002-06-01T09:00:00Z). A request for an earlier year is an operator error
#: rather than an outage, and is refused by name.
RECORD_START_YEAR = 2002

#: Seconds to wait before the single retry docs/02 asks for.
RETRY_DELAY_SECONDS = 2.0

#: Statuses worth asking a second time. ERDDAP answers a query that matched
#: nothing with 404 and a text body saying so, which is an answer rather than an
#: outage and must not be retried.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: How this project identifies itself. A default `python-requests` user agent
#: gives a public service nobody to contact and is what gets throttled.
USER_AGENT = f"kelpcompare/{__version__}"


# --------------------------------------------------------------------------
# Where to ask
# --------------------------------------------------------------------------


def request_bounds(geometry) -> tuple[float, float, float, float]:
    """The bbox to request for one bed: its bounds, padded by one whole cell.

    Padded because the reduction weights every cell the outline *touches*, and a
    cell whose centre sits outside the bounds can still overlap them. Asking for
    exactly the bounds would drop those cells from the payload, so the same
    outline would reduce differently depending on where its edges fell against
    the grid -- silently, and differently for each bed.

    One cell is enough by construction: a cell extends half a step either side of
    its centre, so a cell overlapping the bounds has its centre within half a
    step of them. A whole step is used rather than half so that the landed bytes
    still contain every cell needed if an outline is later nudged outward by less
    than a cell; a redraw larger than that needs a re-fetch, not a re-parse.
    """
    minx, miny, maxx, maxy = geometry.bounds
    return (
        minx - CELL_DEGREES,
        miny - CELL_DEGREES,
        maxx + CELL_DEGREES,
        maxy + CELL_DEGREES,
    )


def _url(bounds: tuple[float, float, float, float], time_constraint: str) -> str:
    """One griddap CSV query: a time constraint and a lat/lon box.

    The axis order is time, latitude, longitude, and it is positional in the
    URL rather than named -- which is why `COLUMNS` is checked on the way back
    in. Bounds are formatted to four decimals, two more than the grid step, so
    rounding cannot move a request edge onto a cell boundary.
    """
    minx, miny, maxx, maxy = bounds
    return (
        f"{BASE_URL.format(dataset_id=DATASET_ID)}?{VARIABLE}"
        f"{time_constraint}"
        f"%5B({miny:.4f}):1:({maxy:.4f})%5D"
        f"%5B({minx:.4f}):1:({maxx:.4f})%5D"
    )


def realtime_url(bounds: tuple[float, float, float, float]) -> str:
    """The most recent `REALTIME_DAYS` daily steps for one bed.

    Expressed as an index offset from the end of the record because `now` is not
    available on a griddap time axis (see the module docstring). That is the
    better form here anyway: it cannot ask for a date the analysis has not
    published yet, and it cannot run off the start of the record.

    Public so the caller can look up what it already knows about this URL before
    asking for it -- the same reason every other fetcher exposes its URLs. A
    fetcher that read the validator cache itself would be writing outside its own
    raw zone, which docs/02 forbids.
    """
    return _url(bounds, f"%5Blast-{REALTIME_DAYS - 1}:1:last%5D")


def archive_url(bounds: tuple[float, float, float, float], year: int) -> str:
    """One calendar year for one bed.

    Half-open on the right so two consecutive years cannot both claim the same
    day. They would dedupe on `OBSERVATION_KEY` anyway, but a window overlapping
    its neighbour by one step makes every row count in the manifest off by one
    and unexplainable.

    The upper bound is a date rather than `last`, so a request for the current
    year does not silently mean something different tomorrow. A year the record
    does not reach yet simply returns the part of it that exists.
    """
    return _url(bounds, f"%5B({year}-01-01T00:00:00Z):1:({year}-12-31T23:59:59Z)%5D")


# --------------------------------------------------------------------------
# Asking
# --------------------------------------------------------------------------


def fetch_realtime(
    bounds: tuple[float, float, float, float],
    *,
    station: str,
    session=None,
    validators: dict[str, str] | None = None,
) -> Payload:
    """The rolling recent window for one bed.

    `station` is the landing directory -- the bed's `polygon_id` with its colon
    replaced, as the Kelp Watch landings do it -- so a payload says which water
    it is of. `validators` is accepted and ignored, for the reason the module
    docstring gives; this fetcher never raises `NotModified`.
    """
    url = realtime_url(bounds)
    body = _get(url, session)
    return new_payload(SOURCE, station, f"{station}_realtime.csv", url, body)


def fetch_archive(
    bounds: tuple[float, float, float, float],
    year: int,
    *,
    station: str,
    session=None,
    validators: dict[str, str] | None = None,
) -> Payload:
    """One calendar year for one bed.

    A year before the record starts is refused by name rather than fetched: MUR
    begins 2002-06, and ERDDAP answers an out-of-range time constraint with a
    400 that `_get` would report as an outage -- which would put a phantom gap in
    the manifest for a year that was never going to exist.
    """
    if year < RECORD_START_YEAR:
        raise ValueError(
            f"{DATASET_ID} begins {RECORD_START_YEAR}-06 and cannot serve {year}; "
            "the request is out of the record rather than a gap in it"
        )
    url = archive_url(bounds, year)
    body = _get(url, session)
    return new_payload(SOURCE, station, f"{station}_{year}.csv", url, body)


def _get(url: str, session) -> bytes:
    """Retrieve one URL. Returns bytes alone, and that is the point.

    Every other fetcher returns the server's validators beside the body. This one
    deliberately does not: measured 2026-08-31, this host serves no `ETag` and a
    `Last-Modified` that is the response's own generation time, so there is
    nothing here that describes a version of the data. Returning it would have
    the ingest CLI record it as though there were.

    Imported lazily so the parser -- the half the tests exercise -- does not need
    `requests` on the import path at all.
    """
    if session is None:
        import requests

        session = requests.Session()

    headers = {"User-Agent": USER_AGENT}
    last = ""
    for attempt in range(2):
        if attempt:
            time.sleep(RETRY_DELAY_SECONDS)
        try:
            response = session.get(url, timeout=300, headers=headers)
        except Exception as error:  # noqa: BLE001 -- one outage, however it arrived
            last = f"{type(error).__name__}: {error}"
            continue

        status = getattr(response, "status_code", None)
        if status == 200:
            return response.content

        last = f"HTTP {status}"
        if status not in RETRYABLE_STATUS:
            break

    raise SourceUnavailable(f"{url}: {last}")


# --------------------------------------------------------------------------
# Reducing a grid to a bed
# --------------------------------------------------------------------------


def parse(
    payload: Payload,
    parameters: Parameters,
    *,
    site_id: str,
    geometry,
    depths_m: dict[str, float] | None = None,
    measured_parameters: tuple[str, ...] = (),
    run_id: str,
) -> ParsedPayload:
    """One griddap CSV -> one observation row per day, UTC and SI.

    Raises `ValueError` on a layout, a unit or a grid step this module has not
    verified, for the reason docs/02 gives: the honest answer is that we do not
    know what the numbers mean, and that belongs in front of a human rather than
    behind a default.

    `geometry` is the outline the payload is reduced over, from
    `polygons.geojson` via the site's `derived_from` block. It is required: this
    module has no fallback that would let a payload land unreduced, because a
    reduction the registry did not describe is a number nobody can reproduce.

    `depths_m` is the registry's `sensor_depths_m`, and for these sites it
    declares nothing -- so `depth_m` is null on every row. That is the docs/03
    shape for a water parameter whose depth the provider has not published, and
    PO.DAAC publishes none: `analysed_sst` is a
    `sea_surface_foundation_temperature`, which is by definition free of diurnal
    stratification rather than taken at a depth. Read from the registry rather
    than hardcoded, so declaring one later is a registry edit -- though a
    one-way one, `depth_m` being part of `OBSERVATION_KEY`.
    """
    warnings: list[str] = []
    if measured_parameters and PARAMETER not in measured_parameters:
        return _nothing(
            payload,
            rows_in=0,
            warnings=(
                (
                    f"{payload.station}: the registry declares measured_parameters "
                    f"{sorted(measured_parameters)}, which does not include {PARAMETER!r}; "
                    "this dataset carries nothing else this project stores"
                ),
            ),
        )
    if PARAMETER not in parameters:
        raise ValueError(
            f"{payload.station}: {PARAMETER!r} is not in {parameters.path}; "
            "the controlled parameter has to exist before its rows can land"
        )
    if geometry is None or geometry.is_empty:
        raise ValueError(
            f"{payload.station}: this bed has no recorded outline, so there is nothing to "
            "reduce the grid over; record the geometry in polygons.geojson first"
        )

    names, units, table = _read(payload)
    _check_unit(dict(zip(names, units, strict=False)), payload)

    values = _values(table, payload, warnings)
    weights, overlapping = _weights(table, geometry, payload)

    daily = _reduce(table, values, weights, payload)
    if not len(daily):
        return _nothing(payload, rows_in=len(table), warnings=tuple(warnings))

    covered = daily["covered"]
    warnings.append(
        f"{payload.station}: the daily value is the area-weighted mean over {overlapping} "
        f"cell(s) overlapping the bed, covering {covered.min():.1%} to {covered.max():.1%} "
        "of its area on the days in this payload"
    )
    blank = int((covered <= 0).sum())
    if blank:
        warnings.append(
            f"{payload.station}: {blank} day(s) carried no value in any cell over the bed "
            "and are stored as missing, not dropped"
        )

    parameter = parameters[PARAMETER]
    absent = daily["value"].isna().to_numpy()
    frame = pd.DataFrame(
        {
            "timestamp": daily["timestamp"].to_numpy(),
            "site_id": site_id,
            "parameter": parameter.name,
            "value": convert_unit(daily["value"], EXPECTED_UNIT, parameter.unit).to_numpy(
                dtype="float64"
            ),
            "depth_m": (depths_m or {}).get(PARAMETER, float("nan")),
            "qc_flag": np.where(absent, FLAG_MISSING, FLAG_NOT_EVALUATED).astype("int8"),
            "qc_tests": "",
            "source": SOURCE,
            "fetch_run_id": run_id,
        }
    )
    frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)

    return ParsedPayload(
        frame=frame[list(OBSERVATION_COLUMNS)],
        station=payload.station,
        layout=DATASET_ID,
        rows_in=len(table),
        warnings=tuple(warnings),
        missing_counts={PARAMETER: int(absent.sum())},
    )


def _nothing(payload: Payload, *, rows_in: int, warnings: tuple[str, ...]) -> ParsedPayload:
    """A well-formed payload that yielded no rows for this project."""
    return ParsedPayload(
        frame=empty_observations(),
        station=payload.station,
        layout=DATASET_ID,
        rows_in=rows_in,
        warnings=warnings,
    )


def _read(payload: Payload) -> tuple[list[str], list[str], pd.DataFrame]:
    """Split the two-line ERDDAP header from the body, and refuse a surprise.

    `griddap` CSV opens with a names line and then a *units* line, exactly as
    `tabledap` does, which is why a unit can be checked at all rather than
    assumed from a variable name.

    `keep_default_na=False` is load-bearing rather than tidiness: pandas would
    otherwise convert a list of tokens of its own to NaN before this module sees
    them, making a token nobody verified indistinguishable from the `NaN` ERDDAP
    actually writes for land.
    """
    text = payload.body.decode("utf-8-sig", errors="strict")
    lines = text.splitlines()
    if len(lines) < 2:
        raise ValueError(
            f"{payload.station}: this payload has {len(lines)} line(s); a griddap CSV opens "
            "with a names line and a units line"
        )

    names = [name.strip() for name in lines[0].split(",")]
    units = [unit.strip() for unit in lines[1].split(",")]
    if tuple(names) != COLUMNS:
        raise ValueError(
            f"{payload.station}: this payload's columns are {names}, not {list(COLUMNS)}; "
            "the axis order is positional in the request URL, so a reordering here means "
            "latitude and longitude may have swapped"
        )

    table = pd.read_csv(
        io.StringIO("\n".join([lines[0], *lines[2:]])),
        keep_default_na=False,
        dtype=str,
    )
    return names, units, table


def _check_unit(declared: dict[str, str], payload: Payload) -> None:
    """Refuse a value column whose declared unit is not the one verified."""
    unit = declared.get(VARIABLE, "")
    if unit != EXPECTED_UNIT:
        raise ValueError(
            f"{payload.station}: {VARIABLE} declares unit {unit!r}, not {EXPECTED_UNIT!r}; "
            "this dataset is served in degC and a temperature in another unit stored as degC "
            "survives into a publication"
        )


def _values(table: pd.DataFrame, payload: Payload, warnings: list[str]) -> pd.Series:
    """The value column as floats, with land and the declared fill as missing."""
    values = pd.to_numeric(table[VARIABLE], errors="coerce")
    filled = values.eq(FILL_VALUE)
    if filled.any():
        warnings.append(
            f"{payload.station}: {int(filled.sum())} cell-day(s) carried the declared "
            f"_FillValue {FILL_VALUE}, read as missing rather than as a reading"
        )
        values = values.mask(filled)
    return values


def _weights(table: pd.DataFrame, geometry, payload: Payload) -> tuple[np.ndarray, int]:
    """Each row's share of the bed, and how much of the bed the grid covers.

    Computed over the payload's *distinct* cells and then broadcast back, not per
    row: a year of a six-cell bed is 2,190 rows over six footprints, and
    intersecting each row's box against the outline would do the same six
    intersections 365 times.

    Returns weights aligned to `table`, zero for a cell the outline does not
    touch, and how many distinct cells that is -- a count of the grid's reach
    into this bed, which is fixed for a bed and does not depend on how many days
    the payload holds. How much of the bed a given *day* is actually backed by is
    a different number and belongs with the values (`_reduce`).
    """
    latitudes = pd.to_numeric(table["latitude"], errors="coerce")
    longitudes = pd.to_numeric(table["longitude"], errors="coerce")
    if latitudes.isna().any() or longitudes.isna().any():
        raise ValueError(
            f"{payload.station}: this payload carries a row with no position; the axes are "
            "the grid itself and cannot be absent"
        )

    cells = pd.DataFrame({"latitude": latitudes, "longitude": longitudes}).drop_duplicates()
    _check_spacing(cells, payload)

    half = CELL_DEGREES / 2
    footprints = gpd.GeoSeries(
        [
            box(lon - half, lat - half, lon + half, lat + half)
            for lat, lon in zip(cells["latitude"], cells["longitude"], strict=True)
        ],
        crs=WGS84,
    ).to_crs(WEIGHT_CRS)
    outline = gpd.GeoSeries([geometry], crs=WGS84).to_crs(WEIGHT_CRS).iloc[0]

    cells["_weight"] = footprints.intersection(outline).area.to_numpy()
    if not (cells["_weight"] > 0).any():
        raise ValueError(
            f"{payload.station}: no cell in this payload overlaps the bed's outline; the "
            "request box and the geometry disagree, which is a bug rather than a data gap"
        )

    positions = pd.DataFrame({"latitude": latitudes, "longitude": longitudes})
    merged = positions.merge(cells, on=["latitude", "longitude"], how="left")
    return merged["_weight"].to_numpy(), int((cells["_weight"] > 0).sum())


def _check_spacing(cells: pd.DataFrame, payload: Payload) -> None:
    """Refuse a grid step that is not the one the footprints are sized for.

    A regridded product would keep every column name and every unit and change
    only this, and each cell would then be weighted by a footprint of the wrong
    size -- which reweights the whole record silently, in a direction that
    depends on where the bed sits against the new grid.

    A payload with one distinct latitude or longitude has no step to measure and
    passes: that is a bed narrower than a cell in one direction, which is the
    case this reduction exists to serve.
    """
    for axis in ("latitude", "longitude"):
        unique = np.sort(cells[axis].unique())
        if len(unique) < 2:
            continue
        step = float(np.median(np.diff(unique)))
        if abs(step - CELL_DEGREES) > CELL_DEGREES / 100:
            raise ValueError(
                f"{payload.station}: the payload's {axis} step is {step:.5f} degrees, not the "
                f"{CELL_DEGREES} this module sizes cell footprints from; the product has been "
                "regridded and every weight in the record would be wrong"
            )


def _reduce(
    table: pd.DataFrame, values: pd.Series, weights: np.ndarray, payload: Payload
) -> pd.DataFrame:
    """One area-weighted mean per timestamp, and how much of the bed backed it.

    `covered` is the share of the bed's overlapping area that carried a value
    that day -- so it answers "a mean over how much of this bed", which the mean
    itself cannot. It is not a constant: the land mask takes a fixed bite out of
    most beds (0.83 to 1.00 across the six), and a day the product does not cover
    takes more.

    A day where every overlapping cell is absent stays in the record as a row
    with no value, which `parse` flags missing. Dropping it would turn an outage
    in a daily product into a gap the coverage arithmetic in docs/04 s3 cannot
    see, and docs/03 keeps outages rather than deleting them.
    """
    stamps = pd.to_datetime(table["time"], utc=True, format="ISO8601", errors="coerce")
    if stamps.isna().any():
        raise ValueError(
            f"{payload.station}: this payload carries a row whose time is not an ISO 8601 "
            "timestamp; the axis is the record's own calendar and cannot be absent"
        )

    overlaps = weights > 0
    working = pd.DataFrame(
        {
            "timestamp": stamps[overlaps].to_numpy(),
            "value": values[overlaps].to_numpy(),
            "weight": weights[overlaps],
        }
    )
    usable = working["value"].notna()

    # Three sums per day rather than one mean: the weighted numerator, the
    # weight that actually carried a value, and the weight the bed overlaps at
    # all. The first two give the mean; the last two give `covered`, and it is
    # summed per day rather than taken once for the bed because a day the
    # product does not cover has to be able to differ from the land mask's
    # standing bite.
    working["_num"] = (working["value"].fillna(0.0) * working["weight"]).where(usable, 0.0)
    working["_valued"] = working["weight"].where(usable, 0.0)
    grouped = working.groupby("timestamp", sort=True, as_index=False)[
        ["_num", "_valued", "weight"]
    ].sum()

    grouped["value"] = (grouped["_num"] / grouped["_valued"]).where(grouped["_valued"] > 0)
    grouped["covered"] = grouped["_valued"] / grouped["weight"]
    return grouped[["timestamp", "value", "covered"]]


__all__ = [
    "DATASET_ID",
    "FETCHER_NAME",
    "PARAMETER",
    "SOURCE",
    "archive_url",
    "fetch_archive",
    "fetch_realtime",
    "parse",
    "realtime_url",
    "request_bounds",
]
