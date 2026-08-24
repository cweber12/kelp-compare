"""What HOBO words mean: headers, `Details`, `Events`, provenance, and the checks.

All the HOBOconnect *semantics* live here, deliberately separated from the
*container* they arrive in. `hobo_xlsx` loads three sheets out of a workbook and
hands them here as `HoboSheets`; a future `hobo_csv` loads the same logical
content out of a different file and hands over the same object (docs/06 s6).

Everything below was verified against the two reference files in
`tests/fixtures/`, not inferred from the vendor's documentation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from kelpcompare.adapters.base import (
    DATA_COLUMNS,
    Check,
    Provenance,
    RawSeries,
    SeriesInfo,
    registry_gate,
)
from kelpcompare.registry import Deployment, Registry, find_deployment

DATA_SHEET = "Data"
EVENTS_SHEET = "Events"
DETAILS_SHEET = "Details"
REQUIRED_SHEETS = (DATA_SHEET, EVENTS_SHEET, DETAILS_SHEET)

COUNTER_COLUMN = "#"
STARTED_EVENT = "Started"
END_OF_FILE_EVENT = "End of File"

#: `Date-Time (PDT)` -- the timezone lives in the header text and nowhere else.
TZ_HEADER_RE = re.compile(r"^Date-Time\s*\((?P<tz>[^)]+)\)\s*$")
#: `Tidbit 1 , degF` -- the sensor name and unit both live in the header text.
SERIES_HEADER_RE = re.compile(r"^(?P<name>.+?)\s*,\s*(?P<unit>[^,]+?)\s*$")
#: `Series : Tidbit 1 , degF` -- section header joining Details stats to a column.
DETAILS_SERIES_RE = re.compile(r"^Series\s*:\s*(?P<column>.+?)\s*$")
#: `0 hour 10 minutes 0 seconds`
INTERVAL_RE = re.compile(r"(\d+)\s*(hour|minute|second)s?", re.IGNORECASE)
#: pandas' placeholder for a blank header cell.
UNNAMED_RE = re.compile(r"^Unnamed:\s*\d+$")

_INTERVAL_UNITS = {"hour": 3600, "minute": 60, "second": 1}


@dataclass(frozen=True)
class HoboSheets:
    """The loader-agnostic input: three sheets plus where the formulas were.

    `formula_cells` is the one thing a values-only read cannot recover, and it is
    a provenance signal (a hand-added `=min(C:C)` helper column), so the loader
    is asked for it explicitly.
    """

    path: Path
    data: pd.DataFrame
    events: pd.DataFrame
    details: pd.DataFrame
    formula_cells: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataColumns:
    """The `Data` sheet's columns, sorted into the roles the format defines."""

    counter: str | None = None
    timestamp: str | None = None
    tz_token: str | None = None
    series: tuple[tuple[str, str, str], ...] = ()  # (column, name, unit)
    extra: tuple[str, ...] = ()


@dataclass(frozen=True)
class HoboDetails:
    """The `Details` sheet: a 4-column hierarchy (section / group / key / value)."""

    device: dict[str, str] = field(default_factory=dict)
    series: dict[str, dict[str, str]] = field(default_factory=dict)
    groups: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def serial(self) -> str | None:
        return self.device.get("Serial Number")

    @property
    def interval_seconds(self) -> int | None:
        return parse_interval_seconds(self.device.get("Logging Interval"))


@dataclass(frozen=True)
class HoboEvent:
    """One deployment-lifecycle row: `Host Connected`, `Started`, `End of File`."""

    event: str
    timestamp: datetime | None
    marker: str | None = None


# --------------------------------------------------------------------------
# Header parsing
# --------------------------------------------------------------------------


def find_timestamp_column(columns) -> tuple[str | None, str | None]:
    """Locate the `Date-Time (TZ)` column and return it with its timezone token."""
    for column in columns:
        match = TZ_HEADER_RE.match(str(column).strip())
        if match:
            return str(column), match.group("tz").strip()
    return None, None


