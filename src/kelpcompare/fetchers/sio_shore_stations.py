"""SIO Shore Stations La Jolla archive -> docs/03 observation rows (docs/02).

The only module that knows what a Shore Stations Program CSV looks like
(docs/01 layer 1), and the second source module with no `fetch`. **One daily
grab sample at Scripps Pier, surface and bottom, since 22 August 1916** -- the
longest in-situ temperature record in the study area by seventy years, and the
reason this source exists here at all.

Like Kelp Watch, it is downloaded by hand and dropped in
`raw/sio_shore_stations/incoming/`, so this is a parser and nothing else: no URL
to build, no outage to survive, no `SourceUnavailable` path. The archive sits
behind a Google Form registration and Anubis proof-of-work bot protection
(docs/02), which is a wall rather than an API, and a fetcher that got past it
would be an evasion rather than a fetcher.

It lives here rather than in `adapters/` for the reason `kelpwatch` does: that
package is the project-sensor vendor-file contract (docs/06) -- a serial, a
deployment window, a series map -- and a public shore station has none of them.
What makes a module belong here is knowing a source's format, and this knows one.

Six things about that format carry the weight, all verified against the fourteen
downloaded snapshots on 2026-08-28 and recorded in
`tests/fixtures/sio_shore_stations/`.

**The preamble is not a fixed length and the encoding is not fixed either.**
Forty-six lines in nine snapshots, forty-five in five; UTF-8 with a byte-order
mark in the newer ones, Mac Roman with none in the older. So the column header
is *found* rather than skipped past, and decoding falls back to latin-1, which
cannot raise. The only non-ASCII byte in any snapshot is the degree sign in the
position line, and nothing here reads it.

**Everything the registry is checked against comes out of the preamble.** The
archive date the pin is compared with, the position that says which station this
is, and the two nominal depths. None of them is assumed from this module, and a
snapshot that moved any of them is quarantined rather than landed under the old
values -- which matters most for the depths, because `depth_m` is part of
`OBSERVATION_KEY` and is therefore permanent (docs/03).

**Two depths are two series, and the file says which.** Surface (~0.5 m) and
bottom (~5 m) share every row, so `sensor_depths_m` declares the *list* form for
this site and these depths are checked against it rather than supplied by it
(docs/03 "A source may be self-describing on depth").

**An absent flag marks a series that did not exist yet.** The bottom series
starts on 1926-07-21, ten years after the surface one, and for exactly the 3,620
days before it the file writes both `BOT_TEMP_C` and `BOT_FLAG` empty -- and for
no other day. That is the source saying the series had not started, not that a
sample was missed, so those rows are dropped. A null *after* a series starts is
a real gap in a running program and lands flagged missing, which is the
distinction the RTOMS parser draws between an outage at a declared depth and
another instrument's profile bin, reached by different evidence.

**Times are PST, which is a fixed -08:00 and not `America/Los_Angeles`.** The
header names Pacific *Standard* Time; a DST-aware zone would move every summer
reading by an hour in the direction that looks like a diurnal signal.

**A day before 1990 has no time at all**, and takes `NOMINAL_LOCAL_TIME` --
10:38 PST, the median of the 12,473 days that do carry one. That is an estimate
from this program's own sampling behaviour rather than a placeholder, and the
choice is argued in docs/02 along with what was rejected. An imputed timestamp
is identifiable afterwards without a new column: a row that carries a
`sample_time` verdict had a time in the file, and a row that carries none did
not, which is the honest reading of docs/03's rule that a test reaching no
verdict records nothing.

Anything else surprising -- a column that is not there, a trailing column that
is not empty, a date that is half a date, a repeated day, a flag code the
file's own legend does not declare -- stops the parse rather than entering the
record, on the docs/02 rule that a format surprise belongs in front of a human
rather than behind a default.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from kelpcompare.adapters.base import Check
from kelpcompare.fetchers.base import ParsedPayload
from kelpcompare.normalize import convert_unit
from kelpcompare.parameters import Parameters
from kelpcompare.qc.flags import summarize
from kelpcompare.registry import Station
from kelpcompare.storage import (
    FLAG_FAIL,
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    FLAG_PASS,
    FLAG_SUSPECT,
    OBSERVATION_COLUMNS,
    empty_observations,
)

#: The docs/03 source vocabulary name for this module's rows.
SOURCE = "sio_shore_stations"

FETCHER_NAME = "sio_shore_stations"

#: The archive's columns, in order. Read from the file and checked, never
#: assumed: a column inserted upstream would shift every value one place, and
#: the salinity file in the same download differs from this one only here.
COLUMNS = (
    "YEAR",
    "MONTH",
    "DAY",
    "TIME_PST",
    "TIME_FLAG",
    "SURF_TEMP_C",
    "SURF_FLAG",
    "BOT_TEMP_C",
    "BOT_FLAG",
)

#: How the column header is found, since the preamble is 45 lines in five of the
#: fourteen snapshots and 46 in the other nine.
HEADER_PREFIX = "YEAR,MONTH,DAY,TIME_PST"

#: The one parameter these files carry. The same download ships `LaJolla_SALT_*`
#: in an otherwise identical layout; salinity has no `parameters.json` entry, and
#: adding one is a registry decision about SI units and QC bounds rather than a
#: parsing convenience (docs/02). `sniff` rejects that layout on its columns.
PARAMETER = "sea_water_temperature"

#: What the value columns declare in their own names. Checked through
#: `convert_unit` rather than assumed to be the storage unit, so a change to the
#: canonical unit in `parameters.json` converts instead of silently relabelling.
COLUMN_UNIT = "degC"

#: Missing, in both value and flag columns, everywhere in every snapshot.
MISSING_TOKEN = "NaN"


@dataclass(frozen=True)
class Series:
    """One of the two depths the archive carries in every row."""

    name: str
    value_column: str
    flag_column: str
    #: How the title line names this series' nominal depth, e.g. `Surface (~0.5m)`.
    depth_label: str


#: Surface first, which is the order the columns come in and the order the record
#: began: the bottom series starts ten years later.
SERIES = (
    Series("surface", "SURF_TEMP_C", "SURF_FLAG", "Surface"),
    Series("bottom", "BOT_TEMP_C", "BOT_FLAG", "Bottom"),
)

#: Pacific *Standard* Time, year round, as the header declares it. Deliberately
#: not a `ZoneInfo`: `America/Los_Angeles` would shift every summer reading an
#: hour, which on a daily series is indistinguishable from a diurnal signal.
PST_OFFSET = timedelta(hours=8)

#: What a day with no recorded time is placed at, in PST. The median time-of-day
#: of the 12,473 days that do carry one; the 2005-onward days, which have no DST
#: ambiguity at all, give 10:43, so the estimate is stable across both halves of
#: the timed record. docs/02 argues it against local midnight and local noon.
#:
#: It has to stay before 16:00: PST is -08:00, so any later local time would put
#: the reading on the following UTC day and move a 31 December sample into the
#: next quarter. Measured times do that legitimately; an assigned one must not.
NOMINAL_LOCAL_TIME = time(10, 38)

#: The docs/03 `qc_tests` name for the archive's own data flag.
SOURCE_FLAG_TEST = "source_flag"

#: The docs/03 `qc_tests` name for the archive's own time flag. Recorded *only*
#: where a time exists, which is what makes an imputed timestamp identifiable:
#: a row with no verdict here had no time to check.
SAMPLE_TIME_TEST = "sample_time"

#: The program's flag vocabulary mapped into docs/03, and the reason this module
#: has a translation table where the RTOMS one does not: these codes are the
#: Shore Stations Program's own, not QARTOD.
#:
#: 4 and 5 have never been written in a temperature file -- across all fourteen
#: snapshots and both series the only codes emitted are 0, 1, 2, 3 -- so those
#: two rows are rules for cases that have not arisen. 5 is `fail` rather than
#: `suspect` because it means the sample may have been taken *somewhere else*,
#: which makes it a real reading that is not a reading of this site: the same
#: thing docs/06 s3 does to a reading taken outside its deployment window, and
#: retained and excluded for the same reason.
FLAG_MEANING = {
    0: "good data",
    1: "illegible entry",
    2: "differs from other sources",
    3: "data uncertain",
    4: "leaky bottle",
    5: "Pier Chlorophyll Program or a different location",
}

STATUS_BY_SOURCE_FLAG = {
    0: FLAG_PASS,
    1: FLAG_SUSPECT,
    2: FLAG_SUSPECT,
    3: FLAG_SUSPECT,
    4: FLAG_SUSPECT,
    5: FLAG_FAIL,
}

#: The flag this module treats as "this reading is not this site's". Named so the
#: warning below and the mapping above cannot drift apart.
ELSEWHERE_FLAG = 5

#: How close the file's declared position has to be to the registry's, in degrees.
#: About 55 m of latitude -- tight enough that a different Shore Stations station
#: (the nearest others are tens of kilometres away) can never match, and loose
#: enough to absorb a rounding change in the DMS the header prints.
POSITION_TOLERANCE_DEG = 5e-4

#: How close the file's nominal depths have to be to the declared ones, in metres.
#: Exact to the precision the title line prints them at; `depth_m` is part of
#: `OBSERVATION_KEY` and is permanent, so this is not a place to be generous.
DEPTH_TOLERANCE_M = 1e-6

#: The docs/06 s5-style checks this source runs, and every one of them stops an
#: ingest. Named here rather than in the CLI so the module that knows what they
#: mean owns their spelling, the way `adapters.base` owns the HOBO gate's.
SITE_MATCH = "site_match"
ARCHIVE_PIN = "archive_pin"
SENSOR_DEPTHS = "sensor_depths"

#: In the order to report them. All three are blocking: an archive attributed to
#: the wrong station, landed under the wrong pin, or landed at a depth nobody has
#: reviewed is worse than one not landed at all -- and the last of the three is
#: not correctable afterwards, since `depth_m` is part of `OBSERVATION_KEY`.
QUARANTINE_CHECKS = (SITE_MATCH, ARCHIVE_PIN, SENSOR_DEPTHS)

_ARCHIVED = re.compile(r"archived\s+(\d{4}-\d{2}-\d{2})")
_AWARD = re.compile(r"Award#\s*([A-Za-z0-9]+)")
_DOI = re.compile(r"(10\.\d{4,9}/[^\s,]+)")
_STATION = re.compile(r"Shore Stations Program\s*-\s*(.+?)\s+Surface\s*\(")
#: Any non-digit run separates the fields, so the degree sign -- the one byte
#: that differs between the two encodings -- is never read.
_POSITION = re.compile(r"(\d+)\D+(\d+)'([\d.]+)\"*N\s+(\d+)\D+(\d+)'([\d.]+)\"*W")
#: `Surface (~0.5m)` / `Bottom (~5m)`, from the title line.
_DEPTH = r"{label}\s*\(~([\d.]+)\s*m\)"
#: `3 = data uncertain,` in the legend block.
_LEGEND_CODE = re.compile(r"^(\d+)\s*=\s*", re.MULTILINE)


@dataclass(frozen=True)
class ArchiveHeader:
    """What the preamble declares about the file below it.

    Everything the registry gate compares against, plus the provenance the run
    manifest should carry. Read rather than assumed, which is the whole point:
    this file names its own version, station, position and sensor depths, so
    none of them has to be trusted to a constant in this module.
    """

    path: Path
    archived: str
    station: str
    lat: float
    lon: float
    depths_m: dict[str, float]
    flag_codes: tuple[int, ...]
    award: str | None = None
    doi: str | None = None

    def depth_for(self, series: Series) -> float:
        return self.depths_m[series.name]

    @property
    def declared_depths(self) -> tuple[float, ...]:
        return tuple(self.depths_m[s.name] for s in SERIES)


def sniff(path: Path) -> bool:
    """Whether this file looks like a Shore Stations *temperature* archive.

    The column header and nothing else, found rather than counted to. There is
    no magic number and no filename convention worth trusting -- but there is a
    near-twin in the same download: the salinity file shares the preamble, the
    flag legend and seven of the nine columns, and differs exactly here.
    """
    try:
        _, header = _find_header(_decode(path.read_bytes()))
    except OSError:
        return False
    if header is None:
        return False
    fields = [field.strip() for field in header.split(",")]
    return tuple(fields[: len(COLUMNS)]) == COLUMNS


def read_header(path: Path) -> ArchiveHeader:
    """The preamble, parsed. Raises `ValueError` on anything it cannot read.

    Not lenient about a missing field. Each one is either compared against the
    registry or written into the record, so a preamble this cannot read is a
    format change to look at rather than a set of defaults to fall back on.
    """
    lines, header = _find_header(_decode(path.read_bytes()))
    if header is None:
        raise ValueError(
            f"{path}: no {HEADER_PREFIX!r} line; this is not a Shore Stations temperature "
            "archive, or its columns have changed and docs/02 needs updating first"
        )

    # Every preamble line is one quoted field followed by empty ones, and the
    # quoting is load-bearing: the title carries a comma and the position carries
    # doubled quote marks around its N and W.
    preamble = [row[0] if row else "" for row in csv.reader(lines[: lines.index(header)])]
    text = "\n".join(preamble)

    archived = _one(_ARCHIVED, text, path, "an `archived YYYY-MM-DD` line")
    station = _one(_STATION, _title(preamble, path), path, "a station name in the title line")
    lat, lon = _position(text, path)
    depths = {series.name: _depth(series, preamble, path) for series in SERIES}

    codes = sorted({int(code) for code in _LEGEND_CODE.findall(text)})
    if not codes:
        raise ValueError(
            f"{path}: the preamble declares no flag legend; it is what says which codes the "
            "flag columns can carry, and docs/02 maps every one of them by hand"
        )

    award = _AWARD.search(text)
    doi = _DOI.search(text)
    return ArchiveHeader(
        path=path,
        archived=archived,
        station=station,
        lat=lat,
        lon=lon,
        depths_m=depths,
        flag_codes=tuple(codes),
        award=award.group(1) if award else None,
        doi=doi.group(1) if doi else None,
    )


def parse(
    path: Path,
    parameters: Parameters,
    *,
    site_id: str,
    declared_depths: tuple[float, ...] = (),
    measured_parameters: tuple[str, ...] = (),
    run_id: str,
) -> ParsedPayload:
    """One archive -> docs/03 observation rows, UTC and SI, two series per day.

    Raises `ValueError` on a layout, a depth or a flag code this module has not
    verified, for the reason docs/02 gives: the honest answer is that we do not
    know what the numbers mean, and that belongs in front of a human rather than
    behind a default.

    `declared_depths` is the registry's record of which depths carry this
    parameter, and here it is a *check* rather than a filter -- the file names
    its own two depths and there is no third series to select against. An
    undeclared depth stops the parse rather than landing, because `depth_m` is
    part of `OBSERVATION_KEY` and a series landed at the wrong depth cannot be
    corrected by a later run (docs/03 "Partition files and idempotence"). An
    empty `declared_depths` means the registry has not recorded them, in which
    case the file's own depths are used and the gap is reported -- an unrecorded
    fact must not quietly become missing data, the same rule
    `measured_parameters` follows.
    """
    warnings: list[str] = []
    if measured_parameters and PARAMETER not in measured_parameters:
        return _nothing(
            path,
            layout="",
            rows_in=0,
            warnings=(
                (
                    f"{path.name}: the registry declares measured_parameters "
                    f"{sorted(measured_parameters)}, which does not include {PARAMETER!r}; "
                    "this archive carries nothing else this project stores"
                ),
            ),
        )
    if PARAMETER not in parameters:
        raise ValueError(
            f"{path}: {PARAMETER!r} is not in {parameters.path}; the controlled parameter "
            "has to exist before its rows can land"
        )

    header = read_header(path)
    _check_legend(header)
    warnings.extend(_check_depths(header, declared_depths))

    table = _read(path)
    rows_in = len(table)
    dates = _dates(table, path)
    local_times, timed = _local_times(table, path)

    parameter = parameters[PARAMETER]
    frames, missing_counts = [], {}
    for series in SERIES:
        frame, absent, elsewhere = _series_rows(
            table,
            series,
            header=header,
            dates=dates,
            local_times=local_times,
            timed=timed,
            parameter=parameter,
            site_id=site_id,
            run_id=run_id,
            path=path,
        )
        if frame.empty:
            continue
        frames.append(frame)
        missing_counts[f"{PARAMETER}@{header.depth_for(series):g}m"] = absent
        warnings.extend(elsewhere)

    if not frames:
        return _nothing(path, layout=header.archived, rows_in=rows_in, warnings=tuple(warnings))

    frame = pd.concat(frames, ignore_index=True)
    frame = frame.sort_values(["timestamp", "depth_m"], kind="stable").reset_index(drop=True)

    imputed = int((~timed).sum())
    if imputed:
        warnings.append(
            f"{imputed} of {len(table)} day(s) carry no time of day and were placed at "
            f"{NOMINAL_LOCAL_TIME:%H:%M} PST, the median of the days that do (docs/02). "
            f"They carry no {SAMPLE_TIME_TEST!r} verdict, which is how they are told apart "
            "from a measured timestamp"
        )

    return ParsedPayload(
        frame=frame[list(OBSERVATION_COLUMNS)],
        station=header.station,
        layout=header.archived,
        rows_in=rows_in,
        warnings=tuple(warnings),
        missing_counts=missing_counts,
    )


def _nothing(path: Path, *, layout: str, rows_in: int, warnings: tuple[str, ...]) -> ParsedPayload:
    """A well-formed archive that yielded no rows for this project."""
    return ParsedPayload(
        frame=empty_observations(),
        station=path.name,
        layout=layout,
        rows_in=rows_in,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Reading the preamble
# --------------------------------------------------------------------------


def _decode(raw: bytes) -> list[str]:
    """The archive as lines: UTF-8 if it decodes, latin-1 if it does not.

    Five of the fourteen snapshots are Mac Roman with no byte-order mark and the
    other nine are UTF-8 with one, so a strict decode loses a third of the
    collection. latin-1 cannot raise, which is what makes the fallback safe: the
    only non-ASCII byte in any snapshot is the degree sign of the position line,
    and the position is read with a pattern that never looks at it.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text.splitlines()


