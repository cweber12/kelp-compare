"""Run manifests: how a number in a notebook traces back to a fetch (docs/03).

Every ingest/QC/feature run writes one JSON file to `raw/_manifests/{run_id}.json`
recording what it touched and what it found. This is the audit trail that ADR-002
offers in place of a scheduler UI, and the reason CLAUDE.md hard rule 7 forbids
writing Parquet outside the CLI: a file with no manifest cannot be traced, and an
untraceable number cannot be published.

The manifest is also where a run says what it did *not* do. A quarantined file, a
source that was down, an unmapped series -- all are recorded and the run
continues (docs/02 cross-cutting rules: fail soft, never fatal). Silence about a
skipped input would be the actual failure.

A run that stopped early says so too. The manifest is written twice: once when the
run starts work, marked `running`, and once when it stops, marked `completed` or
`interrupted`. Writing only at the end would mean an interrupted run left rows in
`observations/` under a `fetch_run_id` no manifest described -- traceable bytes and
an untraceable run, which is the one thing hard rule 7 exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from kelpcompare.adapters.base import Check
from kelpcompare.storage import Zones

#: What happened to one input file. `failed` is an unexpected error; a file the
#: registry gate turned away is `quarantined`, which is the system working; and
#: `unchanged` is a pulled window the source says we already hold, which is the
#: system working at zero cost. `skipped` is neither of those -- it is an outage
#: or a file nothing recognised, and it is the only one that also notes a gap.
Outcome = Literal["ingested", "quarantined", "skipped", "unchanged", "failed"]

#: How a run ended, as opposed to how one input fared. `running` is the record
#: written before the work starts; a manifest still saying it when no process is
#: alive is a run that was killed without unwinding -- closing a console window on
#: Windows terminates the process without running `finally`, so this record is the
#: only trace such a run leaves. `interrupted` is what an unwinding run writes on
#: its way out, describing the work that did complete.
#:
#: A manifest carrying no `status` at all predates this field and describes a
#: completed run: before it existed, the only manifest a run could write was the
#: one at the end.
Status = Literal["running", "completed", "interrupted"]

_RUN_ID_FORMAT = "%Y%m%dT%H%M%S"


def new_run_id(command: str) -> str:
    """`20260824T193012481Z-ingest`.

    Millisecond precision and no punctuation, so run ids sort chronologically as
    plain strings. `storage._dedupe` relies on that ordering to break ties
    between overlapping readouts deterministically (docs/06 s5 check 5).
    """
    now = datetime.now(UTC)
    return f"{now.strftime(_RUN_ID_FORMAT)}{now.microsecond // 1000:03d}Z-{command}"


def code_version() -> tuple[str | None, bool]:
    """The git SHA and whether the tree was dirty.

    Both, because a SHA alone overstates reproducibility: a dirty tree means the
    code that produced this run is not the code that SHA names. Never fatal --
    the pipeline must run outside a checkout.
    """
    sha = _git("rev-parse", "HEAD")
    if sha is None:
        return None, False
    return sha, bool(_git("status", "--porcelain"))


@dataclass
class FileEntry:
    """One input's story, in the order a reader wants it.

    "File" is the older half of the truth: a pulled source's input is a URL and a
    window, not a file on disk. The fields are the same ones either way -- what
    it was, what happened to it, how many rows came in and out -- so the entry is
    shared rather than duplicated. `adapter` and `fetcher` are how a reader tells
    which road a row travelled, and exactly one of them is ever set.

    `site_id` and `polygon_id` are alternatives, not a pair: an observation
    belongs to a site and a canopy value belongs to a polygon, and calling a
    polygon a site to save a field would put a lie in the audit trail.
    `dataset_revision` is the upstream version a landing came from, for the two
    sources that are downloaded by hand: a Kelp Watch export carries no version
    of its own and takes the revision the polygon registry pins, while a Shore
    Stations archive declares its own archive date and takes that. Hence
    `int | str` -- the field records what the upstream calls its version, and the
    two upstreams do not agree on what a version looks like.
    """

    path: str
    outcome: Outcome
    adapter: str | None = None
    fetcher: str | None = None
    provenance: str | None = None
    serial: str | None = None
    site_id: str | None = None
    polygon_id: str | None = None
    dataset_revision: int | str | None = None
    landed: str | None = None
    quarantined_to: str | None = None
    rows_in: int | None = None
    rows_out: int | None = None
    qc_flags: dict[str, int] = field(default_factory=dict)
    partitions: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str | None = None

    def record_checks(self, checks: tuple[Check, ...]) -> None:
        self.checks = [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks]
        self.warnings.extend(
            f"{c.name}: {c.detail}" for c in checks if c.status in ("warn", "skipped", "fail")
        )


@dataclass
class SeriesEntry:
    """One series a run looked at. What a `FileEntry` is to ingest, this is to
    the stages that read the zone.

    A qc or features run has no input files -- it reads a zone, not a drop
    directory -- so recording its work as files would be a fiction that made the
    manifest harder to read, not easier.

    Shared between the two stages rather than split, because a series is a
    series whichever stage looked at it. Each fills the fields it has: qc the
    flag histogram and the tests it ran, features the quarter counts. The
    quarter counts are what make coverage attrition readable without opening the
    Parquet -- how many quarters a series produced, and how many survived the
    coverage floor.

    A kelp series is a polygon rather than a site and has no parameter or depth,
    so it fills `polygon_id` and leaves those empty -- the same alternative
    `FileEntry` draws between a site and a polygon. `quarters_observed` is the
    kelp half's: a bed can be fully observed and mostly unusable, or the
    reverse, and one number cannot say which.
    """

    source: str
    site_id: str | None = None
    polygon_id: str | None = None
    parameter: str | None = None
    depth_m: float | None = None
    rows: int = 0
    tests: list[str] = field(default_factory=list)
    qc_flags: dict[str, int] = field(default_factory=dict)
    quarters: int | None = None
    quarters_usable: int | None = None
    quarters_observed: int | None = None
    first_quarter: str | None = None
    last_quarter: str | None = None


@dataclass
class RunManifest:
    """One run, accumulated as it goes and written at its start and its end."""

    run_id: str
    command: str
    argv: list[str] = field(default_factory=list)
    code_sha: str | None = None
    code_dirty: bool = False
    started_at: str = ""
    finished_at: str | None = None
    status: Status = "running"
    sources: list[str] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    series: list[SeriesEntry] = field(default_factory=list)
    qc_flags: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @classmethod
    def start(
        cls,
        command: str,
        *,
        run_id: str | None = None,
        argv: list[str] | None = None,
        sources: list[str] | None = None,
    ) -> RunManifest:
        sha, dirty = code_version()
        return cls(
            run_id=run_id or new_run_id(command),
            command=command,
            argv=list(argv or []),
            code_sha=sha,
            code_dirty=dirty,
            started_at=_now(),
            sources=list(sources or []),
        )

    def add_file(self, path: Path | str, outcome: Outcome, **fields) -> FileEntry:
        entry = FileEntry(path=str(path), outcome=outcome, **fields)
        self.files.append(entry)
        return entry

    def add_series(self, **fields) -> SeriesEntry:
        entry = SeriesEntry(**fields)
        self.series.append(entry)
        return entry

    def note_flags(self, counts: dict[str, int]) -> None:
        """Fold a flag histogram into the run-level one docs/03 requires.

        Run-level rather than summed from `series` on demand, because rows a run
        read but could not evaluate belong in the total too -- a histogram that
        quietly counted only the rows that went well would overstate coverage.
        """
        for flag, count in counts.items():
            self.qc_flags[flag] = self.qc_flags.get(flag, 0) + count

    def note_warning(self, message: str) -> None:
        self.warnings.append(message)

    def note_gap(self, message: str) -> None:
        """An upstream hole: a source outage, a cadence gap, a missing quarter."""
        self.gaps.append(message)

    def counts(self) -> dict[str, int]:
        """Outcome histogram -- the one line an operator reads first."""
        tally: dict[str, int] = {}
        for entry in self.files:
            tally[entry.outcome] = tally.get(entry.outcome, 0) + 1
        return tally

    def finish(self) -> RunManifest:
        self.finished_at = _now()
        self.status = "completed"
        return self

    def interrupt(self) -> RunManifest:
        """The run is unwinding and will not reach its report.

        Stamped with a finish time like a completed run, because it did stop
        then -- `status` is what says the stopping was not the end of the work.
        """
        self.finished_at = _now()
        self.status = "interrupted"
        return self

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["counts"] = self.counts()
        return payload

    def write(self, zones: Zones) -> Path:
        """Write the run's terminal record to `raw/_manifests/{run_id}.json`.

        The one sanctioned write into `raw/` besides a landing (hard rule 1): a
        manifest describes the landings, so it lives beside them.

        Finishes a run that has not been given a terminal state yet, which is
        what the report functions rely on. A run already marked `interrupted`
        keeps that: this is the call its unwind path uses to record itself, and
        silently promoting it to `completed` would put back into the audit trail
        the exact lie the field exists to prevent.
        """
        if self.status == "running":
            self.finish()
        return self._write(zones)

    def write_start(self, zones: Zones) -> Path:
        """Write the `running` record, before the run does any work.

        Overwritten by `write` when the run stops. The pair is what survives a
        process killed without unwinding, where no `finally` runs and this file
        is the only evidence the run existed.
        """
        return self._write(zones)

    def _write(self, zones: Zones) -> Path:
        zones.manifests.mkdir(parents=True, exist_ok=True)
        target = zones.manifests / f"{self.run_id}.json"
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return target


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None
