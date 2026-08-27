"""NDBC standard meteorological files -> docs/03 observation rows (docs/02).

The only module that knows what an NDBC file looks like (docs/01 layer 1). Two
layouts, parsed separately because they differ in three ways at once:

|                | realtime                     | stdmet archive            |
|----------------|------------------------------|---------------------------|
| coverage       | ~45 days, rolling            | one calendar year         |
| row order      | **newest first**             | oldest first              |
| missing token  | `MM`                         | the column's all-nines fill |
| extra column   | `PTDY`                       | --                        |
| `VIS` unit     | `nmi`                        | `mi`                      |

That last row is the reason units are read from the file's own units line and
checked, never assumed from the column name: the same station reports the same
quantity under two different unit tokens depending on which file you asked for.
A column whose declared unit is not the one this module expects stops the parse
rather than entering the record -- a wind speed in knots stored as m/s is the
kind of error that survives into a publication.

**Sentinels are numeric and per-column.** NDBC fills a missing value with nines
to the column's own width and precision, so water temperature reads `999.0`,
wave height `99.00`, and wind direction `999`. Read naively, a missing water
temperature becomes a 999 °C measurement -- inside no valid range, so QC would
catch it, but it would be counted as an observation all the way there. They
become null here, at the boundary, which is what docs/02 requires.

**No deployment record is involved.** Public stations do not go through
`normalize.to_observations`: there is no vendor series name to map, no local
timezone to resolve (NDBC timestamps are UTC), and no in-water window to judge,
so the deployment machinery would have nothing to say. Rows are built into the
docs/03 schema directly, and the one thing that stage really owns -- the unit
boundary -- is still `normalize.convert_unit`, so a surprising unit refuses here
exactly as it would for a HOBO file.
"""

from __future__ import annotations

import gzip
import io
import time
from dataclasses import dataclass, field

import pandas as pd

from kelpcompare.fetchers.base import Payload, SourceUnavailable, new_payload
from kelpcompare.normalize import convert_unit
from kelpcompare.parameters import Parameters
from kelpcompare.storage import FLAG_MISSING, FLAG_NOT_EVALUATED, OBSERVATION_COLUMNS

#: The docs/03 source vocabulary name for this fetcher's rows.
SOURCE = "ndbc"

FETCHER_NAME = "ndbc"

#: Roughly the last 45 days, refreshed continuously.
REALTIME_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station}.txt"

#: One calendar year per file, gzipped. `h` is the standard-meteorological set.
ARCHIVE_URL = "https://www.ndbc.noaa.gov/data/historical/stdmet/{station_lower}h{year}.txt.gz"

#: The five time columns both verified layouts open with. `MM` here is the month
#: -- unrelated to the `MM` missing token, which never appears in these columns.
TIME_COLUMNS = ("YY", "MM", "DD", "hh", "mm")

#: Seconds to wait before the single retry docs/02 asks for.
RETRY_DELAY_SECONDS = 2.0

#: Statuses worth asking a second time. A 404 is an answer -- the station
#: never published that year -- and 4xx generally will not change on a retry.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ColumnSpec:
    """One NDBC column this project stores, and what it must be to be stored.

    `unit` is the token the file's own units line has to declare. `missing` is
    the all-nines fill NDBC writes for that column, compared numerically so a
    change in printed precision (`99.0` for `99.00`) does not smuggle a sentinel
    through as a measurement.
    """

    column: str
    parameter: str
    unit: str
    missing: float


#: docs/02 "NDBC": every column mapped to a controlled parameter, and only those.
#: Deliberately absent: PRES (no `parameters.json` entry, and adding one is a
#: registry decision, not a parsing one) and TIDE, whose datum NDBC does not
#: declare -- `water_level` is MLLW by definition (docs/03), and mixing an
#: undeclared datum into it would be invisible afterwards. Water level comes
#: from CO-OPS, which states its datum on every request.
COLUMNS = (
    ColumnSpec("WTMP", "sea_water_temperature", "degC", 999.0),
    ColumnSpec("ATMP", "air_temperature", "degC", 999.0),
    ColumnSpec("WVHT", "wave_significant_height", "m", 99.0),
    ColumnSpec("DPD", "wave_peak_period", "sec", 99.0),
    ColumnSpec("WSPD", "wind_speed", "m/s", 99.0),
)

