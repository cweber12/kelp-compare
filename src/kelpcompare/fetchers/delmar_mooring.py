"""Del Mar shelf mooring -> docs/03 observation rows (docs/02).

The SIO/SCCOOS mooring on the ~90 m shelf off Del Mar, and the first SCCOOS
dataset this project reads. It exists here for a hole docs/04 §4.5 names: the
three North County beds have no local series carrying an anomaly, because
`NDBC:46266` begins 2019-12 and clears no baseline under docs/04 §3. This
mooring's 1 m record starts in 2006 and does.

It is close to the RTOMS module in shape -- an ERDDAP `tabledap` CSV, a moored
string, depth read from the payload -- and different in four ways that are the
whole content of this file.

**The depths are columns, not a `z` per row.** RTOMS reads a
`TimeSeriesProfile` flattened onto one vertical axis and takes `z` off each row.
This dataset serves one row per timestamp and a column per depth, `T_1m`
through `T_90m`, so the parse is a melt and the registry's declared depth set is
checked against the *header*. That is strictly better than RTOMS's position: an
undeclared sensor is visible before a single value is read, rather than inferred
from which depths turned up in the rows.

**The provider declares no unit, and mislabels what the numbers are.** There is
no `units` attribute on any temperature column, the CSV's units line is empty
for all nine, and `ioos_category` reads `Sea Level` -- which they are not. So
there is no unit to verify, and `EXPECTED_UNIT` pins the *absence* instead: the
parse refuses a payload that has started declaring one, because a unit appearing
is a provider change someone has to read rather than a fact this module may
absorb. That degC is the right reading is inferred, and the inference is
recorded rather than hidden: ERDDAP's own `actual_range` is 11.56-27.57 on
`T_1m` and 10.2-23.65 on `T_15m`, which is coastal Southern California in degC
and is nothing at all in degF.

**Every row is in the wrong hemisphere.** `longitude` arrives as `+117.32` under
`units = degrees_east` on all 371,657 served rows. The mooring is at −117.32.
This module refuses a positive longitude rather than negating it, for the reason
the RTOMS module refuses a positive `z`: a sign convention corrected upstream is
invisible afterwards, and a silent negation here would flip every landed row's
meaning the day it happened. The position that reaches the record comes from
`sites.json` regardless -- nothing downstream reads these columns -- so refusing
costs nothing and catches the one thing worth catching.

**There are no provider QC flags at all.** Both datasets are titled
`*** PRELIMINARY, No QA/QC info ***` and carry no `_qc_agg`, no `_qc_tests`, no
flag column. Every other pulled source in this project hands over some verdict;
this one hands over none, so rows land `2 / not evaluated` and `kelpcompare qc`
against the `parameters.json` bounds is the only opinion there is. That is the
position the project's own loggers are in, so the machinery fits -- but it is
worth knowing before reading a result off this series.

**Conditional requests do not work here, and are not pretended.** ERDDAP answers
`If-Modified-Since` with `200` and the whole body, verified against the same
software for RTOMS on 2026-08-28. `validators` is accepted and ignored, so this
module never raises `NotModified`; a re-run is made cheap by asking for a
narrower window instead.
"""

from __future__ import annotations

import io
import time
from urllib.parse import quote

import pandas as pd

from kelpcompare import __version__
from kelpcompare.fetchers.base import (
    ParsedPayload,
    Payload,
    SourceUnavailable,
    new_payload,
)
from kelpcompare.normalize import convert_unit
from kelpcompare.parameters import Parameters
from kelpcompare.storage import (
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    OBSERVATION_COLUMNS,
    empty_observations,
)

#: The docs/03 source vocabulary name for this fetcher's rows.
SOURCE = "delmar_mooring"

FETCHER_NAME = "delmar_mooring"

#: This fetcher reads `depth_m` from the payload, so the ingest CLI hands it the
#: registry's declared depth *set* rather than the scalar map a fixed-depth
#: station gets (docs/03 "A source may be self-describing on depth"). Read by
#: `cli._ingest_window`.
READS_DEPTH_FROM_PAYLOAD = True