def classify_columns(columns) -> DataColumns:
    """Sort `Data` columns into counter / timestamp / series / extra.

    Order matters: the timestamp column is claimed before the series pattern is
    tried, so a timezone token containing a comma could never be mistaken for a
    series. Anything left over is `extra` -- a provenance signal, not an error.
    """
    timestamp, tz_token = find_timestamp_column(columns)
    counter: str | None = None
    series: list[tuple[str, str, str]] = []
    extra: list[str] = []

    for column in columns:
        label = str(column).strip()
        if label == COUNTER_COLUMN:
            counter = str(column)
            continue
        if str(column) == timestamp:
            continue
        match = SERIES_HEADER_RE.match(label)
        if match and not UNNAMED_RE.match(label):
            series.append((str(column), match.group("name").strip(), match.group("unit").strip()))
            continue
        extra.append(str(column))

    return DataColumns(
        counter=counter,
        timestamp=timestamp,
        tz_token=tz_token,
        series=tuple(series),
        extra=tuple(extra),
    )


def parse_interval_seconds(text: str | None) -> int | None:
    """`"0 hour 10 minutes 0 seconds"` -> `600`."""
    if not text:
        return None
    matches = INTERVAL_RE.findall(str(text))
    if not matches:
        return None
    return sum(int(value) * _INTERVAL_UNITS[unit.lower()] for value, unit in matches)


def parse_filename(path: Path) -> dict[str, str | None]:
    """Split the `{name}__{serial}__{readout}` filename convention.

    Returns all-None for a renamed file (`yellow_buoy_temps.xlsx`); the `Details`
    sheet is authoritative for the serial either way.
    """
    parts = Path(path).stem.split("__")
    if len(parts) < 3:
        return {"name": None, "serial": None, "readout": None}
    return {"name": parts[0], "serial": parts[1], "readout": "__".join(parts[2:])}


# --------------------------------------------------------------------------
# Sheet parsing
# --------------------------------------------------------------------------


def parse_details(details: pd.DataFrame) -> HoboDetails:
    """Walk the `Details` hierarchy: column A section, B group, C key, D value."""
    device: dict[str, str] = {}
    series: dict[str, dict[str, str]] = {}
    groups: dict[str, dict[str, str]] = {}
    section: str | None = None
    group: str | None = None

    for row in details.itertuples(index=False, name=None):
        cells = [_clean(value) for value in row] + [None] * 4
        a, b, key, value = cells[0], cells[1], cells[2], cells[3]
        if a is not None:
            section, group = a, None
        if b is not None:
            group = b
        if key is None:
            continue

        groups.setdefault(group or "", {})[key] = value or ""
        series_match = DETAILS_SERIES_RE.match(section or "")
        if series_match:
            series.setdefault(series_match.group("column"), {})[key] = value or ""
        else:
            device[key] = value or ""

    return HoboDetails(device=device, series=series, groups=groups)


def parse_events(events: pd.DataFrame) -> tuple[HoboEvent, ...]:
    """Read the lifecycle rows.

    The event type is whichever non-counter, non-timestamp column holds a marker
    on that row, and the type names come from the sheet rather than a hardcoded
    list -- so an event this project has not seen yet still parses.
    """
    timestamp_column, _ = find_timestamp_column(events.columns)
    if timestamp_column is None:
        return ()

    event_columns = [
        column
        for column in events.columns
        if str(column).strip() != COUNTER_COLUMN
        and str(column) != timestamp_column
        and not UNNAMED_RE.match(str(column).strip())
    ]
    timestamps = pd.to_datetime(events[timestamp_column], errors="coerce")

    parsed: list[HoboEvent] = []
    for position, timestamp in enumerate(timestamps):
        for column in event_columns:
            marker = _clean(events[column].iloc[position])
            if marker is not None:
                parsed.append(
                    HoboEvent(
                        event=str(column).strip(),
                        timestamp=None if pd.isna(timestamp) else timestamp.to_pydatetime(),
                        marker=marker,
                    )
                )
    return tuple(parsed)