def _find_header(lines: list[str]) -> tuple[list[str], str | None]:
    """`(lines, the column header line)` -- or None if there is no such line.

    Found by prefix rather than by counting, because the preamble is 45 lines in
    five snapshots and 46 in nine.

    Deliberately does *not* check the value columns. The salinity twin in the
    same download shares this prefix and differs further along, and telling an
    operator "expected the archive columns [...], got [...]" is worth more than
    "no header line" -- so that check lives in one place, `_read`, and `sniff`
    reaches it through the same code rather than repeating it.
    """
    for line in lines:
        if line.startswith(HEADER_PREFIX):
            return lines, line
    return lines, None


def _title(preamble: list[str], path: Path) -> str:
    for line in preamble:
        if "Shore Stations Program -" in line:
            return line
    raise ValueError(
        f"{path}: no `Shore Stations Program - ...` title line in the preamble; it is where "
        "the station and both nominal depths are declared"
    )


def _one(pattern: re.Pattern[str], text: str, path: Path, what: str) -> str:
    found = pattern.search(text)
    if not found:
        raise ValueError(
            f"{path}: the preamble carries no {what}. docs/02 records it as present in every "
            "snapshot, so its absence is a format change to check rather than a value to guess"
        )
    return found.group(1).strip()


def _position(text: str, path: Path) -> tuple[float, float]:
    """The header's DMS position as WGS84 decimal degrees.

    Northern and western hemispheres only, and asserted rather than parsed
    generally: every station this program runs is on the California coast, and a
    file that reported otherwise would not be one of them.
    """
    found = _POSITION.search(text)
    if not found:
        raise ValueError(
            f"{path}: the preamble carries no `DD MM'SS.S\"N DDD MM'SS.S\"W` position. It is "
            "what says which station this file is, so it is not something to default"
        )
    d1, m1, s1, d2, m2, s2 = found.groups()
    lat = int(d1) + int(m1) / 60 + float(s1) / 3600
    lon = -(int(d2) + int(m2) / 60 + float(s2) / 3600)
    return lat, lon