#: SCCOOS rather than CeNCOOS: this is a SIO mooring and SCCOOS is the regional
#: association that serves it. Same ERDDAP software as the RTOMS feeds, so the
#: URL grammar and the two-line CSV header are identical.
BASE_URL = "https://erddap.sccoos.org/erddap/tabledap/{dataset_id}.csv"

#: The nine depth columns, and the metres each stands for. Ordered shallow to
#: deep, which is the order they are requested in and the order the parse checks
#: it got them in.
#:
#: Spelled out rather than parsed from whatever columns turned up, because the
#: depth is the thing being declared: a column named `T_100m` appearing next
#: year is a sensor nobody has reviewed, and it has to be refused by name rather
#: than land as a tenth series because its name happened to match a pattern.
DEPTH_COLUMNS: dict[str, float] = {
    "T_1m": 1.0,
    "T_6m": 6.0,
    "T_15m": 15.0,
    "T_21m": 21.0,
    "T_32m": 32.0,
    "T_45m": 45.0,
    "T_57m": 57.0,
    "T_72m": 72.0,
    "T_90m": 90.0,
}

#: Requested in this order, and the parse checks it got them in this order.
#: `latitude` and `longitude` are asked for although nothing downstream reads
#: them: they are what makes the hemisphere check possible, and a payload landed
#: without them could not be re-checked from `raw/` afterwards.
VARIABLES = ("time", "latitude", "longitude", *DEPTH_COLUMNS)

#: The one parameter read from this feed. Salinity is served as
#: `delmar_salinity` and is deliberately not read -- it would need a
#: `parameters.json` entry whose QC bounds nothing in this project has evidence
#: for, and docs/03 refuses a guessed threshold. There is no oxygen or
#: chlorophyll dataset on this server at all, whatever the source bundle says.
PARAMETER = "sea_water_temperature"

#: What the units line declares for a temperature column today: nothing.
#:
#: This is a pinned absence, not a missing check. The provider publishes no
#: `units` attribute and an empty units cell for all nine columns, so there is
#: no token to verify against -- and a module that shrugged at that would also
#: shrug the day a unit appeared. Pinning the empty string turns that day into a
#: parse failure someone reads, which is the only handling that survives the
#: provider changing its mind.
EXPECTED_UNIT = ""

#: What the values are taken to be, given the provider says nothing. Named
#: rather than inlined so that the assumption is greppable and sits next to the
#: evidence for it in this module's docstring.
ASSUMED_UNIT = "degree_Celsius"

#: Roughly the window NDBC's realtime feed covers, so "realtime" means the same
#: span across sources. ERDDAP resolves `now-Nd` server-side.
REALTIME_DAYS = 45

#: Seconds to wait before the single retry docs/02 asks for.
RETRY_DELAY_SECONDS = 2.0

#: Statuses worth asking a second time. ERDDAP answers a query that matched
#: nothing with 404 and a text body saying so, which is an answer rather than an
#: outage and must not be retried.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: How this project identifies itself (docs/02 "Saying who is asking").
USER_AGENT = f"kelpcompare/{__version__}"


def _url(dataset_id: str, constraint: str) -> str:
    """A `tabledap` CSV query for one dataset and one time constraint.

    The variable list is part of the URL, so a raw landing records exactly which
    columns were asked for and re-parses as the payload it was rather than as a
    file with a column missing.
    """
    return f"{BASE_URL.format(dataset_id=dataset_id)}?{','.join(VARIABLES)}{constraint}"


def realtime_url(dataset_id: str) -> str:
    """The most recent `REALTIME_DAYS` for this mooring.

    Public so the caller can look up what it already knows about this URL before
    asking for it; a fetcher that read the validator cache itself would be
    reaching outside its own raw zone, which docs/02 forbids.
    """
    return _url(dataset_id, f"&time%3E=now-{REALTIME_DAYS}days")


def archive_url(dataset_id: str, year: int) -> str:
    """One calendar year for this mooring.

    Half-open on the right so two consecutive years cannot both claim midnight
    on 1 January. They would dedupe on `OBSERVATION_KEY` anyway, but a window
    overlapping its neighbour by one instant makes every row count in the
    manifest off by a handful and unexplainable.
    """
    opens = quote(f"{year}-01-01T00:00:00Z")
    closes = quote(f"{year + 1}-01-01T00:00:00Z")
    return _url(dataset_id, f"&time%3E={opens}&time%3C{closes}")