def first_event(events: tuple[HoboEvent, ...], name: str) -> HoboEvent | None:
    for event in events:
        if event.event == name:
            return event
    return None


# --------------------------------------------------------------------------
# Provenance and assembly
# --------------------------------------------------------------------------


def detect_provenance(
    sheets: HoboSheets, columns: DataColumns, measurements: pd.DataFrame
) -> tuple[Provenance, tuple[str, ...]]:
    """Decide `original` vs `edited` from file *structure* only.

    Deliberately not from the statistics. Marking a file `edited` because its
    numbers disagree with `Details` would make the docs/06 s5 check-1 cross-check
    circular -- it could never fail, and a genuinely truncated or corrupted
    export (the thing that check exists to catch) would be quietly excused as a
    hand edit.
    """
    signals: list[str] = []

    for column in columns.extra:
        signals.append(f"unexpected column {column!r} in the {DATA_SHEET} sheet")

    orphans = len(sheets.data) - len(measurements)
    if orphans > 0:
        signals.append(f"{orphans} row(s) in {DATA_SHEET} without a valid Date-Time")

    if sheets.formula_cells:
        listed = ", ".join(sheets.formula_cells[:5])
        suffix = ", ..." if len(sheets.formula_cells) > 5 else ""
        signals.append(f"formula cell(s) in {DATA_SHEET}: {listed}{suffix}")

    counter_signal = _counter_signal(columns, measurements)
    if counter_signal:
        signals.append(counter_signal)

    return ("edited" if signals else "original"), tuple(signals)


def build_raw_series(sheets: HoboSheets) -> RawSeries:
    """Assemble the long-format measurements. Extraction only -- no conversion."""
    columns = classify_columns(sheets.data.columns)
    if columns.timestamp is None:
        raise ValueError(f"{sheets.path}: no 'Date-Time (TZ)' column in the {DATA_SHEET} sheet")
    if not columns.series:
        raise ValueError(f"{sheets.path}: no '{{name}} , {{unit}}' series column in {DATA_SHEET}")

    timestamps = pd.to_datetime(sheets.data[columns.timestamp], errors="coerce")
    # docs/06 s4: only rows with a valid datetime are measurements.
    measurements = sheets.data.loc[timestamps.notna()]
    timestamps = timestamps.loc[measurements.index]

    provenance, signals = detect_provenance(sheets, columns, measurements)

    frames: list[pd.DataFrame] = []
    infos: list[SeriesInfo] = []
    for column, name, unit in columns.series:
        values = pd.to_numeric(measurements[column], errors="coerce")
        frames.append(
            pd.DataFrame(
                {
                    "row_number": _row_numbers(columns, measurements),
                    "timestamp_local": timestamps.to_numpy(),
                    "series_name": name,
                    "unit": unit,
                    "value": values.to_numpy(dtype="float64"),
                }
            )
        )
        infos.append(
            SeriesInfo(
                name=name,
                unit=unit,
                column=column,
                n=len(values),
                first=_as_datetime(timestamps.min()),
                last=_as_datetime(timestamps.max()),
                minimum=_as_float(values.min()),
                maximum=_as_float(values.max()),
                mean=_as_float(values.mean()),
            )
        )

    data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=DATA_COLUMNS)
    return RawSeries(
        path=sheets.path,
        provenance=provenance,
        edit_signals=signals,
        tz_token=columns.tz_token,
        data=data[list(DATA_COLUMNS)],
        series=tuple(infos),
    )


