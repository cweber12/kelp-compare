"""City of San Diego RTOMS moorings -> docs/03 observation rows (docs/02).

The only module that knows what a CeNCOOS ERDDAP `tabledap` CSV looks like
(docs/01 layer 1). Two moored strings off the Point Loma and South Bay ocean
outfalls, each carrying `sea_water_temperature` at many depths at once -- the
only depth-resolved temperature in the study region, which is the whole reason
this source exists here (docs/02 "City of San Diego RTOMS").

**Depth comes from the payload, not the registry.** A mooring measures one
parameter at many depths, so there is no single `sensor_depths_m` value to
write and the registry instead declares the *set* of depths anyone has looked
at (docs/03 "A source may be self-describing on depth"). This module reads
`z` per row and checks it against that set, so a string back from a refit with
a sensor at a new depth is reported rather than landed as a series nobody has
reviewed.

**`z` is altitude and the sign flips.** ERDDAP serves the vertical coordinate
positive *up*, so every value is negative or zero; docs/03 `depth_m` is
positive *down*. Getting this backwards puts every reading above the water and
is completely silent in a Parquet file, so it is asserted at the boundary.

**Most rows are not this sensor's.** The datasets are `TimeSeriesProfile` and
flatten every instrument on the string onto one vertical axis, so the ADCP
contributes a velocity bin every metre and temperature is null on all of them.
The declared depth set is what tells a real outage at a real sensor depth --
which stays in the record, flagged missing -- from another instrument's bin,
which was never this parameter's row at all.

**The QARTOD vocabulary is already ours.** `_qc_agg` declares
`flag_values: 1, 2, 3, 4, 9` against
`flag_meanings: PASS NOT_EVALUATED SUSPECT FAIL MISSING`, which is the docs/03
`qc_flag` set value for value. So this is a pass-through, not a mapping, and
there is no translation table to get wrong. `_qc_tests` is an 11-character
string whose positions the provider names in its own `comment` attribute; it is
decoded into the docs/03 `name:status` form so the provider's evidence survives
into `qc_tests` rather than being discarded (docs/02 SCCOOS: feeds carrying
their own QC flags are mapped into our scheme, not dropped).

**Conditional requests do not work here, and are not pretended.** ERDDAP
answers `If-Modified-Since` with `200` and the whole body, verified 2026-08-28.
A re-run is therefore made cheap by asking for a narrower time window rather
than by asking whether the last one changed -- which suits a growing time
series better than an ETag would anyway.
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
from kelpcompare.qc.flags import FLAG_BY_STATUS, STATUS_BY_FLAG, format_tests
from kelpcompare.storage import (
    FLAG_FAIL,
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    FLAG_PASS,
    FLAG_SUSPECT,
    OBSERVATION_COLUMNS,
    empty_observations,
)

#: The docs/03 source vocabulary name for this fetcher's rows.
SOURCE = "sd_rtoms"

FETCHER_NAME = "sd_rtoms"

#: This fetcher reads `depth_m` from the payload, so the ingest CLI hands it the
#: registry's declared depth *set* to check against rather than the scalar map it
#: gives a fixed-depth station (docs/03 "A source may be self-describing on
#: depth"). Read by `cli._ingest_window`; there is nothing else to branch on,
#: since both kinds of fetcher otherwise present the same module interface.
READS_DEPTH_FROM_PAYLOAD = True

#: CeNCOOS rather than the City's own portal. Both serve these measurements;
#: docs/02 records why this one won -- it runs a year and a half further, it
#: subsets server-side instead of shipping the year, and its QC flags are already
#: the docs/03 vocabulary.
BASE_URL = "https://erddap.cencoos.org/erddap/tabledap/{dataset_id}.csv"

#: Requested in this order, and the parse checks it got them in this order. The
#: QC pair travels with the value deliberately: a fetch that returned the reading
#: without the provider's verdict on it would silently downgrade every row to
#: "not evaluated" while looking like a complete payload.
VARIABLES = (
    "time",
    "latitude",
    "longitude",
    "z",
    "sea_water_temperature",
    "sea_water_temperature_qc_agg",
    "sea_water_temperature_qc_tests",
)

#: The one parameter read from these feeds. The moorings also carry salinity,
#: oxygen, pH, chlorophyll, CDOM, turbidity, xCO2, BOD and currents; none has a
#: `parameters.json` entry, and adding one is a registry decision about SI units
#: and QC bounds rather than a parsing convenience (docs/02).
PARAMETER = "sea_water_temperature"

#: What the file's own units line has to declare for the value column. Checked
#: rather than assumed, for the reason the NDBC module gives at greater length:
#: a temperature in the wrong unit stored as degC survives into a publication.
EXPECTED_UNIT = "degree_Celsius"

#: Roughly the window NDBC's realtime feed covers, so "realtime" means the same
#: span across sources. ERDDAP resolves `now-Nd` server-side.
REALTIME_DAYS = 45

#: The provider's own name for each position in the `_qc_tests` string, from the
#: `comment` attribute on that variable, mapped to this project's test names.
#: The four the project also computes keep their names on purpose: a later
#: `kelpcompare qc` run supersedes them with verdicts from `parameters.json`
#: bounds, which is the right precedence, and `qc.qartod` preserves the seven it
#: does not implement rather than dropping them.
TEST_NAMES = (
    "gap",
    "syntax",
    "location",
    "gross_range",
    "climatology",
    "spike",
    "rate_of_change",
    "flat_line",
    "multi_variate",
    "attenuated_signal",
    "neighbor",
)

#: Every value docs/03 allows in `qc_flag`. Wider than `qc.flags.STATUS_BY_FLAG`
#: on purpose: that map deliberately has no word for "not evaluated", because a
#: test reaching no verdict should record nothing rather than record that it said
#: nothing -- but 2 is still a perfectly good flag for a *row*, and it is the one
#: the provider writes on every profile bin. Validating the aggregate against the
#: narrower map would reject the commonest value on the feed.
STORABLE_FLAGS = frozenset({FLAG_PASS, FLAG_NOT_EVALUATED, FLAG_SUSPECT, FLAG_FAIL, FLAG_MISSING})

#: Seconds to wait before the single retry docs/02 asks for.
RETRY_DELAY_SECONDS = 2.0

#: Statuses worth asking a second time. ERDDAP answers a query that matched
#: nothing with 404 and a text body saying so, which is an answer rather than an
#: outage and must not be retried.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: How this project identifies itself. Same reasoning as the NDBC module: a
#: default `python-requests` user agent gives a public service nobody to contact
#: and is what gets throttled.
USER_AGENT = f"kelpcompare/{__version__}"


def _url(dataset_id: str, constraint: str) -> str:
    """A `tabledap` CSV query for one dataset and one time constraint.

    The variable list is part of the URL, so the raw landing records exactly
    which columns were asked for. A payload that was fetched before a column was
    added to `VARIABLES` therefore re-parses as the payload it was, rather than
    looking like a file with a column missing.
    """
    variables = ",".join(VARIABLES)
    return f"{BASE_URL.format(dataset_id=dataset_id)}?{variables}{constraint}"


def realtime_url(dataset_id: str) -> str:
    """The most recent `REALTIME_DAYS` for one mooring.

    Public so the caller can look up what it already knows about this URL before
    asking for it -- the same reason the NDBC module exposes its URLs. A fetcher
    that read the validator cache itself would be writing outside its own raw
    zone, which docs/02 forbids.
    """
    return _url(dataset_id, f"&time%3E=now-{REALTIME_DAYS}days")


def archive_url(dataset_id: str, year: int) -> str:
    """One calendar year for one mooring.

    Half-open on the right so two consecutive years cannot both claim midnight on
    1 January. They would dedupe on `OBSERVATION_KEY` anyway, but a window that
    overlaps its neighbour by one instant makes every row count in the manifest
    off by a handful and unexplainable.
    """
    start = quote(f"{year}-01-01T00:00:00Z")
    end = quote(f"{year + 1}-01-01T00:00:00Z")
    return _url(dataset_id, f"&time%3E={start}&time%3C{end}")


def fetch_realtime(
    dataset_id: str, *, session=None, validators: dict[str, str] | None = None
) -> Payload:
    """The rolling recent window for one mooring."""
    url = realtime_url(dataset_id)
    body, etag, last_modified = _get(url, session)
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
    """One calendar year for one mooring.

    A year the mooring did not report is an ordinary hole in a public record --
    these are redeployed annually and go down between deployments -- so ERDDAP's
    "nothing matched" answer becomes `SourceUnavailable`, recorded as a gap and
    stepped over (docs/01 §5).

    `validators` is accepted and ignored. The ingest CLI passes what a previous
    run recorded about this URL to every fetcher alike, and ERDDAP answers
    `If-Modified-Since` with the whole body and a `200` (verified 2026-08-28), so
    sending them would cost a round trip to learn nothing. Taking the argument
    and doing nothing with it is better than the CLI having to know which sources
    support conditional requests -- but it is why this module never raises
    `NotModified`.
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

    Raises `ValueError` on a layout or a unit this module has not verified, for
    the reason docs/02 gives: the honest answer is that we do not know what the
    numbers mean, and that belongs in front of a human rather than behind a
    default.

    `declared_depths` is the registry's record of which depths carry this
    parameter. It is a filter and a check, not a source of values -- the depth
    itself is read per row. A row at a declared depth is kept even when its value
    is absent, because that is an outage at a known sensor and docs/03 keeps
    outages in the record; a row at an undeclared depth is not this parameter's
    row at all and is dropped. An undeclared depth carrying an actual reading is
    the interesting case and is reported by name: it means the string came back
    from a refit with a sensor nobody has reviewed.

    An empty `declared_depths` means the registry has not recorded them, in which
    case every row carrying a value is kept and the gap is reported -- an
    unrecorded fact must not quietly become missing data, the same rule
    `measured_parameters` follows.
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
    _check_unit(dict(zip(names, units, strict=False)), payload)

    depths = _depths(table["z"], payload)
    values = pd.to_numeric(table["sea_water_temperature"], errors="coerce")

    keep, dropped = _selection(depths, values, declared_depths)
    warnings.extend(dropped)

    kept = table[keep]
    if kept.empty:
        return _nothing(payload, rows_in=len(table), warnings=tuple(warnings))

    parameter = parameters[PARAMETER]
    absent = pd.isna(values[keep].to_numpy())
    flags, tests, flag_warnings = _verdicts(kept, absent, payload)
    warnings.extend(flag_warnings)

    # A row with no reading AND no verdict on it is not this sensor's row.
    #
    # The depth filter above catches the ADCP bins, which sit at depths no
    # temperature sensor occupies. It cannot catch the same thing happening *at*
    # a temperature depth: another instrument at 20 m reporting on a clock a
    # minute off the temperature sensor's puts a row at (t, -20.0) with the
    # temperature null, and 20 m is a declared depth. On a real 2023 South Bay
    # ingest that was 17,755 rows -- it made a series that is essentially
    # complete look 40% missing, which would carry into `pct_coverage` and the
    # quarterly features built on it.
    #
    # The provider separates them itself, and exactly: across that ingest every
    # row carrying a value had a qc_tests verdict, without exception, so an
    # empty verdict means the provider never ran a temperature test on that row.
    # That is not a sensor that failed -- it is a row that was never about this
    # sensor. A gap the provider *did* evaluate keeps its row and its flag 9,
    # which is the outage docs/03 wants in the record.
    phantom = absent & pd.Series(tests).eq("").to_numpy()
    #
    # Dropped silently, like the profile bins above and for the same reason: it
    # is a property of how ERDDAP flattens a TimeSeriesProfile, present in every
    # payload, and a warning on every ingest about the normal shape of the feed
    # is one the operator learns to ignore. The attrition is visible anyway --
    # the manifest records rows_in against rows_out -- and docs/02 explains it.
    if phantom.any():
        # `keep` indexes the whole table and decides which values are read below,
        # so it has to lose exactly the rows `kept` does or the two fall out of
        # step and every column after this is offset against its own timestamps.
        keep = keep.copy()
        keep.loc[kept.index[phantom]] = False
        survivors = ~phantom
        kept = kept[survivors]
        flags = flags[survivors]
        tests = [text for text, alive in zip(tests, survivors, strict=True) if alive]
        absent = absent[survivors]
        if kept.empty:
            return _nothing(payload, rows_in=len(table), warnings=tuple(warnings))

    # An absent reading is `missing`, whatever the provider called it. docs/03
    # gives 9 to a row with no value, and `qc.flags` states the reason as a
    # deliberate divergence from `ioos_qc`: there is nothing in an absence to
    # judge. The provider writes 2 -- not evaluated -- on these, which is true of
    # its own tests but would land a hole in the record under a flag the default
    # `qc_flag <= 2` filter lets through as data.
    flags = flags.copy()
    flags[absent] = FLAG_MISSING

    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(kept["time"], utc=True, format="ISO8601").to_numpy(),
            "site_id": site_id,
            "parameter": parameter.name,
            "value": convert_unit(values[keep], EXPECTED_UNIT, parameter.unit).to_numpy(
                dtype="float64"
            ),
            "depth_m": depths[keep].to_numpy(dtype="float64"),
            "qc_flag": flags,
            "qc_tests": tests,
            "source": SOURCE,
            "fetch_run_id": run_id,
        }
    )
    frame = frame.sort_values(["timestamp", "depth_m"], kind="stable").reset_index(drop=True)

    return ParsedPayload(
        frame=frame[list(OBSERVATION_COLUMNS)],
        station=payload.station,
        layout=payload.station,
        rows_in=len(table),
        warnings=tuple(warnings),
        missing_counts={PARAMETER: int(values[keep].isna().sum())},
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

    `tabledap` CSV opens with a names line and then a *units* line, which is why
    a unit can be checked at all rather than assumed from a variable name.

    `keep_default_na=False` is load-bearing rather than tidiness, for the reason
    the NDBC module gives: pandas otherwise converts a list of tokens of its own
    to NaN before this module sees them, making a token nobody verified
    indistinguishable from the `NaN` ERDDAP actually writes.
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


def _check_unit(declared: dict[str, str], payload: Payload) -> None:
    """Stop the parse on a unit this module has not verified.

    The same station can report the same quantity under different unit tokens
    depending on what was asked for, which is why this reads the file's own
    units line instead of trusting the variable name.
    """
    found = declared.get(PARAMETER, "")
    if found != EXPECTED_UNIT:
        raise ValueError(
            f"{payload.station}: {PARAMETER} is declared in {found or '<no unit>'!r}, not "
            f"{EXPECTED_UNIT!r}; a temperature in an unverified unit must not enter the record"
        )


def _depths(column: pd.Series, payload: Payload) -> pd.Series:
    """`z` (altitude, positive up) -> docs/03 `depth_m` (positive down).

    A positive `z` would mean a sensor above the water line, which these
    moorings do not carry and which would land as a negative depth. Refused
    rather than negated quietly: the sign convention flipping upstream is
    exactly the kind of change that is invisible afterwards.
    """
    altitude = pd.to_numeric(column, errors="coerce")
    above = altitude > 0
    if bool(above.any()):
        raise ValueError(
            f"{payload.station}: {int(above.sum())} row(s) report a positive z, which would be "
            "a sensor above the water line; z is altitude and depth_m is its negation, so this "
            "is a sign convention change upstream rather than a deep reading"
        )
    return -altitude


def _selection(
    depths: pd.Series, values: pd.Series, declared: tuple[float, ...]
) -> tuple[pd.Series, list[str]]:
    """Which rows are this parameter's, and what to say about the rest.

    The declared depth set is what separates the two kinds of absent value on
    this feed. A null at a declared depth is a sensor that did not report and
    stays in the record flagged missing (docs/03); a null at an undeclared depth
    is another instrument's profile bin sharing the vertical axis and was never
    this parameter's row.
    """
    warnings: list[str] = []
    if not declared:
        return values.notna(), [
            (
                "the site registry declares no depths for this parameter, so every row carrying "
                "a reading was stored; record them in sensor_depths_m so a new sensor depth is "
                "noticed rather than landed"
            )
        ]

    known = pd.Series(depths.isin(declared), index=depths.index)
    surprises = sorted({float(d) for d in depths[~known & values.notna()].dropna()})
    if surprises:
        warnings.append(
            f"{len(surprises)} depth(s) carry a reading but are not declared in "
            f"sensor_depths_m and were NOT stored: {', '.join(f'{d:g} m' for d in surprises)}. "
            "A mooring back from a refit puts a sensor at a new depth; review it and add it to "
            "the registry, because depth_m is part of the storage key and cannot be corrected "
            "once rows have landed"
        )
    return known, warnings


def _verdicts(kept: pd.DataFrame, absent, payload: Payload):
    """`qc_flag` and `qc_tests` for the kept rows, from the provider's own QARTOD.

    `_qc_agg` is taken as `qc_flag` unchanged -- it is the provider's roll-up in
    the docs/03 vocabulary, value for value -- and `_qc_tests` is decoded into
    the docs/03 `name:status` form beside it.

    The two are cross-checked rather than assumed consistent. Where the decoded
    tests roll up to something the aggregate disagrees with, the provider's two
    columns disagree with each other, and a human should hear that rather than
    have this module silently prefer one. `qc_flag` still comes from the
    aggregate in that case: it is the column the provider documents as its
    verdict.

    **Rows with no value are excluded from that check.** On these feeds the
    provider systematically writes `qc_agg = 2` while its own `qc_tests` records
    the gross range test as `9` -- verified across the recorded fixture, where
    every one of the seven 26 m gaps disagrees this way (docs/02). The caller
    resolves those to `9` regardless, on docs/03's rule that an absent value is
    missing, so warning about them on every run would report a known quirk that
    has already been handled. What is left is a disagreement about a reading
    that exists, which nothing else accounts for.
    """
    warnings: list[str] = []
    aggregate = pd.to_numeric(kept["sea_water_temperature_qc_agg"], errors="coerce")

    unknown = sorted({int(f) for f in aggregate.dropna().unique() if int(f) not in STORABLE_FLAGS})
    if unknown:
        raise ValueError(
            f"{payload.station}: qc_agg carries {unknown}, which is not a docs/03 qc_flag; "
            "the provider's flag vocabulary has changed and docs/02 needs updating"
        )

    flags = aggregate.fillna(FLAG_NOT_EVALUATED).astype("int8").to_numpy()
    tests, disagreements = [], 0
    for raw, flag, missing in zip(
        kept["sea_water_temperature_qc_tests"], flags, absent, strict=True
    ):
        verdicts = _decode(raw)
        tests.append(format_tests(verdicts))
        if verdicts and not missing and _rollup(verdicts) != int(flag):
            disagreements += 1

    if disagreements:
        warnings.append(
            f"{disagreements} row(s) carrying a reading have a qc_agg that disagrees with "
            "rolling up their own qc_tests; qc_flag follows qc_agg, which is the column the "
            "provider documents as its verdict, but the two should not differ on a row that "
            "has a value"
        )
    return flags, tests, warnings


def _decode(raw: object) -> dict[str, str]:
    """One 11-character `_qc_tests` string -> `{test: status}`.

    Positions are named by the provider in that variable's own `comment`
    attribute and are pinned in `TEST_NAMES`. Tests that reached no verdict are
    omitted, which is the same thing `qc.flags` does when writing the column:
    a test that said nothing should record nothing, not record that it said
    nothing.

    Anything that is not the documented 11 characters yields no verdicts rather
    than a partial reading. The rows that carry `NaN` here are the profile bins
    the depth filter drops anyway, and a half-decoded QC string is worse than an
    absent one.
    """
    text = str(raw).strip()
    if len(text) != len(TEST_NAMES) or not text.isdigit():
        return {}
    verdicts = {}
    for name, digit in zip(TEST_NAMES, text, strict=True):
        flag = int(digit)
        if flag == FLAG_NOT_EVALUATED:
            continue
        if flag in STATUS_BY_FLAG:
            verdicts[name] = STATUS_BY_FLAG[flag]
    return verdicts


def _rollup(verdicts: dict[str, str]) -> int:
    """The docs/03 flag a set of decoded verdicts summarises to.

    Deliberately the project's precedence and not QARTOD's: missing ranks
    highest here, because there is nothing in an absent reading to judge. That
    divergence is stated once in `qc.flags` and this follows it, so the
    cross-check below compares like with like.
    """
    flags = {FLAG_BY_STATUS[status] for status in verdicts.values()}
    for level in (FLAG_MISSING, FLAG_FAIL, FLAG_SUSPECT, FLAG_PASS):
        if level in flags:
            return level
    return FLAG_NOT_EVALUATED


def _get(url: str, session) -> tuple[bytes, str | None, str | None]:
    """Retrieve one URL, with what the server called this version of it.

    Deliberately not shared with the NDBC module's near-twin. The two differ
    where it matters: this one sends no conditional-request headers, because
    ERDDAP answers them with the whole body and a `200`, and it reads a `404` as
    "the mooring did not report that window" rather than "no such station".
    Folding them together would mean a helper that branches on which source is
    calling it, which is the knowledge docs/01 keeps inside one module per
    source.

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
            response = session.get(url, timeout=60, headers=headers)
        except Exception as error:  # noqa: BLE001 -- one outage, however it arrived
            last = f"{type(error).__name__}: {error}"
            continue

        status = getattr(response, "status_code", None)
        if status == 200:
            served = getattr(response, "headers", None) or {}
            return response.content, _header(served, "ETag"), _header(served, "Last-Modified")

        last = f"HTTP {status}"
        if status not in RETRYABLE_STATUS:
            break

    raise SourceUnavailable(f"{url}: {last}")


def _header(headers, name: str) -> str | None:
    """One response header, or None. Case-insensitive, since HTTP is."""
    for key, value in dict(headers).items():
        if str(key).lower() == name.lower():
            return str(value)
    return None