#: The columns above, for reporting what a file carried that we did not store.
MAPPED_COLUMNS = frozenset(spec.column for spec in COLUMNS)


@dataclass(frozen=True)
class ParsedPayload:
    """Observation rows plus what the run manifest should hear about them."""

    frame: pd.DataFrame
    station: str
    layout: str
    rows_in: int
    warnings: tuple[str, ...] = ()
    unmapped_columns: tuple[str, ...] = ()
    undeclared_parameters: tuple[str, ...] = ()
    missing_counts: dict[str, int] = field(default_factory=dict)

    @property
    def flag_counts(self) -> dict[str, int]:
        """The docs/03 flag histogram, shaped as `NormalizedBatch` reports it.

        Same shape on purpose: the manifest should not be able to tell whether a
        run's rows arrived through an adapter or a fetcher.
        """
        counts = self.frame["qc_flag"].value_counts().to_dict()
        return {str(flag): int(n) for flag, n in sorted(counts.items())}


def fetch_realtime(station: str, *, session=None) -> Payload:
    """The rolling realtime file for one station (~45 days)."""
    url = REALTIME_URL.format(station=station.upper())
    return new_payload(SOURCE, station.upper(), f"{station.upper()}.txt", url, _get(url, session))


def fetch_archive(station: str, year: int, *, session=None) -> Payload:
    """One calendar year of standard meteorological data, gzipped.

    A year the station did not report is an ordinary hole in a public record --
    stations are installed, retired, and go down for repair -- so a 404 is
    `SourceUnavailable`, recorded as a gap and stepped over (docs/01 §5).
    """
    label = f"{station.lower()}h{year}.txt.gz"
    url = ARCHIVE_URL.format(station_lower=station.lower(), year=year)
    return new_payload(SOURCE, station.upper(), label, url, _get(url, session))


def parse(
    payload: Payload,
    parameters: Parameters,
    *,
    site_id: str,
    depths_m: dict[str, float] | None = None,
    measured_parameters: tuple[str, ...] = (),
    run_id: str,
) -> ParsedPayload:
    """One NDBC payload -> docs/03 observation rows, UTC and SI.

    Raises `ValueError` on a layout or a unit this module has not verified. Both
    are cases where the honest answer is that we do not know what the numbers
    mean, and docs/02 puts format surprises in front of a human rather than
    through a default.

    `measured_parameters` is the registry's list of what this station carries an
    instrument for; only those are stored. The stdmet format has fixed columns,
    so a shore station with no wave sensor still has `WVHT` and `DPD` in every
    file, filled with the sentinel -- and storing those was landing millions of
    rows that say nothing (https://github.com/cweber12/kelp-compare/issues/21).
    Empty means the registry has not recorded it, in which case everything
    recognised is stored and the gap is reported: an unrecorded fact must not
    become missing data.

    Note what this does *not* do. A station that declares a sensor and reports
    the sentinel still gets its rows, flagged missing -- that is an outage, and
    it stays in the record. The registry is what tells the two apart, which is
    why this is a declaration rather than a per-payload judgement about whether
    a column looked empty.
    """
    text = _text(payload)
    names, units, body = _split_header(text, payload)
    layout = "realtime" if "PTDY" in names else "archive"

    # `keep_default_na=False` is load-bearing, not tidiness. pandas otherwise
    # converts a list of tokens of its own -- `N/A`, `NA`, `null`, `NaN`, `-1.#IND`
    # -- to NaN before this module sees them, which would make a token nobody
    # verified indistinguishable from a sentinel NDBC documents. Every token
    # arrives here exactly as the station wrote it, and `_values` decides.
    table = pd.read_csv(
        body, sep=r"\s+", names=names, dtype=str, keep_default_na=False, engine="python"
    )
    timestamps = _timestamps(table, payload)

    declared = dict(zip(names, units, strict=False))
    frames: list[pd.DataFrame] = []
    warnings = list(_declaration_warnings(measured_parameters, parameters, payload))
    undeclared: list[str] = []
    missing_counts: dict[str, int] = {}

    for spec in COLUMNS:
        # Asked first, so a station that has no such sensor does not also collect
        # a warning about the column being absent from a file it was never in.
        if measured_parameters and spec.parameter not in measured_parameters:
            undeclared.append(spec.parameter)
            continue
        if spec.column not in table.columns:
            warnings.append(f"{payload.station}: no {spec.column} column in this {layout} file")
            continue
        if spec.parameter not in parameters:
            warnings.append(
                f"{payload.station}: {spec.column} maps to {spec.parameter!r}, which is not in "
                f"{parameters.path}; {spec.column} left unread"
            )
            continue

        _check_unit(spec, declared, payload=payload, layout=layout)
        values, unreadable = _values(table[spec.column], spec)
        if unreadable:
            warnings.append(
                f"{payload.station}: {len(unreadable)} {spec.column} value(s) are neither a "
                f"number nor a documented sentinel and were read as missing: "
                f"{', '.join(sorted(set(unreadable))[:5])}"
            )

        parameter = parameters[spec.parameter]
        missing_counts[spec.parameter] = int(values.isna().sum())
        frames.append(
            _rows(
                timestamps,
                convert_unit(values, spec.unit, parameter.unit),
                site_id=site_id,
                parameter=parameter.name,
                depth_m=(depths_m or {}).get(spec.parameter),
                run_id=run_id,
            )
        )

    frame = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=list(OBSERVATION_COLUMNS))
    )
    if not frame.empty:
        frame = frame.sort_values(["timestamp", "parameter"], kind="stable").reset_index(drop=True)

    return ParsedPayload(
        frame=frame[list(OBSERVATION_COLUMNS)],
        station=payload.station,
        layout=layout,
        rows_in=len(table),
        warnings=tuple(warnings),
        unmapped_columns=tuple(
            n for n in names if n not in TIME_COLUMNS and n not in MAPPED_COLUMNS
        ),
        undeclared_parameters=tuple(undeclared),
        missing_counts=missing_counts,
    )