def read_metadata(sheets: HoboSheets, raw: RawSeries) -> dict:
    """Serial, model, interval, events, and the export-time statistics (docs/06 s4)."""
    details = parse_details(sheets.details)
    events = parse_events(sheets.events)
    return {
        "path": str(sheets.path),
        "filename": parse_filename(sheets.path),
        "provenance": raw.provenance,
        "edit_signals": list(raw.edit_signals),
        "tz_token": raw.tz_token,
        "app_name": details.device.get("App Name"),
        "app_version": details.device.get("Version"),
        "product": details.device.get("Product"),
        "serial": details.serial,
        "firmware_version": details.device.get("Firmware Version"),
        "manufacturer": details.device.get("Manufacturer"),
        "deployment_name": details.device.get("Name"),
        "deployment_number": _as_int(details.device.get("Deployment Number")),
        "configure_date": details.device.get("Configure Date"),
        "start_logging": details.device.get("Start Logging"),
        "logging_interval_text": details.device.get("Logging Interval"),
        "logging_interval_seconds": details.interval_seconds,
        "logging_mode": details.device.get("Logging Mode"),
        "events": [
            {
                "event": event.event,
                "timestamp": event.timestamp.isoformat() if event.timestamp else None,
                "marker": event.marker,
            }
            for event in events
        ],
        "series": [
            {
                "name": info.name,
                "unit": info.unit,
                "column": info.column,
                "n": info.n,
                "details_statistics": details.series.get(info.column, {}),
            }
            for info in raw.series
        ],
    }


# --------------------------------------------------------------------------
# Validation checks (docs/06 s5)
# --------------------------------------------------------------------------


def check_details_statistics(raw: RawSeries, details: HoboDetails) -> Check:
    """Check 1: parsed n/min/max/mean must match the `Details` statistics."""
    if raw.provenance == "edited":
        return Check(
            "details_statistics",
            "skipped",
            "skipped for a hand-edited file (docs/06 s3): Details reports the pre-edit "
            f"statistics, so this check fails by construction. Signals: {'; '.join(raw.edit_signals)}",
        )

    problems: list[str] = []
    summaries: list[str] = []
    for info in raw.series:
        reported = details.series.get(info.column)
        if not reported:
            problems.append(f"{info.column!r}: no 'Series :' section in {DETAILS_SHEET}")
            continue
        for label, parsed, key in (
            ("n", info.n, "Samples"),
            ("min", info.minimum, "Min"),
            ("max", info.maximum, "Max"),
            ("mean", info.mean, "Avg"),
        ):
            if not _matches(parsed, reported.get(key)):
                problems.append(
                    f"{info.column!r}: {label}={_fmt(parsed)} but {DETAILS_SHEET} "
                    f"reports {key}={reported.get(key)!r}"
                )
        summaries.append(
            f"{info.column!r} n={info.n} min={_fmt(info.minimum)} "
            f"max={_fmt(info.maximum)} mean={_fmt(info.mean)}"
        )

    if problems:
        return Check("details_statistics", "fail", "; ".join(problems))
    return Check(
        "details_statistics",
        "pass",
        f"n/min/max/mean match the {DETAILS_SHEET} statistics for "
        f"{len(raw.series)} series: {'; '.join(summaries)}",
    )


def check_events_consistency(
    raw: RawSeries, details: HoboDetails, events: tuple[HoboEvent, ...]
) -> Check:
    """Check 2: first/last sample match `Started`/`End of File`; interval matches."""
    if raw.provenance == "edited":
        return Check(
            "events_consistency",
            "skipped",
            "skipped for a hand-edited file (docs/06 s3): rows were trimmed, so the "
            "first and last samples no longer correspond to the logged events",
        )

    problems: list[str] = []
    started = first_event(events, STARTED_EVENT)
    end_of_file = first_event(events, END_OF_FILE_EVENT)
    first = min((info.first for info in raw.series if info.first), default=None)
    last = max((info.last for info in raw.series if info.last), default=None)

    if started is None or started.timestamp is None:
        problems.append(f"no {STARTED_EVENT!r} event in {EVENTS_SHEET}")
    elif first != started.timestamp:
        problems.append(f"first sample {first} != {STARTED_EVENT} {started.timestamp}")

    if end_of_file is None or end_of_file.timestamp is None:
        problems.append(f"no {END_OF_FILE_EVENT!r} event in {EVENTS_SHEET}")
    elif last != end_of_file.timestamp:
        problems.append(f"last sample {last} != {END_OF_FILE_EVENT} {end_of_file.timestamp}")

    configured = details.interval_seconds
    observed = _observed_intervals(raw)
    if configured is None:
        problems.append(f"no readable 'Logging Interval' in {DETAILS_SHEET}")
    elif observed and observed != {float(configured)}:
        problems.append(
            f"configured interval {configured}s but observed spacings {sorted(observed)}"
        )

    if problems:
        return Check("events_consistency", "fail", "; ".join(problems))
    return Check(
        "events_consistency",
        "pass",
        f"first sample == {STARTED_EVENT} ({first}), last == {END_OF_FILE_EVENT} ({last}), "
        f"spacing == configured interval ({configured}s)",
    )