def _depth(series: Series, preamble: list[str], path: Path) -> float:
    title = _title(preamble, path)
    found = re.search(_DEPTH.format(label=series.depth_label), title)
    if not found:
        raise ValueError(
            f"{path}: the title line does not declare a {series.depth_label} depth as "
            f"`{series.depth_label} (~Nm)`: {title!r}. depth_m is part of the storage key and "
            "permanent, so it is read from the file rather than assumed here"
        )
    return float(found.group(1))


# --------------------------------------------------------------------------
# What the registry has to agree with
# --------------------------------------------------------------------------


def _check_legend(header: ArchiveHeader) -> None:
    """Refuse a snapshot whose legend declares a code this module cannot map.

    The same rule the RTOMS parser applies when a provider's flag vocabulary
    changes, moved to the header because that is where this source declares it:
    a new code found at row 30,000 is a run that has already done its work.
    """
    unknown = sorted(set(header.flag_codes) - set(FLAG_MEANING))
    if unknown:
        raise ValueError(
            f"{header.path}: the flag legend declares code(s) {unknown}, which docs/02 does "
            f"not map; known: {sorted(FLAG_MEANING)}. A new code is a change to the program's "
            "own vocabulary and needs a decision before its rows can be read"
        )


def _undeclared_depths(header: ArchiveHeader, declared: tuple[float, ...]) -> list[str]:
    """Which of the file's series sit at a depth the registry has not reviewed.

    One comparison for two callers that do different things with the answer:
    `validate` turns it into a quarantine verdict for the manifest, and
    `_check_depths` raises for a caller that never asked for one. Sharing the
    predicate is what keeps them from drifting into disagreeing about which
    depths are acceptable -- which would show up as a file that passes the gate
    and then fails in the parser.
    """
    return [
        f"{series.name} {header.depth_for(series):g} m"
        for series in SERIES
        if not any(abs(header.depth_for(series) - d) <= DEPTH_TOLERANCE_M for d in declared)
    ]