def fetch_realtime(
    dataset_id: str, *, session=None, validators: dict[str, str] | None = None
) -> Payload:
    """The rolling recent window, which for this mooring is always empty.

    The record ends 2021-05-05 and the mooring is not reporting, so this asks a
    question whose true answer is "nothing" every time. That is recorded as a
    gap rather than engineered around, because it is a true statement about the
    record -- but the message says so, so a reader of the manifest is not left
    inferring an outage from a bare "no rows matched".
    """
    url = realtime_url(dataset_id)
    body, etag, last_modified = _get(url, session, closed_record=True)
    return new_payload(
        SOURCE,
        dataset_id,
        f"{dataset_id}_realtime.csv",
        url,
        body,
        etag=etag,
        last_modified=last_modified,
    )


def fetch_archive(
    dataset_id: str, year: int, *, session=None, validators: dict[str, str] | None = None
) -> Payload:
    """One calendar year for this mooring.

    A year the mooring did not report is an ordinary hole in a public record --
    it is serviced between deployments and the longest gap in the served record
    is 59.3 days -- so ERDDAP's "nothing matched" becomes `SourceUnavailable`,
    recorded as a gap and stepped over (docs/01 §5).

    `validators` is accepted and ignored, for the reason the module docstring
    gives: ERDDAP answers a conditional request with the whole body.
    """
    url = archive_url(dataset_id, year)
    body, etag, last_modified = _get(url, session)
    return new_payload(
        SOURCE,
        dataset_id,
        f"{dataset_id}_{year}.csv",
        url,
        body,
        etag=etag,
        last_modified=last_modified,
    )