def check_cadence(raw: RawSeries, interval_seconds: int | None) -> Check:
    """Check 3: audit spacing. Runs on every file, including hand-edited ones.

    An audit, not a gate -- irregular spacing is a clock-drift symptom worth
    reporting (docs/02), not grounds for refusing the file.
    """
    reports: list[str] = []
    irregular = False

    for info in raw.series:
        stamps = raw.series_frame(info.name)["timestamp_local"]
        if len(stamps) < 2:
            irregular = True
            reports.append(f"{info.name!r}: {len(stamps)} sample(s), cadence not assessable")
            continue

        spacings = stamps.diff().dropna().dt.total_seconds()
        modal = float(spacings.mode().iloc[0])
        expected = float(interval_seconds) if interval_seconds else modal
        deviations = spacings[spacings != expected]
        gaps = spacings[spacings > expected]

        if deviations.empty:
            reports.append(f"{info.name!r}: {len(spacings)} intervals, all {expected:g}s, no gaps")
            continue

        irregular = True
        worst = stamps.iloc[deviations.index[:3]].dt.strftime("%Y-%m-%d %H:%M").tolist()
        reports.append(
            f"{info.name!r}: {len(deviations)} of {len(spacings)} intervals deviate from "
            f"{expected:g}s (modal {modal:g}s, {len(gaps)} gap(s)); first at {', '.join(worst)}"
        )

    return Check("cadence_audit", "warn" if irregular else "pass", "; ".join(reports))


def check_timezone(raw: RawSeries, deployment: Deployment | None) -> Check:
    """Cross-check the header timezone token against the registry zone.

    Warn-only by design. docs/06 s6 records that HOBOconnect's behaviour across
    the November DST transition is unverified, so a deployment spanning it is
    flagged for a human rather than silently resolved here.
    """
    if raw.tz_token is None:
        return Check("timezone_crosscheck", "warn", "no timezone token in the Date-Time header")
    if deployment is None or not deployment.tz:
        return Check(
            "timezone_crosscheck",
            "skipped",
            f"header token {raw.tz_token!r}; no registry timezone to compare (see registry_gate)",
        )

    first = min((info.first for info in raw.series if info.first), default=None)
    last = max((info.last for info in raw.series if info.last), default=None)
    at_start = _zone_abbreviation(first, deployment.tz)
    at_end = _zone_abbreviation(last, deployment.tz)

    if at_start is None or at_end is None:
        return Check(
            "timezone_crosscheck",
            "warn",
            f"could not resolve registry timezone {deployment.tz!r} to compare with "
            f"header token {raw.tz_token!r}",
        )
    if at_start != at_end:
        return Check(
            "timezone_crosscheck",
            "warn",
            f"deployment spans a DST transition ({at_start} -> {at_end} in {deployment.tz}) "
            f"but the header carries the single token {raw.tz_token!r}; HOBOconnect's "
            "behaviour here is unverified (docs/06 s6) -- flag for manual review",
        )
    if at_start != raw.tz_token:
        return Check(
            "timezone_crosscheck",
            "warn",
            f"header token {raw.tz_token!r} != {at_start} in registry timezone {deployment.tz}",
        )
    return Check(
        "timezone_crosscheck",
        "pass",
        f"header token {raw.tz_token!r} agrees with {deployment.tz} over the whole span",
    )