def _listed(depths: tuple[float, ...]) -> str:
    return ", ".join(f"{depth:g}" for depth in depths) + " m"


def _check_depths(header: ArchiveHeader, declared: tuple[float, ...]) -> list[str]:
    """The file's two nominal depths against the ones the registry has reviewed.

    Refused rather than warned, unlike the RTOMS equivalent, and the difference
    is the shape of the source. A mooring can legitimately come back from a refit
    with a twelfth sensor, so an undeclared depth there is one series to leave
    out; here there are exactly two series and an undeclared depth means the file
    disagrees with the registry about where this station measures. Landing that
    would write a permanent `depth_m` nobody has reviewed.
    """
    if not declared:
        return [
            (
                "the site registry declares no depths for this parameter, so the file's own "
                f"{', '.join(f'{d:g} m' for d in header.declared_depths)} were used; record "
                "them in sensor_depths_m so a re-sounded depth is noticed rather than landed"
            )
        ]

    surprises = _undeclared_depths(header, declared)
    if surprises:
        raise ValueError(
            f"{header.path}: this archive reports {', '.join(surprises)}, which "
            f"sensor_depths_m does not declare ({_listed(declared)}). depth_m is part of the "
            "storage key and cannot be corrected once rows have landed, so review the change "
            "and update the registry first"
        )
    return []