def _declaration_warnings(
    measured_parameters: tuple[str, ...], parameters: Parameters, payload: Payload
) -> tuple[str, ...]:
    """What is wrong with the station's declaration, before a row is built.

    Two gaps, and both are the registry's rather than the file's. An undeclared
    station is reported every run, because the rows it lands are the ones this
    field exists to stop. A declared parameter the vocabulary does not know is
    worse than it looks: it matches no column, so the typo silently subtracts a
    real series rather than adding a fictional one.
    """
    if not measured_parameters:
        undeclared = (
            f"{payload.station}: the site registry declares no measured_parameters, so every "
            f"recognised column is stored -- including any this station has no sensor for"
        )
        return (undeclared,)
    unknown = [name for name in measured_parameters if name not in parameters]
    return tuple(
        f"{payload.station}: the site registry declares measured_parameters {name!r}, which is "
        f"not in {parameters.path}; no column can match it"
        for name in unknown
    )


def _get(url: str, session) -> bytes:
    """Retrieve one URL, turning every upstream failure into `SourceUnavailable`.

    Retries once, after a pause, then gives up and lets the run record a gap --
    the "retry politely" of docs/02. Once rather than a backoff ladder: this is
    a hand-run pipeline against a free public service, and the difference between
    one retry and five is borne entirely by NOAA.

    Imported lazily so the parser -- the half that tests exercise -- does not
    need `requests` on the import path at all.
    """
    if session is None:
        import requests

        session = requests.Session()

    last: str = ""
    for attempt in range(2):
        if attempt:
            time.sleep(RETRY_DELAY_SECONDS)
        try:
            response = session.get(url, timeout=60)
        # Every transport failure -- DNS, timeout, refused, malformed TLS -- is
        # one thing here: the source did not answer. Recorded and retried once,
        # then re-raised as SourceUnavailable below; never swallowed.
        except Exception as error:  # noqa: BLE001 -- one outage, however it arrived
            last = f"{type(error).__name__}: {error}"
            continue

        status = getattr(response, "status_code", None)
        if status == 200:
            return response.content

        last = f"HTTP {status}"
        # A 404 is an answer, not an outage: the station never published this
        # year. Retrying cannot change it, and asking twice for a file that does
        # not exist is exactly the impoliteness the retry rule is about.
        if status not in RETRYABLE_STATUS:
            break

    raise SourceUnavailable(f"{url}: {last}")


