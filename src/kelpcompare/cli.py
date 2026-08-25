"""CLI entry point: kelpcompare ingest|qc|features|rebuild (docs/01 s5, ADR-002).

The dependency order lives here rather than in a scheduler (ADR-002), and every
run writes a manifest (hard rule 7): a Parquet file written outside this CLI
cannot be traced back to a fetch, and an untraceable number cannot be published.

Both commands fail soft, per the docs/02 cross-cutting rules. One unreadable
file, one unregistered serial, one source outage is recorded and stepped over --
never allowed to abort a run and lose the inputs that were fine. Recorded, then
reflected in the exit code: a run that quietly skipped something is worse than
one that says so.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import click

from kelpcompare.adapters import hobo_xlsx
from kelpcompare.adapters.base import REGISTRY_GATE, Check, ValidationReport
from kelpcompare.fetchers import ndbc
from kelpcompare.fetchers.base import SourceUnavailable, land
from kelpcompare.manifest import RunManifest
from kelpcompare.normalize import to_observations
from kelpcompare.parameters import load_parameters
from kelpcompare.qc import evaluate
from kelpcompare.registry import (
    Deployment,
    Registry,
    Station,
    find_deployments,
    find_stations,
    load_registry,
)
from kelpcompare.storage import Zones, read_observations, stored_sources, write_observations

#: Tried in order; the first whose `sniff()` accepts the file wins. A new logger
#: brand is one adapter module and one entry here (docs/06 s4).
ADAPTERS = (hobo_xlsx,)

#: docs/03 source vocabulary -> its raw landing directory. The names differ on
#: purpose: the source is `project`, the directory is `project_sensors/`.
RAW_DIRECTORY = {"project": "project_sensors"}

#: Sources that are pulled rather than dropped. A new public source is one
#: fetcher module and one entry here (docs/02). Keyed by the docs/03 source name,
#: which is also the raw landing directory for these -- the asymmetry above is
#: peculiar to project sensors.
FETCHERS = {ndbc.SOURCE: ndbc}

#: Where a file-drop source expects its files (docs/02 "Project sensors").
INCOMING = "incoming"

#: Non-`pass` here means the file's local timestamps cannot be placed in UTC with
#: confidence, and docs/06 s6 says flag for a human rather than guess.
TIMEZONE_CHECK = "timezone_crosscheck"


@click.group()
def main() -> None:
    """kelpcompare pipeline commands."""


@main.command()
@click.option("--source", required=True, help="Source name per docs/02 (currently: project, ndbc).")
@click.option(
    "--path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="File-drop sources only. File or directory to ingest; defaults to incoming/.",
)
@click.option(
    "--station",
    multiple=True,
    help="Pulled sources only. Station code or site_id; repeatable. Defaults to every "
    "station the registry declares for the source.",
)
@click.option(
    "--year",
    multiple=True,
    type=int,
    help="Pulled sources only. Archive year; repeatable. Defaults to the realtime feed.",
)
@click.option(
    "--data-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Root of the docs/03 data zones. Defaults to ./data.",
)
@click.option(
    "--registry",
    "registry_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Site registry. Defaults to {data-root}/registry/sites.json.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would happen; write no files, not even the manifest.",
)
def ingest(
    source: str,
    path: Path | None,
    station: tuple[str, ...],
    year: tuple[int, ...],
    data_root: Path | None,
    registry_path: Path | None,
    dry_run: bool,
) -> None:
    """Land one source's data in raw/ and observations/ (docs/03).

    Two shapes of source, one command, because what an operator wants is the
    same either way -- everything for this source, landed and normalized, with a
    manifest. Project sensors arrive as files dropped in `incoming/`; public
    sources are pulled over HTTP. The options that apply to only one shape say
    so, and refuse rather than being quietly ignored: `--year 2023` silently
    doing nothing to a HOBO ingest is how an operator comes to believe they have
    a year of data they never fetched.
    """
    if source in FETCHERS:
        if path is not None:
            raise SystemExit(f"--path does not apply to {source!r}, which is pulled, not dropped")
        return _ingest_pulled(
            source,
            stations=station,
            years=year,
            data_root=data_root,
            registry_path=registry_path,
            dry_run=dry_run,
        )

    if source not in RAW_DIRECTORY:
        raise SystemExit(
            f"ingest --source {source!r} is not implemented; available: "
            f"{', '.join(sorted(set(RAW_DIRECTORY) | set(FETCHERS)))}. See docs/02 for the rest."
        )
    if station or year:
        raise SystemExit(
            f"--station/--year do not apply to {source!r}, which is a file-drop source; "
            "its deployment windows come from the registry (docs/06 §3)"
        )

    zones = Zones.at(data_root)
    registry = load_registry(registry_path or zones.sites_json)
    parameters = load_parameters(zones.parameters_json)

    raw_directory = RAW_DIRECTORY[source]
    inputs = _discover(path or zones.raw_source(raw_directory) / INCOMING)
    if not inputs:
        click.echo(f"nothing to ingest for {source!r}")
        return

    run = RunManifest.start("ingest", argv=[f"--source={source}"], sources=[source])
    for candidate in inputs:
        _ingest_file(
            candidate,
            zones=zones,
            registry=registry,
            parameters=parameters,
            run=run,
            source=source,
            raw_directory=raw_directory,
            dry_run=dry_run,
        )

    _report(run, zones, dry_run=dry_run)


@main.command()
@click.option(
    "--source",
    default=None,
    help="Source name per docs/02. Defaults to every source with stored rows.",
)
@click.option(
    "--data-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Root of the docs/03 data zones. Defaults to ./data.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be flagged; write no files, not even the manifest.",
)
def qc(source: str | None, data_root: Path | None, dry_run: bool) -> None:
    """Run the QARTOD tests over stored observations (docs/04 s1, ADR-004).

    Re-derives `qc_flag` and `qc_tests` for every row it reads and writes them
    back in place, so analysis keeps filtering one table on `qc_flag <= 2` with
    no join (docs/03). Rows are never added, removed, or reordered.

    No `--registry` option: the site registry is ingest's business. The one
    verdict ingest decides, the deployment window, is read back out of
    `qc_tests` rather than recomputed, so a qc run cannot reach a different
    conclusion about it than the run that landed the rows did.
    """
    zones = Zones.at(data_root)
    parameters = load_parameters(zones.parameters_json)
    sources = [source] if source else list(stored_sources(zones))

    run = RunManifest.start("qc", argv=_argv(source), sources=sources)
    evaluated = [
        _qc_source(name, zones=zones, parameters=parameters, run=run, dry_run=dry_run)
        for name in sources
    ]

    if not any(evaluated):
        click.echo("nothing to evaluate" + (f" for {source!r}" if source else ""))
        return

    _report_qc(run, zones, dry_run=dry_run)


def _qc_source(name: str, *, zones: Zones, parameters, run: RunManifest, dry_run: bool) -> bool:
    """Evaluate one source's rows. Returns whether there were any to evaluate.

    Never raises. One source whose stored rows cannot be read, evaluated, or
    written back must not cost the run the sources that are fine -- the same
    fail-soft rule ingest follows (docs/02 cross-cutting rules) -- but it does
    set the exit code, because a zone that silently went unevaluated is worse
    than a loud failure. The whole body is guarded, not just the evaluation: an
    unreadable partition and a partition that cannot be rewritten are both
    ordinary disk failures, and either one escaping would abort the run and take
    its manifest with it (hard rule 7).
    """
    try:
        frame = read_observations(zones, name)
        if frame.empty:
            return False

        # Storage keeps timestamps tz-naive UTC (docs/03); everything upstream of
        # that boundary carries the zone explicitly, so put it back before use.
        frame["timestamp"] = frame["timestamp"].dt.tz_localize("UTC")
        outcome = evaluate(frame, parameters)

        run.note_flags(outcome.flag_counts)
        for warning in outcome.warnings:
            run.note_warning(f"{name}: {warning}")
        for series in outcome.series:
            run.add_series(
                source=series.source,
                site_id=series.site_id,
                parameter=series.parameter,
                depth_m=series.depth_m,
                rows=series.rows,
                tests=list(series.tests),
                qc_flags=series.flag_counts,
            )

        if not dry_run:
            write_observations(outcome.frame, zones, source=name, run_id=run.run_id)
    except Exception as error:  # noqa: BLE001 -- one bad source must not end the run
        run.note_warning(f"{name}: {type(error).__name__}: {error}")
    return True


def _argv(source: str | None) -> list[str]:
    return [f"--source={source}"] if source else []


def _report_qc(run: RunManifest, zones: Zones, *, dry_run: bool) -> None:
    for series in run.series:
        histogram = " ".join(f"{flag}:{n}" for flag, n in sorted(series.qc_flags.items()))
        click.echo(f"{series.site_id:>20}  {series.parameter}  {series.rows} rows  {histogram}")
    for warning in run.warnings:
        click.echo(f"     warning  {warning}")

    total = " ".join(f"{flag}:{n}" for flag, n in sorted(run.qc_flags.items()))
    click.echo(f"\n{len(run.series)} series evaluated" + (f" -- {total}" if total else ""))

    if dry_run:
        click.echo("dry run: nothing written, no manifest")
    else:
        click.echo(f"manifest: {run.write(zones)}")

    if run.warnings:
        raise SystemExit(1)


@main.command()
def rebuild() -> None:
    """Regenerate all derived zones from raw/. Not yet implemented."""
    raise SystemExit("rebuild not implemented — see docs/03 integrity rules")


# --------------------------------------------------------------------------
# Pulled sources, one station-window at a time
# --------------------------------------------------------------------------


def _ingest_pulled(
    source: str,
    *,
    stations: tuple[str, ...],
    years: tuple[int, ...],
    data_root: Path | None,
    registry_path: Path | None,
    dry_run: bool,
) -> None:
    """Fetch, land, parse and store every requested station-window (docs/02).

    The unit of work is one station and one window, because that is the unit an
    outage applies to: a station that is down for 2019 must cost the run 2019 for
    that station and nothing else.
    """
    zones = Zones.at(data_root)
    registry = load_registry(registry_path or zones.sites_json)
    parameters = load_parameters(zones.parameters_json)
    fetcher = FETCHERS[source]

    declared = find_stations(registry, source)
    wanted = _select_stations(declared, stations)
    if not wanted:
        known = ", ".join(s.station_code for s in declared) or "none"
        raise SystemExit(
            f"no station to ingest for {source!r}; requested "
            f"{', '.join(stations) or 'all'}, registry declares: {known}"
        )

    run = RunManifest.start("ingest", argv=_pulled_argv(source, stations, years), sources=[source])
    for site in wanted:
        for window in years or (None,):
            _ingest_window(
                site,
                window,
                zones=zones,
                parameters=parameters,
                run=run,
                source=source,
                fetcher=fetcher,
                dry_run=dry_run,
            )

    _report(run, zones, dry_run=dry_run)


def _ingest_window(
    site: Station,
    year: int | None,
    *,
    zones: Zones,
    parameters,
    run: RunManifest,
    source: str,
    fetcher,
    dry_run: bool,
) -> None:
    """One station, one window. Never raises (docs/02 fail-soft rule).

    Two failures, told apart deliberately. A source that did not answer is
    `skipped` and recorded as a manifest gap -- docs/01 s5 requires a missing
    NDBC month not to block the rest of a run, so it does not set the exit code.
    Anything else is `failed`, which does: a payload that arrived and could not
    be parsed is a bug or a format change, and both need a human.
    """
    label = f"{site.station_code} {year if year is not None else 'realtime'}"
    entry = run.add_file(label, "failed", fetcher=getattr(fetcher, "FETCHER_NAME", source))
    entry.site_id = site.site_id

    try:
        payload = (
            fetcher.fetch_archive(site.station_code, year)
            if year is not None
            else fetcher.fetch_realtime(site.station_code)
        )
    except SourceUnavailable as outage:
        entry.outcome = "skipped"
        entry.reason = str(outage)
        run.note_gap(f"{source}: {label}: {outage}")
        return
    except Exception as error:  # noqa: BLE001 -- one window must not end the run
        entry.outcome = "failed"
        entry.reason = f"{type(error).__name__}: {error}"
        run.note_warning(f"{label}: {entry.reason}")
        return

    entry.path = payload.url
    try:
        # Landed before parsing, always: NDBC realtime holds roughly 45 days, so
        # a payload not written down today cannot be fetched again tomorrow
        # (docs/02 cross-cutting rules).
        if not dry_run:
            entry.landed = str(land(payload, zones))

        parsed = fetcher.parse(
            payload,
            parameters,
            site_id=site.site_id,
            depths_m=site.sensor_depths_m,
            run_id=run.run_id,
        )
        entry.rows_in = parsed.rows_in
        entry.rows_out = len(parsed.frame)
        entry.qc_flags = parsed.flag_counts
        entry.warnings.extend(parsed.warnings)
        run.note_flags(parsed.flag_counts)
        for warning in parsed.warnings:
            run.note_warning(f"{label}: {warning}")

        if not dry_run:
            written = write_observations(parsed.frame, zones, source=source, run_id=run.run_id)
            entry.partitions = [str(p) for p in written]
        entry.outcome = "ingested"

    except Exception as error:  # noqa: BLE001 -- one window must not end the run
        entry.outcome = "failed"
        entry.reason = f"{type(error).__name__}: {error}"
        run.note_warning(f"{label}: {entry.reason}")


def _select_stations(declared: tuple[Station, ...], requested: tuple[str, ...]) -> list[Station]:
    """Match `--station` against either identifier, case-insensitively.

    Both, because an operator thinks in station codes (`LJAC1`) while the rest of
    the project joins on `site_id` (`NDBC:LJAC1`), and making them type the one
    they were not thinking of buys nothing.
    """
    if not requested:
        return list(declared)
    wanted = {value.strip().upper() for value in requested}
    return [
        site
        for site in declared
        if site.station_code.upper() in wanted or site.site_id.upper() in wanted
    ]


def _pulled_argv(source: str, stations: tuple[str, ...], years: tuple[int, ...]) -> list[str]:
    return [
        f"--source={source}",
        *(f"--station={value}" for value in stations),
        *(f"--year={value}" for value in years),
    ]


# --------------------------------------------------------------------------
# One file, start to finish
# --------------------------------------------------------------------------


def _ingest_file(
    path: Path,
    *,
    zones: Zones,
    registry: Registry,
    parameters,
    run: RunManifest,
    source: str,
    raw_directory: str,
    dry_run: bool,
) -> None:
    """Route, validate, land, normalize, write -- recording every outcome.

    Never raises: an unexpected failure on one file becomes a `failed` entry so
    the remaining files still get their chance (docs/02 fail-soft rule).
    """
    adapter = _route(path)
    if adapter is None:
        run.add_file(path, "skipped", reason="no adapter recognized this file")
        return

    entry = run.add_file(path, "failed", adapter=adapter.ADAPTER_NAME)
    try:
        report = adapter.validate(path, registry=registry)
        entry.provenance = report.provenance
        entry.record_checks(report.checks)

        # The adapter is asked for the file three times (validate, metadata,
        # parse) rather than caching a load. A handful of files per quarter makes
        # that irrelevant, and it keeps the docs/06 s4 contract to three
        # functions instead of growing a fourth for this caller's convenience.
        metadata = adapter.metadata(path)
        entry.serial = metadata.get("serial")

        blocking = _blocking_check(report)
        if blocking is not None:
            entry.outcome = "quarantined"
            entry.reason = f"{blocking.name}: {blocking.detail}"
            entry.quarantined_to = _as_text(_quarantine(path, zones, dry_run=dry_run))
            return

        deployment, ambiguity = _select_deployment(registry, metadata)
        if deployment is None:
            entry.outcome = "quarantined"
            entry.reason = ambiguity
            entry.quarantined_to = _as_text(_quarantine(path, zones, dry_run=dry_run))
            return
        entry.site_id = deployment.site_id

        raw = adapter.parse(path)
        entry.rows_in = len(raw.data)

        batch = to_observations(raw, deployment, parameters, source=source, run_id=run.run_id)
        entry.rows_out = len(batch.frame)
        entry.qc_flags = batch.flag_counts
        entry.warnings.extend(batch.warnings)

        entry.landed = _as_text(_land(path, zones, raw_directory, entry.serial, dry_run=dry_run))
        if not dry_run:
            written = write_observations(batch.frame, zones, source=source, run_id=run.run_id)
            entry.partitions = [str(p) for p in written]
        entry.outcome = "ingested"

    except Exception as error:  # noqa: BLE001 -- one bad file must not end the run
        entry.outcome = "failed"
        entry.reason = f"{type(error).__name__}: {error}"
        run.note_warning(f"{path.name}: {entry.reason}")


def _route(path: Path):
    """The first adapter that recognizes the file, or None."""
    for adapter in ADAPTERS:
        if adapter.sniff(path):
            return adapter
    return None


def _blocking_check(report: ValidationReport) -> Check | None:
    """The checks that stop an ingest, in the order they should be reported.

    Only two. The registry gate is docs/06 s5 check 4. The timezone cross-check
    joins it because the normalizer places timestamps using a fixed offset taken
    from the registry zone: if the header disagrees with that zone, or the
    deployment straddles a DST transition HOBOconnect handles in an unverified
    way (docs/06 s6), the honest answer is that we do not know the offset.

    Everything else -- a statistics mismatch, a cadence gap, a renamed file -- is
    recorded and ingested. Those are facts about the data, not reasons to refuse
    it.
    """
    gate = report.check(REGISTRY_GATE)
    if gate is not None and gate.status == "fail":
        return gate

    timezone = report.check(TIMEZONE_CHECK)
    if timezone is not None and timezone.status != "pass":
        return timezone
    return None


def _select_deployment(registry: Registry, metadata: dict) -> tuple[Deployment | None, str | None]:
    """Pick which deployment of this serial the file belongs to.

    A logger is redeployed -- the reviewed TidbiT is on its third deployment, and
    each has its own window (docs/06 s3). Applying deployment 1's window to
    deployment 3's file would flag every row out-of-window, so an ambiguous match
    quarantines rather than picking one.
    """
    serial = metadata.get("serial")
    candidates = [d for d in find_deployments(registry, serial or "") if d.is_complete]
    if not candidates:
        return None, f"no complete deployment record for serial {serial!r}"
    if len(candidates) == 1:
        return candidates[0], None

    number = metadata.get("deployment_number")
    if number is not None:
        numbered = [d for d in candidates if d.deployment_number == number]
        if len(numbered) == 1:
            return numbered[0], None

    return None, (
        f"serial {serial} matches {len(candidates)} deployment records and the file "
        f"reports deployment_number={number!r}; cannot tell which window applies"
    )


# --------------------------------------------------------------------------
# Files on disk
# --------------------------------------------------------------------------


def _discover(target: Path) -> list[Path]:
    if not target.exists():
        return []
    if target.is_file():
        return [target]
    return sorted(p for p in target.iterdir() if p.is_file() and not p.name.startswith("."))


def _land(
    path: Path, zones: Zones, raw_directory: str, serial: str | None, *, dry_run: bool
) -> Path:
    """Copy the file into `raw/`, named by content hash. Never moves, never overwrites.

    Copy rather than move because the drop directory sits inside `raw/` and hard
    rule 1 forbids deleting from it -- the operator clears `incoming/`, not the
    pipeline. Content-addressed so re-running on the same bytes is a no-op and
    two different files can never collide on one name.
    """
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    target = (
        zones.raw_source(raw_directory) / (serial or "unknown-serial") / f"{digest}__{path.name}"
    )
    if dry_run or target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def _quarantine(path: Path, zones: Zones, *, dry_run: bool) -> Path:
    """Copy a rejected file to `quarantine/` -- deliberately not into `raw/`.

    Hard rule 5 says quarantine *instead of* ingest, and `raw/` is the record of
    what the project chose to trust. The original stays in the drop directory, so
    fixing the registry and re-running picks it straight back up.
    """
    target = zones.quarantine / path.name
    if dry_run:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


def _as_text(path: Path | None) -> str | None:
    return None if path is None else str(path)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _report(run: RunManifest, zones: Zones, *, dry_run: bool) -> None:
    counts = run.counts()
    for entry in run.files:
        rows = "" if entry.rows_out is None else f" {entry.rows_out} rows"
        detail = f" -- {entry.reason}" if entry.reason else ""
        click.echo(f"{entry.outcome:>12}  {Path(entry.path).name}{rows}{detail}")

    summary = ", ".join(f"{n} {outcome}" for outcome, n in sorted(counts.items()))
    click.echo(f"\n{summary or 'no files'}")

    if dry_run:
        click.echo("dry run: nothing written, no manifest")
    else:
        click.echo(f"manifest: {run.write(zones)}")

    if counts.get("failed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