def position_matches(header: ArchiveHeader, lat: float | None, lon: float | None) -> bool:
    """Whether a site record's position is the one this file declares.

    How a dropped archive is matched to a site. Unlike a Kelp Watch export --
    which says nothing at all about the geometry it describes, so the registry
    has to claim it by filename -- this file carries its own position, so the
    identification is evidence rather than a naming convention. A site with no
    position cannot be matched: an unsurveyed public station is not a thing this
    project has, and guessing would attach a century of readings to the wrong
    place.
    """
    if lat is None or lon is None:
        return False
    return (
        abs(float(lat) - header.lat) <= POSITION_TOLERANCE_DEG
        and abs(float(lon) - header.lon) <= POSITION_TOLERANCE_DEG
    )


# --------------------------------------------------------------------------
# Reading the rows
# --------------------------------------------------------------------------


def select_site(header: ArchiveHeader, stations) -> tuple[Station | None, Check]:
    """Which registered station this archive is, decided by its own position.

    The docs/06 s5 check-4 gate for this source: no site record, no ingest
    (hard rule 5). It is not the HOBO gate, because there is no serial and no
    deployment to match -- and it is not the Kelp Watch gate either, which has
    to claim an export by filename because a Kelp Watch export says nothing
    about the geometry it describes. This file names its own position, so the
    match is evidence rather than a naming convention.

    Ambiguity quarantines rather than picking one, for the reason
    `cli._select_deployment` does: two stations at one position is a registry
    error, and attaching a century of readings to whichever came first would
    hide it.
    """
    candidates = [site for site in stations if position_matches(header, site.lat, site.lon)]
    where = f"{header.lat:.6f}, {header.lon:.6f}"

    if not candidates:
        placed = ", ".join(
            f"{site.site_id} at {site.lat}, {site.lon}"
            for site in stations
            if site.lat is not None and site.lon is not None
        )
        return None, Check(
            SITE_MATCH,
            "fail",
            f"this archive declares position {where} and no {SOURCE} site in the registry "
            f"is there; registered: {placed or 'none with a position'} -- quarantine",
        )
    if len(candidates) > 1:
        listed = ", ".join(site.site_id for site in candidates)
        return None, Check(
            SITE_MATCH,
            "fail",
            f"position {where} matches {len(candidates)} site records ({listed}); two "
            "stations cannot be in one place, so this is a registry error -- quarantine",
        )

    site = candidates[0]
    return site, Check(
        SITE_MATCH,
        "pass",
        f"position {where} matches {site.site_id} ({site.name or site.station_code})",
    )