def _text(payload: Payload) -> str:
    """Decode, transparently un-gzipping the archive files."""
    body = payload.body
    if body[:2] == b"\x1f\x8b":  # gzip magic
        try:
            body = gzip.decompress(body)
        except OSError as error:
            raise ValueError(
                f"{payload.url}: gzip payload would not decompress: {error}"
            ) from error
    return body.decode("latin-1")


def _split_header(text: str, payload: Payload) -> tuple[list[str], list[str], io.StringIO]:
    """The names line, the units line, and the rows after them.

    Both verified layouts open with two `#`-prefixed lines. A file that does not
    is a layout this project has not seen, and guessing which line held the names
    would be a guess about every number underneath it.
    """
    lines = text.splitlines()
    header = [line for line in lines[:2] if line.startswith("#")]
    if len(header) != 2:
        raise ValueError(
            f"{payload.url}: expected two '#' header lines (names, then units); "
            f"got {len(header)}. This is an NDBC layout docs/02 has not recorded."
        )

    names = header[0].lstrip("#").split()
    units = header[1].lstrip("#").split()
    if tuple(names[:5]) != TIME_COLUMNS:
        raise ValueError(
            f"{payload.url}: expected the time columns {list(TIME_COLUMNS)}, got {names[:5]}. "
            "Pre-2005 archives use a different time layout and are not supported."
        )

    body = "\n".join(line for line in lines[2:] if line.strip() and not line.startswith("#"))
    return names, units, io.StringIO(body)


def _timestamps(table: pd.DataFrame, payload: Payload) -> pd.Series:
    """The five time columns as one tz-aware UTC instant per row.

    NDBC publishes in UTC, so this is a read rather than a conversion -- but the
    zone is attached explicitly, because storage refuses anything that has not
    said so (hard rule 2).
    """
    try:
        return pd.to_datetime(
            {
                "year": table["YY"].astype(int),
                "month": table["MM"].astype(int),
                "day": table["DD"].astype(int),
                "hour": table["hh"].astype(int),
                "minute": table["mm"].astype(int),
            },
            utc=True,
        )
    except (ValueError, TypeError) as error:
        raise ValueError(f"{payload.url}: time columns would not parse: {error}") from error


def _check_unit(
    spec: ColumnSpec, declared: dict[str, str], *, payload: Payload, layout: str
) -> None:
    reported = declared.get(spec.column)
    if reported != spec.unit:
        raise ValueError(
            f"{payload.url}: {spec.column} is declared in {reported!r} but this {layout} "
            f"parser expects {spec.unit!r}. NDBC changed a unit, or this is a layout "
            f"docs/02 has not recorded -- do not store the column until it is checked."
        )


def _values(column: pd.Series, spec: ColumnSpec) -> tuple[pd.Series, list[str]]:
    """One column as floats, with sentinels as null.

    Returns the tokens that were neither. `MM` is expected and silent; anything
    else is a surprise the manifest should carry, because a token quietly read as
    missing and a measurement that really is missing are indistinguishable
    afterwards.
    """
    numeric = pd.to_numeric(column, errors="coerce")
    unreadable = [
        str(token)
        for token, parsed in zip(column, numeric, strict=True)
        if pd.isna(parsed) and str(token).strip() != "MM"
    ]
    return numeric.mask(numeric == spec.missing), unreadable


def _rows(
    timestamps: pd.Series,
    values: pd.Series,
    *,
    site_id: str,
    parameter: str,
    depth_m: float | None,
    run_id: str,
) -> pd.DataFrame:
    """docs/03 rows for one parameter.

    Everything lands at `not evaluated` except an absent value, which is `9`.
    A public station has no deployment window, so `qc_tests` is empty: there is
    no ingest-time verdict to record, and the QARTOD tests run later in
    `kelpcompare qc` exactly as they do for a project sensor.
    """
    return pd.DataFrame(
        {
            "timestamp": timestamps.to_numpy(),
            "site_id": site_id,
            "parameter": parameter,
            "value": values.to_numpy(dtype="float64"),
            "depth_m": depth_m,
            "qc_flag": values.isna()
            .map({True: FLAG_MISSING, False: FLAG_NOT_EVALUATED})
            .astype("int8"),
            "qc_tests": "",
            "source": SOURCE,
            "fetch_run_id": run_id,
        }
    )