def check_filename_serial(sheets: HoboSheets, details: HoboDetails) -> Check:
    """Cross-check the filename serial against `Details`, which is authoritative."""
    from_name = parse_filename(sheets.path)["serial"]
    from_details = details.serial

    if from_name is None:
        return Check(
            "filename_serial",
            "pass",
            f"filename does not encode a serial; using {DETAILS_SHEET} ({from_details})",
        )
    if from_details and str(from_name).strip() != str(from_details).strip():
        return Check(
            "filename_serial",
            "warn",
            f"filename serial {from_name!r} != {DETAILS_SHEET} serial {from_details!r}; "
            f"{DETAILS_SHEET} wins",
        )
    return Check(
        "filename_serial", "pass", f"filename serial {from_name!r} agrees with {DETAILS_SHEET}"
    )


def run_checks(sheets: HoboSheets, raw: RawSeries, registry: Registry) -> tuple[Check, ...]:
    """All of docs/06 s5 for one file, in the order the doc lists them."""
    details = parse_details(sheets.details)
    events = parse_events(sheets.events)
    deployment = find_deployment(registry, details.serial or "")
    return (
        check_details_statistics(raw, details),
        check_events_consistency(raw, details, events),
        check_cadence(raw, details.interval_seconds),
        check_timezone(raw, deployment),
        check_filename_serial(sheets, details),
        registry_gate(details.serial, registry),
    )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _clean(value) -> str | None:
    """Blank-ish cell -> None; anything else -> a stripped string."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return None
    text = str(value).strip()
    return text or None


def _row_numbers(columns: DataColumns, measurements: pd.DataFrame):
    """The `#` counter as a nullable integer -- it may be float or blank."""
    if columns.counter is None:
        return pd.array([pd.NA] * len(measurements), dtype="Int64")
    numeric = pd.to_numeric(measurements[columns.counter], errors="coerce")
    return numeric.astype("Int64").array


def _counter_signal(columns: DataColumns, measurements: pd.DataFrame) -> str | None:
    """A legitimate export counts its rows 1..n. Anything else is an edit signal."""
    if columns.counter is None:
        return f"no {COUNTER_COLUMN!r} counter column in {DATA_SHEET}"
    numeric = pd.to_numeric(measurements[columns.counter], errors="coerce")
    if numeric.isna().any():
        return f"{COUNTER_COLUMN!r} has missing or non-numeric values on measurement rows"

    values = numeric.to_numpy(dtype="float64")
    if len(values) == 0:
        return None
    if not np.array_equal(values, np.round(values)):
        return f"{COUNTER_COLUMN!r} has non-integer values"
    if not np.array_equal(values, np.arange(1, len(values) + 1, dtype="float64")):
        return (
            f"{COUNTER_COLUMN!r} is not a contiguous run from 1 "
            f"(observed {int(values[0])}..{int(values[-1])} over {len(values)} rows)"
        )
    return None


def _observed_intervals(raw: RawSeries) -> set[float]:
    observed: set[float] = set()
    for info in raw.series:
        stamps = raw.series_frame(info.name)["timestamp_local"]
        if len(stamps) > 1:
            observed.update(stamps.diff().dropna().dt.total_seconds().unique().tolist())
    return observed


def _zone_abbreviation(moment: datetime | None, tz: str) -> str | None:
    """`PDT`/`PST` for a naive local timestamp in `tz`, or None if `tz` is unknown."""
    if moment is None:
        return None
    try:
        localized = pd.Timestamp(moment).tz_localize(
            tz, ambiguous=True, nonexistent="shift_forward"
        )
    except (LookupError, ValueError, TypeError):
        # An unknown zone name in the registry is a data problem to report, not
        # a crash: the caller turns None into a warning.
        return None
    return localized.tzname()


def _matches(parsed, reported: str | None, places: int = 2) -> bool:
    """Compare against a `Details` value at the precision `Details` reports it."""
    if parsed is None or reported is None:
        return False
    try:
        expected = float(str(reported).strip())
    except ValueError:
        return False
    return round(float(parsed), places) == round(expected, places)


def _fmt(value) -> str:
    return "None" if value is None else f"{float(value):.2f}"


def _as_float(value) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


def _as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_datetime(value) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime()