def validate(header: ArchiveHeader, site: Station) -> tuple[Check, ...]:
    """The archive pin and the declared depths, as verdicts for the manifest.

    Returns verdicts and does nothing about them. Moving a file into
    `data/quarantine/` is the ingest CLI's job (docs/03): one place decides what
    happens to a file, the way `adapters.base.registry_gate` is arranged.
    """
    return (_archive_check(header, site), _depth_check(header, site))


def _archive_check(header: ArchiveHeader, site: Station) -> Check:
    """The pin, checked against the archive date the file declares itself.

    An unpinned site cannot accept a file at all. Each download is a cumulative
    snapshot of the whole record, so a landing made without a pin could never be
    traced to a citable dataset afterwards and "whatever was on the site that
    day" would have become the source of record -- the reason
    `cli._ingest_kelpwatch` refuses without a revision, reached the same way.
    """
    if site.archive is None:
        return Check(
            ARCHIVE_PIN,
            "fail",
            f"{site.site_id} pins no archive.archived; each download is a cumulative "
            "snapshot of the whole record, so a landing without a pin could not be traced "
            f"to a citable dataset. This file declares {header.archived} -- quarantine",
        )
    if site.archive.archived != header.archived:
        return Check(
            ARCHIVE_PIN,
            "fail",
            f"this archive declares {header.archived} and {site.site_id} pins "
            f"{site.archive.archived}. Two snapshots of one cumulative record must not be "
            "read as one series; bump the pin deliberately, or drop the pinned file "
            "-- quarantine",
        )
    return Check(
        ARCHIVE_PIN,
        "pass",
        f"archive {header.archived} is the one {site.site_id} pins"
        + (f" (DOI {header.doi})" if header.doi else ""),
    )


def _depth_check(header: ArchiveHeader, site: Station) -> Check:
    """The file's two nominal depths against the reviewed set.

    A verdict here and a raise in `parse`, deliberately: this one decides the
    file's fate and is recorded in the manifest, and that one is the guard for a
    caller that never asked. `depth_m` is part of `OBSERVATION_KEY`, so a series
    landed at an unreviewed depth is permanent.
    """
    declared = site.declared_depths(PARAMETER)
    found = ", ".join(f"{series.name} {header.depth_for(series):g} m" for series in SERIES)

    if not declared:
        return Check(
            SENSOR_DEPTHS,
            "fail",
            f"{site.site_id} declares no sensor_depths_m for {PARAMETER!r} and this archive "
            f"reports {found}. depth_m is part of the storage key and cannot be corrected "
            "once rows have landed, so it is reviewed before the first landing rather than "
            "after -- quarantine",
        )

    surprises = _undeclared_depths(header, declared)
    if surprises:
        return Check(
            SENSOR_DEPTHS,
            "fail",
            f"this archive reports {', '.join(surprises)}, which {site.site_id} does not "
            f"declare ({_listed(declared)}). A re-sounded depth is a new series, permanently "
            "-- review it and update the registry first -- quarantine",
        )
    return Check(SENSOR_DEPTHS, "pass", f"the registry declares both depths this file has: {found}")


def _read(path: Path) -> pd.DataFrame:
    """Every cell as text, with pandas' own NA tokens left alone.

    `keep_default_na=False` is load-bearing rather than tidiness, for the reason
    the NDBC and Kelp Watch parsers give: pandas would otherwise convert a list
    of tokens of its own to NaN before this module sees them, making a token
    nobody verified indistinguishable from the `NaN` this archive actually
    writes -- which is the only thing separating a missing reading from a
    measured one here.
    """
    lines, header = _find_header(_decode(path.read_bytes()))
    if header is None:
        raise ValueError(f"{path}: no {HEADER_PREFIX!r} line")

    body = lines[lines.index(header) + 1 :]
    table = pd.read_csv(
        io.StringIO("\n".join([header, *body])),
        dtype=str,
        keep_default_na=False,
    )
    table.columns = [str(name).strip() for name in table.columns]

    if tuple(table.columns[: len(COLUMNS)]) != COLUMNS:
        raise ValueError(
            f"{path}: expected the archive columns {list(COLUMNS)}, got "
            f"{list(table.columns[: len(COLUMNS)])}. This is a layout docs/02 has not "
            "recorded -- do not store it until the new column has been checked."
        )

    # Every snapshot carries unnamed trailing columns -- five in a temperature
    # file, two in the salinity twin -- and every one of them is empty in every
    # row. Tolerated for that reason and only that reason: a non-empty one is a
    # column this parser is silently discarding.
    extra = list(table.columns[len(COLUMNS) :])
    populated = [name for name in extra if table[name].str.strip().any()]
    if populated:
        raise ValueError(
            f"{path}: trailing column(s) {populated} carry values. They are empty in every "
            "row of every recorded snapshot, so this is data arriving in a column docs/02 "
            "has not recorded rather than padding to ignore."
        )

    # Filler rows: five of the fourteen snapshots end with between 30 and 119
    # rows that are nothing but commas. A row with no date at all is padding; a
    # row with *part* of a date is a format surprise and is left to `_dates`.
    keyed = table[list(COLUMNS[:3])].apply(lambda column: column.str.strip())
    return table.loc[keyed.ne("").any(axis=1)].reset_index(drop=True)


