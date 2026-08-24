"""The vendor adapter contract: shared types plus the registry gate.

Every adapter implements `sniff(path) -> bool`, `parse(path) -> RawSeries`, and
`metadata(path) -> dict` (docs/06 s4) and returns the types defined here. Nothing
in this module knows about any particular vendor -- that is the point: the next
logger brand reuses these types and `registry_gate` unchanged.

Adapters extract faithfully and judge; they never convert and never act. No
timezone conversion, no unit conversion, no deployment-window trimming, no file
moves. Converting is the normalizer's job; acting on a quarantine verdict is the
ingest CLI's (docs/03).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd

from kelpcompare.registry import Deployment, Registry, find_deployments

Provenance = Literal["original", "edited"]
CheckStatus = Literal["pass", "fail", "warn", "skipped"]

#: Name of the check that decides quarantine (docs/06 s5 check 4).
REGISTRY_GATE = "registry_gate"

#: Columns of `RawSeries.data`, long format so a multi-series logger needs no
#: schema change (docs/06 s6).
DATA_COLUMNS = ("row_number", "timestamp_local", "series_name", "unit", "value")


@dataclass(frozen=True)
class Check:
    """One validation result. `detail` is written to be read in a manifest."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class SeriesInfo:
    """Per-series metadata as the file reports it.

    `unit` is verbatim from the column header ("degF", "degC", ...). It is never
    normalized here -- HOBOconnect is configurable, so the header is the only
    trustworthy source (docs/06 s6).
    """

    name: str
    unit: str
    column: str
    n: int
    first: datetime | None = None
    last: datetime | None = None
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None


@dataclass(frozen=True)
class RawSeries:
    """What `parse()` returns: measurements plus the metadata the format carries.

    `timestamp_local` is tz-naive on purpose. The header timezone token is
    reported separately as `tz_token`; applying it is the normalizer's job.
    """

    path: Path
    provenance: Provenance
    edit_signals: tuple[str, ...]
    tz_token: str | None
    data: pd.DataFrame
    series: tuple[SeriesInfo, ...]

    def series_frame(self, name: str) -> pd.DataFrame:
        """Just the rows for one series, in file order."""
        return self.data.loc[self.data["series_name"] == name].reset_index(drop=True)


@dataclass(frozen=True)
class ValidationReport:
    """The docs/06 s5 results for one file. Destined for the run manifest."""

    path: Path
    provenance: Provenance
    checks: tuple[Check, ...]

    def check(self, name: str) -> Check | None:
        for result in self.checks:
            if result.name == name:
                return result
        return None

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    @property
    def warnings(self) -> tuple[str, ...]:
        """Details of everything the operator should look at.

        Skipped checks count: docs/06 s3 requires the consistency checks to be
        "skipped with a warning" on hand-edited files, so a skip is never silent.
        """
        return tuple(
            f"{c.name}: {c.detail}" for c in self.checks if c.status in ("warn", "skipped")
        )

    @property
    def quarantined(self) -> bool:
        """True iff the registry gate failed -- the only check that quarantines."""
        gate = self.check(REGISTRY_GATE)
        return gate is not None and gate.status == "fail"


def registry_gate(serial: str | None, registry: Registry) -> Check:
    """docs/06 s5 check 4: no deployment record for this serial, no ingest.

    Needs only a serial string, so every vendor adapter shares it. Requires a
    timezone, an in-water window, and a series map on the record -- but
    deliberately not a position, which may legitimately be null (see
    `registry.Deployment`).

    Returns a verdict. Moving the file into `data/quarantine/` is the ingest
    CLI's job (docs/03): one place decides what happens to a file.
    """
    if not serial:
        return Check(
            REGISTRY_GATE,
            "fail",
            "no serial found in the file; cannot match a deployment record -- quarantine",
        )

    matches = find_deployments(registry, serial)
    if not matches:
        return Check(
            REGISTRY_GATE,
            "fail",
            f"no deployment record for serial {serial} in {registry.path} -- quarantine",
        )

    complete = [d for d in matches if d.is_complete]
    if not complete:
        missing = ", ".join(_incomplete_reason(d) for d in matches)
        return Check(
            REGISTRY_GATE,
            "fail",
            f"deployment record(s) for serial {serial} are incomplete ({missing}) -- quarantine",
        )

    listed = ", ".join(
        f"{d.site_id} deployment {d.deployment_number} tz={d.tz} window={d.window_local}"
        for d in complete
    )
    return Check(
        REGISTRY_GATE, "pass", f"serial {serial} matched {len(complete)} record(s): {listed}"
    )


def _incomplete_reason(deployment: Deployment) -> str:
    """Name which of the required registry fields a record is missing."""
    required = (
        ("tz", deployment.tz),
        ("window_local", deployment.window_local),
        ("series_map", deployment.series_map),
    )
    missing = " and ".join(name for name, value in required if not value)
    return f"{deployment.site_id} deployment {deployment.deployment_number} missing {missing}"
