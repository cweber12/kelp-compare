"""Vendor extraction -> docs/03 observation rows: UTC, SI, controlled names.

This is the boundary CLAUDE.md hard rule 2 describes. Upstream of it, an adapter
reports what a file said -- local timestamps, whatever unit the header carried,
whatever the operator named the sensor. Downstream of it, everything is UTC, SI,
and drawn from the controlled vocabulary. Nothing past this module ever sees a
HOBO word (docs/06 s4).

Three decisions here are load-bearing and none of them guess:

**Time.** The offset is derived once, from the registry timezone at the first
sample, and applied to every timestamp as a fixed offset -- exactly what docs/06
s6 prescribes while HOBOconnect's behaviour across the November DST transition
remains unverified. A deployment that actually spans a transition is caught
upstream by `hobo_common.check_timezone` and refused by the ingest CLI, rather
than silently resolved with a guess about which side of the boundary a
timestamp fell on.

**Units.** The source unit comes from the file header and the target from
`parameters.json`. An unrecognized or unconvertible pair raises. There is no
default and no passthrough: a Fahrenheit value stored as if it were Celsius is
the kind of error that survives into a publication.

**The deployment window.** Out-of-window rows are written and flagged, not
dropped (docs/06 s3, ADR-004). Flags, never deletions: the install transient
stays inspectable, the default `qc_flag <= 2` filter excludes it anyway, and
correcting a window is a registry edit plus a rebuild rather than a re-ingest.

The docs/03 source vocabulary calls this source `project` while its raw landing
directory is `project_sensors/` -- a deliberate asymmetry, not a typo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from kelpcompare.adapters.base import RawSeries
from kelpcompare.parameters import Parameters
from kelpcompare.registry import Deployment
from kelpcompare.storage import OBSERVATION_COLUMNS

#: docs/03 QARTOD roll-up: 1 pass, 2 not evaluated, 3 suspect, 4 fail, 9 missing.
FLAG_NOT_EVALUATED = 2
FLAG_FAIL = 4
FLAG_MISSING = 9

#: The one test this stage can decide. QARTOD proper runs in `kelpcompare qc`.
WINDOW_TEST = "deployment_window"

#: Header units seen in the wild, canonicalized. Extended deliberately, one
#: family at a time -- an unknown unit must raise, never fall through.
_UNIT_ALIASES = {
    "f": "degF",
    "degf": "degF",
    "fahrenheit": "degF",
    "c": "degC",
    "degc": "degC",
    "celsius": "degC",
}

#: Only conversions the project has a reason to perform.
_CONVERSIONS = {("degF", "degC"): lambda values: (values - 32.0) * 5.0 / 9.0}


@dataclass(frozen=True)
class NormalizedBatch:
    """docs/03 rows plus what the normalizer wants the manifest to record."""

    frame: pd.DataFrame
    warnings: tuple[str, ...] = ()
    skipped_series: tuple[str, ...] = ()
    utc_offset: timedelta | None = None
    window_utc: tuple[pd.Timestamp, pd.Timestamp] | None = None

    @property
    def flag_counts(self) -> dict[str, int]:
        counts = self.frame["qc_flag"].value_counts().to_dict()
        return {str(flag): int(n) for flag, n in sorted(counts.items())}


def to_observations(
    raw: RawSeries,
    deployment: Deployment,
    parameters: Parameters,
    *,
    source: str,
    run_id: str,
) -> NormalizedBatch:
    """Convert one parsed file into observation rows for one deployment.

    Raises `ValueError` when a mapped series carries a unit that cannot become
    the parameter's canonical unit, or when the deployment lacks the timezone and
    window the registry gate is supposed to guarantee. Unmapped series are a
    softer problem -- reported and skipped, so one unrecognized column on a
    multi-series logger does not cost the run its temperature record.
    """
    if not deployment.tz or deployment.window_local is None:
        raise ValueError(
            f"deployment {deployment.site_id}/{deployment.deployment_number} has no "
            "timezone or window; the registry gate should have quarantined this file"
        )

    offset = _utc_offset(raw, deployment)
    window = _window_utc(deployment, offset)

    warnings: list[str] = []
    skipped: list[str] = []
    frames: list[pd.DataFrame] = []

    for info in raw.series:
        parameter = deployment.parameter_for(info.name)
        if parameter is None:
            skipped.append(info.name)
            warnings.append(
                f"series {info.name!r} ({info.unit}) has no series_map entry on "
                f"{deployment.site_id} deployment {deployment.deployment_number}; skipped"
            )
            continue
        if parameter not in parameters:
            raise ValueError(
                f"series_map points {info.name!r} at {parameter!r}, which is not in "
                f"{parameters.path}; known: {', '.join(parameters.names)}"
            )

        frames.append(
            _series_rows(
                raw.series_frame(info.name),
                header_unit=info.unit,
                parameter=parameters[parameter],
                deployment=deployment,
                offset=offset,
                window=window,
                source=source,
                run_id=run_id,
            )
        )

    frame = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=list(OBSERVATION_COLUMNS))
    )
    return NormalizedBatch(
        frame=frame[list(OBSERVATION_COLUMNS)],
        warnings=tuple(warnings),
        skipped_series=tuple(skipped),
        utc_offset=offset,
        window_utc=window,
    )


def convert_unit(values: pd.Series, header_unit: str, target_unit: str) -> pd.Series:
    """Convert a series to the parameter's canonical unit, or refuse.

    Refusing is the point. The unit is read from the file header because
    HOBOconnect is configurable (docs/06 s6), so an unrecognized token means the
    project has met a format it has not verified -- which is a change to make
    deliberately, in `_UNIT_ALIASES`, not one to absorb silently.
    """
    source = _canonical_unit(header_unit)
    target = _canonical_unit(target_unit)
    if source == target:
        return values.astype("float64")

    conversion = _CONVERSIONS.get((source, target))
    if conversion is None:
        raise ValueError(
            f"cannot convert {header_unit!r} to {target_unit!r}: no conversion from "
            f"{source!r} to {target!r} is defined in normalize.observations"
        )
    return conversion(values.astype("float64"))


def _series_rows(
    series: pd.DataFrame,
    *,
    header_unit: str,
    parameter,
    deployment: Deployment,
    offset: timedelta,
    window: tuple[pd.Timestamp, pd.Timestamp],
    source: str,
    run_id: str,
) -> pd.DataFrame:
    timestamps = (series["timestamp_local"] - offset).dt.tz_localize("UTC")
    values = convert_unit(series["value"], header_unit, parameter.unit)

    # Inclusive at both ends: the window's endpoints are readings that were in
    # the water, which is what reproduces the hand-edited reference file exactly.
    inside = (timestamps >= window[0]) & (timestamps <= window[1])
    missing = values.isna()

    return pd.DataFrame(
        {
            "timestamp": timestamps.to_numpy(),
            "site_id": deployment.site_id,
            "parameter": parameter.name,
            "value": values.to_numpy(dtype="float64"),
            "depth_m": deployment.depth_m,
            # Missing wins the roll-up: an absent value cannot be evaluated at
            # all. The window verdict is recorded either way, in qc_tests.
            "qc_flag": np.where(
                missing, FLAG_MISSING, np.where(inside, FLAG_NOT_EVALUATED, FLAG_FAIL)
            ).astype("int8"),
            "qc_tests": np.where(inside, f"{WINDOW_TEST}:pass", f"{WINDOW_TEST}:fail"),
            "source": source,
            "fetch_run_id": run_id,
        }
    )


def _utc_offset(raw: RawSeries, deployment: Deployment) -> timedelta:
    """The deployment's fixed UTC offset, taken at its first sample (docs/06 s6)."""
    first = min((info.first for info in raw.series if info.first), default=None)
    if first is None:
        raise ValueError(f"{raw.path}: no timestamped samples to place in time")

    localized = pd.Timestamp(first).tz_localize(
        deployment.tz, ambiguous=True, nonexistent="shift_forward"
    )
    return localized.utcoffset()


def _window_utc(deployment: Deployment, offset: timedelta) -> tuple[pd.Timestamp, pd.Timestamp]:
    start, end = deployment.window_local
    return (
        (pd.Timestamp(start) - offset).tz_localize("UTC"),
        (pd.Timestamp(end) - offset).tz_localize("UTC"),
    )


def _canonical_unit(unit: str) -> str:
    """Fold header spellings together: `°F`, `degF`, `deg F` are one unit.

    Punctuation is stripped before lookup so a lookalike degree sign cannot
    create a second, silently-unequal unit. Anything unrecognized keeps its
    original spelling, so it fails to match a target and raises rather than
    quietly comparing equal to something it is not.
    """
    token = re.sub(r"[^a-z0-9]", "", (unit or "").lower())
    return _UNIT_ALIASES.get(token, (unit or "").strip())