def _dates(table: pd.DataFrame, path: Path) -> pd.Series:
    """The calendar day of each row. Refuses a half-date or a repeated day."""
    parts = {
        name.lower(): pd.to_numeric(table[name], errors="coerce")
        for name in ("YEAR", "MONTH", "DAY")
    }
    dates = pd.to_datetime(parts, errors="coerce")

    unreadable = table.loc[dates.isna(), list(COLUMNS[:3])]
    if len(unreadable):
        shown = unreadable.head(3).to_dict("records")
        raise ValueError(
            f"{path}: {len(unreadable)} row(s) carry a date this parser cannot read; first: "
            f"{shown}. A row with no date at all is padding and was already dropped, so what "
            "is left is a date that is partly there -- a format change, not a blank."
        )

    repeated = dates[dates.duplicated()]
    if len(repeated):
        labels = [d.date().isoformat() for d in repeated.head(5)]
        raise ValueError(
            f"{path}: {len(repeated)} day(s) appear more than once; first: {labels}. This "
            "archive is one row per calendar day, and a repeated day is two readings this "
            "parser cannot tell apart."
        )
    return dates


def _local_times(table: pd.DataFrame, path: Path) -> tuple[pd.Series, pd.Series]:
    """`(local timestamp, whether the file supplied its time)`, both PST-naive.

    `TIME_PST` is `HHMM` as an integer with no leading zero -- `858` is 08:58 and
    midnight is `0` -- so it is arithmetic on the number rather than string
    slicing, which would read `858` as 85:8.
    """
    raw = pd.to_numeric(table["TIME_PST"].str.strip(), errors="coerce")
    timed = raw.notna()

    minutes = raw.fillna(0).astype("int64")
    hours, remainder = minutes // 100, minutes % 100
    invalid = timed & ((hours > 23) | (remainder > 59) | (minutes < 0))
    if bool(invalid.any()):
        shown = sorted(table.loc[invalid, "TIME_PST"].head(5))
        raise ValueError(
            f"{path}: {int(invalid.sum())} row(s) carry a TIME_PST that is not an HHMM time; "
            f"first: {shown}. docs/02 records this column as HHMM without a leading zero."
        )

    offsets = pd.to_timedelta(
        np.where(
            timed,
            hours * 3600 + remainder * 60,
            NOMINAL_LOCAL_TIME.hour * 3600 + NOMINAL_LOCAL_TIME.minute * 60,
        ),
        unit="s",
    )
    return pd.Series(offsets, index=table.index), timed


def _series_rows(
    table: pd.DataFrame,
    series: Series,
    *,
    header: ArchiveHeader,
    dates: pd.Series,
    local_times: pd.Series,
    timed: pd.Series,
    parameter,
    site_id: str,
    run_id: str,
    path: Path,
) -> tuple[pd.DataFrame, int, list[str]]:
    """One depth's observation rows, plus its absent count and its warnings."""
    values = pd.to_numeric(table[series.value_column].str.strip(), errors="coerce")
    flags = _source_flags(table[series.flag_column], series, path)

    # A day with neither a reading nor a flag is a day before this series began:
    # the source leaves both columns empty for exactly the 3,620 days before the
    # bottom series starts and for no others (docs/02). Dropped rather than
    # landed flagged missing, because an outage is a sample nobody took and this
    # is a series that did not exist. A reading with no flag beside it -- which
    # the archive has never written -- is kept, at "not evaluated".
    started = values.notna() | flags.notna()
    if not bool(started.any()):
        return pd.DataFrame(), 0, []

    kept = started.to_numpy()
    absent = values[kept].isna().to_numpy()

    # An absent reading is `missing` whatever the flag column says. The archive
    # writes 0 -- good data -- beside 1,330 absent surface readings and 2,256
    # absent bottom ones; docs/03 gives 9 to a row with no value, and there is
    # nothing in an absence to judge. Recorded as a verdict rather than patched
    # onto the flag afterwards, so the roll-up and the record agree.
    verdict = flags[kept].map(STATUS_BY_SOURCE_FLAG).to_numpy(dtype="float64")
    verdict = np.where(np.isnan(verdict), FLAG_NOT_EVALUATED, verdict)
    verdict = np.where(absent, FLAG_MISSING, verdict).astype("int8")

    sample_time = _time_flags(table["TIME_FLAG"], timed, path)[kept]

    qc_flag, qc_tests = summarize(
        {SOURCE_FLAG_TEST: verdict, SAMPLE_TIME_TEST: sample_time},
        rows=int(kept.sum()),
    )

    local = (dates[kept] + local_times[kept]).to_numpy()
    frame = pd.DataFrame(
        {
            "timestamp": (pd.DatetimeIndex(local) + PST_OFFSET).tz_localize("UTC"),
            "site_id": site_id,
            "parameter": parameter.name,
            "value": convert_unit(values[kept], COLUMN_UNIT, parameter.unit).to_numpy(
                dtype="float64"
            ),
            "depth_m": header.depth_for(series),
            "qc_flag": qc_flag,
            "qc_tests": qc_tests,
            "source": SOURCE,
            "fetch_run_id": run_id,
        }
    )
    return frame, int(absent.sum()), _elsewhere(flags[kept], dates[kept], series, header)