def parse(
    payload: Payload,
    parameters: Parameters,
    *,
    site_id: str,
    declared_depths: tuple[float, ...] = (),
    measured_parameters: tuple[str, ...] = (),
    run_id: str,
) -> ParsedPayload:
    """One ERDDAP CSV -> docs/03 observation rows, UTC and SI.

    Raises `ValueError` on a layout, a unit or a hemisphere this module has not
    verified, for the reason docs/02 gives: the honest answer is that we do not
    know what the numbers mean, and that belongs in front of a human rather than
    behind a default.

    `declared_depths` is the registry's record of which depths carry this
    parameter, and here it filters *columns* rather than rows. A declared column
    is melted whether or not it holds readings, because an empty sensor is an
    outage and docs/03 keeps outages in the record. An undeclared column
    carrying readings is the interesting case and is reported by name: it means
    the string came back from a refit with a sensor nobody has reviewed.

    An empty `declared_depths` means the registry has not recorded them, in
    which case every known column is read and the gap is reported -- an
    unrecorded fact must not quietly become missing data.
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
                    "this feed carries nothing else this project stores"
                ),
            ),
        )
    if PARAMETER not in parameters:
        raise ValueError(
            f"{payload.station}: {PARAMETER!r} is not in {parameters.path}; "
            "the controlled parameter has to exist before its rows can land"
        )

    names, units, table = _read(payload)
    _check_units(dict(zip(names, units, strict=False)), payload)
    _check_hemisphere(table["longitude"], payload)

    columns, dropped = _selection(declared_depths)
    warnings.extend(dropped)
    if not columns or table.empty:
        return _nothing(payload, rows_in=len(table), warnings=tuple(warnings))

    parameter = parameters[PARAMETER]
    timestamps = pd.to_datetime(table["time"], utc=True, format="ISO8601")

    # The melt. One frame per declared column, concatenated -- rather than
    # `pd.melt`, which would have to be told the column-to-depth mapping
    # afterwards anyway and would put the two halves of that mapping in
    # different statements.
    #
    # A column with no reading at a timestamp stays, flagged missing: this
    # string reports all its depths on one clock, so an absence at a declared
    # depth is that sensor's own outage and is the gap docs/03 wants in the
    # record. That is the whole reason this source needs none of the sampling
    # grid reconstruction the RTOMS module carries -- there, several
    # instruments share a vertical axis and a null may belong to another one.
    frames = []
    missing = 0
    for column in columns:
        values = pd.to_numeric(table[column], errors="coerce")
        absent = values.isna().to_numpy()
        missing += int(absent.sum())
        frames.append(
            pd.DataFrame(
                {
                    "timestamp": timestamps.to_numpy(),
                    "site_id": site_id,
                    "parameter": parameter.name,
                    "value": convert_unit(values, ASSUMED_UNIT, parameter.unit).to_numpy(
                        dtype="float64"
                    ),
                    "depth_m": DEPTH_COLUMNS[column],
                    # No provider verdict exists on this feed, so every row
                    # arrives unjudged and `kelpcompare qc` supplies the only
                    # one. An absent reading is `missing` rather than
                    # `not evaluated`: docs/03 gives 9 to a row with no value,
                    # and there is nothing in an absence to judge.
                    "qc_flag": pd.Series(FLAG_NOT_EVALUATED, index=values.index).mask(
                        absent, FLAG_MISSING
                    ),
                    "qc_tests": "",
                    "source": SOURCE,
                    "fetch_run_id": run_id,
                }
            )
        )

    frame = pd.concat(frames, ignore_index=True)
    frame = frame.sort_values(["timestamp", "depth_m"], kind="stable").reset_index(drop=True)

    return ParsedPayload(
        frame=frame[list(OBSERVATION_COLUMNS)],
        station=payload.station,
        layout=payload.station,
        rows_in=len(table),
        warnings=tuple(warnings),
        missing_counts={PARAMETER: missing},
    )


def _nothing(payload: Payload, *, rows_in: int, warnings: tuple[str, ...]) -> ParsedPayload:
    """A well-formed payload that yielded no rows for this project."""
    return ParsedPayload(
        frame=empty_observations(),
        station=payload.station,
        layout=payload.station,
        rows_in=rows_in,
        warnings=warnings,
    )


def _read(payload: Payload) -> tuple[list[str], list[str], pd.DataFrame]:
    """Split the two-line ERDDAP header from the body, and refuse a surprise.

    `tabledap` CSV opens with a names line and then a *units* line, which is
    what makes `_check_units` possible at all.

    `keep_default_na=False` is load-bearing rather than tidiness: pandas would
    otherwise convert a list of tokens of its own to NaN before this module sees
    them, making a token nobody verified indistinguishable from the `NaN` ERDDAP
    actually writes.
    """
    text = payload.body.decode("utf-8-sig", errors="strict")
    lines = text.splitlines()
    if len(lines) < 2:
        raise ValueError(
            f"{payload.station}: this payload has {len(lines)} line(s); a tabledap CSV opens "
            "with a names line and a units line"
        )

    names = [name.strip() for name in lines[0].split(",")]
    units = [unit.strip() for unit in lines[1].split(",")]
    if tuple(names) != VARIABLES:
        raise ValueError(
            f"{payload.station}: this payload carries columns {names}, not the "
            f"{list(VARIABLES)} that were requested -- the dataset's variables have changed "
            "and docs/02 needs updating before these rows can be trusted"
        )

    body = io.StringIO("\n".join(lines[2:]))
    table = pd.read_csv(body, names=names, dtype=str, keep_default_na=False, engine="python")
    return names, units, table


def _check_units(declared: dict[str, str], payload: Payload) -> None:
    """Stop the parse if a temperature column has started declaring a unit.

    Inverted from every other fetcher's unit check, and deliberately. There is
    nothing to verify -- this provider publishes no unit for any of the nine
    columns -- so what is pinned is the absence. A unit appearing means the
    provider has said something about these numbers for the first time, and
    whether it agrees with `ASSUMED_UNIT` is exactly the question a human should
    answer once, rather than one this module should answer every run.
    """
    surprises = {
        column: declared.get(column, "")
        for column in DEPTH_COLUMNS
        if declared.get(column, "") != EXPECTED_UNIT
    }
    if surprises:
        named = ", ".join(f"{column}={unit!r}" for column, unit in sorted(surprises.items()))
        raise ValueError(
            f"{payload.station}: {named}. This feed has always declared no unit at all, so "
            f"{ASSUMED_UNIT!r} is this project's inference and not the provider's statement "
            "(docs/02). A unit appearing is the provider settling that question -- read it and "
            "update docs/02 before these rows land"
        )


def _check_hemisphere(column: pd.Series, payload: Payload) -> None:
    """Refuse the positive longitude this feed serves, rather than negating it.

    Every served row reports `+117.32` under `units = degrees_east`, which is
    western China; the mooring is at −117.32. Nothing downstream reads this
    column -- position comes from `sites.json` -- so the only thing this check
    can do is notice the day the provider fixes it, which is precisely the day
    somebody needs to re-read docs/02. Negating it here would make that day
    invisible.
    """
    longitude = pd.to_numeric(column, errors="coerce").dropna()
    if longitude.empty:
        return
    if not bool((longitude > 0).all()):
        raise ValueError(
            f"{payload.station}: longitude is no longer uniformly positive "
            f"(range {longitude.min()} to {longitude.max()}). This feed has always served the "
            "sign inverted -- +117.32 for a mooring at -117.32 -- so a change here means the "
            "provider has corrected it, and docs/02 and sites.json need re-reading rather than "
            "this parse continuing"
        )


def _selection(declared: tuple[float, ...]) -> tuple[tuple[str, ...], list[str]]:
    """Which depth columns are this site's, and what to say about the rest.

    Decided on the header rather than on the rows, which is the one way this
    source is easier than RTOMS: an undeclared sensor is a column name, visible
    before any value is read, so it cannot be confused with a declared sensor
    that happened to report nothing.
    """
    if not declared:
        return tuple(DEPTH_COLUMNS), [
            (
                "the site registry declares no depths for this parameter, so all "
                f"{len(DEPTH_COLUMNS)} depth columns were stored; record them in "
                "sensor_depths_m so a new sensor depth is noticed rather than landed"
            )
        ]

    keep = tuple(column for column, depth in DEPTH_COLUMNS.items() if depth in declared)
    warnings: list[str] = []
    surprises = sorted(depth for column, depth in DEPTH_COLUMNS.items() if depth not in declared)
    if surprises:
        warnings.append(
            f"{len(surprises)} depth column(s) are served but not declared in sensor_depths_m "
            f"and were NOT stored: {', '.join(f'{d:g} m' for d in surprises)}. Review them and "
            "add them to the registry, because depth_m is part of the storage key and cannot be "
            "corrected once rows have landed"
        )
    return keep, warnings


def _get(url: str, session, *, closed_record: bool = False):
    """One GET, one polite retry, and ERDDAP's "nothing matched" as an outage.

    Mirrors the RTOMS module's transport rather than sharing it: the two differ
    on host, on which statuses mean what, and on the message a caller should get
    back, and a shared helper parameterised on all three would be longer than
    both.
    """
    try:
        import requests
    except ModuleNotFoundError as missing:  # pragma: no cover - environment guard
        raise SourceUnavailable(
            f"requests is not installed, so {url} cannot be fetched"
        ) from missing

    get = (session or requests).get
    headers = {"User-Agent": USER_AGENT}

    last: Exception | None = None
    for attempt in (0, 1):
        try:
            response = get(url, headers=headers, timeout=120)
        except Exception as error:  # noqa: BLE001 - any transport failure is an outage
            last = error
        else:
            if response.status_code == 404:
                # ERDDAP answers an empty result with 404 and a text body saying
                # so. That is an answer, not an outage to retry -- and for this
                # mooring the commonest cause is a window past the end of a
                # record that stopped in 2021, which the message says outright.
                raise SourceUnavailable(
                    f"{url} matched no rows"
                    + (
                        "; this mooring's record ends 2021-05-05 and it is not currently "
                        "reporting, so a realtime window is expected to be empty"
                        if closed_record
                        else " (a servicing gap, or a year outside the record)"
                    )
                )
            if response.status_code not in RETRYABLE_STATUS:
                response.raise_for_status()
                return (
                    response.content,
                    response.headers.get("ETag"),
                    response.headers.get("Last-Modified"),
                )
            last = SourceUnavailable(f"{url} returned HTTP {response.status_code}")
        if attempt == 0:
            time.sleep(RETRY_DELAY_SECONDS)

    raise SourceUnavailable(f"{url} could not be fetched: {last}")