def _source_flags(column: pd.Series, series: Series, path: Path) -> pd.Series:
    """One flag column as nullable integers, refusing a code the legend did not
    declare.

    `_check_legend` catches a vocabulary change announced in the preamble; this
    catches one that arrives in the data without being announced, which is the
    worse of the two because nothing in the file admits to it.
    """
    flags = pd.to_numeric(column.str.strip(), errors="coerce")
    unknown = sorted({int(f) for f in flags.dropna().unique() if int(f) not in FLAG_MEANING})
    if unknown:
        raise ValueError(
            f"{path}: {series.flag_column} carries code(s) {unknown}, which the file's own "
            f"legend does not declare and docs/02 does not map; known: "
            f"{sorted(FLAG_MEANING)}. Do not store the file until it has been checked."
        )
    return flags


def _time_flags(column: pd.Series, timed: pd.Series, path: Path) -> np.ndarray:
    """`TIME_FLAG` as docs/03 verdicts -- recorded only where a time exists.

    Both halves matter. Where a time exists this is the source telling us
    whether it is legible, and a disputed time makes the observation suspect
    because an observation is a reading *and* a time. Where no time exists there
    is nothing to judge, so no verdict is recorded -- and that absence is what
    identifies an imputed timestamp afterwards.

    The archive is no help in telling them apart on its own: all 766 post-1990
    days with no `TIME_PST` still carry `TIME_FLAG = 0`, "good data", about a
    time that is not there. So `timed` decides, and it comes from the time
    column.
    """
    flags = pd.to_numeric(column.str.strip(), errors="coerce")
    unknown = sorted({int(f) for f in flags.dropna().unique() if int(f) not in FLAG_MEANING})
    if unknown:
        raise ValueError(
            f"{path}: TIME_FLAG carries code(s) {unknown}, which docs/02 does not map; "
            f"known: {sorted(FLAG_MEANING)}"
        )

    verdict = flags.map(STATUS_BY_SOURCE_FLAG).to_numpy(dtype="float64")
    verdict = np.where(np.isnan(verdict) | ~timed.to_numpy(), FLAG_NOT_EVALUATED, verdict)
    return verdict.astype("int8")


def _elsewhere(flags: pd.Series, dates: pd.Series, series: Series, header: ArchiveHeader):
    """Say so, by date, the first time a flag-5 reading is ever landed.

    Zero rows in all fourteen recorded snapshots, so this warning has never
    fired. That is exactly why it exists: the mapping is a decision taken in
    docs/02 about a case that has not arisen, and the run that first exercises
    it should put the decision in front of the operator rather than apply it
    silently.
    """
    hits = dates[flags.eq(ELSEWHERE_FLAG).to_numpy()]
    if not len(hits):
        return []
    labels = ", ".join(d.date().isoformat() for d in hits.head(5))
    more = "" if len(hits) <= 5 else f" (+{len(hits) - 5} more)"
    return [
        (
            f"{len(hits)} {series.name} reading(s) carry source flag {ELSEWHERE_FLAG} "
            f"({FLAG_MEANING[ELSEWHERE_FLAG]}): {labels}{more}. docs/02 lands these at "
            f"qc_flag {FLAG_FAIL} -- on the record, out of the default filter -- because "
            "the code cannot distinguish a sample taken here for another program from one "
            "taken somewhere else. First time this has ever fired; review it. Archive "
            f"{header.archived}"
        )
    ]


__all__ = [
    "ARCHIVE_PIN",
    "COLUMNS",
    "FETCHER_NAME",
    "FLAG_MEANING",
    "NOMINAL_LOCAL_TIME",
    "PARAMETER",
    "QUARANTINE_CHECKS",
    "SAMPLE_TIME_TEST",
    "SENSOR_DEPTHS",
    "SERIES",
    "SITE_MATCH",
    "SOURCE",
    "SOURCE_FLAG_TEST",
    "STATUS_BY_SOURCE_FLAG",
    "ArchiveHeader",
    "Series",
    "parse",
    "position_matches",
    "read_header",
    "select_site",
    "sniff",
    "validate",
]
