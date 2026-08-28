"""CLI entry point: kelpcompare ingest|qc|features|rebuild (docs/01 s5, ADR-002).

The dependency order lives here rather than in a scheduler (ADR-002), and every
run writes a manifest (hard rule 7): a Parquet file written outside this CLI
cannot be traced back to a fetch, and an untraceable number cannot be published.

Every command fails soft, per the docs/02 cross-cutting rules. One unreadable
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
import pandas as pd

from kelpcompare.adapters import hobo_xlsx
from kelpcompare.adapters.base import QUARANTINE_CHECKS, Check, ValidationReport
from kelpcompare.features.build import BuildOutcome, build_features
from kelpcompare.features.climatology import CLIMATOLOGY_KEY, anomaly_columns
from kelpcompare.features.comparison import COMPARISON_KEY, build_comparison
from kelpcompare.features.config import load_feature_config
from kelpcompare.features.kelp import (
    CLIMATOLOGY_KELP_KEY,
    MEASURED,
    QUARTERLY_KELP_KEY,
    KelpOutcome,
    build_kelp,
)
from kelpcompare.features.quarterly import QUARTERLY_KEY, feature_columns
from kelpcompare.fetchers import cache, kelpwatch, ndbc
from kelpcompare.fetchers.base import NotModified, SourceUnavailable, land
from kelpcompare.manifest import RunManifest
from kelpcompare.normalize import to_observations
from kelpcompare.parameters import load_parameters
from kelpcompare.polygons import Polygons, load_polygons
from kelpcompare.qc import evaluate
from kelpcompare.registry import (
    Deployment,
    Registry,
    Station,
    find_deployments,
    find_stations,
    load_registry,
)
from kelpcompare.storage import (
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    FLAG_PASS,
    Zones,
    read_features,
    read_observations,
    replace_features,
    stored_sources,
    write_features,
    write_observations,
)

#: Tried in order; the first whose `sniff()` accepts the file wins. A new logger
#: brand is one adapter module and one entry here (docs/06 s4).
ADAPTERS = (hobo_xlsx,)

#: docs/03 source vocabulary -> its raw landing directory. The names differ for
#: project sensors on purpose: the source is `project`, the directory is
#: `project_sensors/`.
RAW_DIRECTORY = {"project": "project_sensors", kelpwatch.SOURCE: kelpwatch.SOURCE}

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

#: The `source` a manifest series entry carries when it describes the comparison
#: table rather than a built series. Not a docs/03 source: the comparison is a
#: join of two of them, and it is recorded as a series entry only because that
#: is where a run says how much it produced.
COMPARISON_SERIES = "comparison"


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
    """Land one source's data in raw/, and in observations/ where it belongs there.

    Two shapes of source, one command, because what an operator wants is the
    same either way -- everything for this source, landed, with a manifest.
    Project sensors and Kelp Watch exports arrive as files dropped in
    `incoming/`; public station data is pulled over HTTP. The options that apply
    to only one shape say so, and refuse rather than being quietly ignored:
    `--year 2023` silently doing nothing to a HOBO ingest is how an operator
    comes to believe they have a year of data they never fetched.

    Kelp Watch is the one source that lands raw and writes no observations. A
    canopy value belongs to a polygon and `observations` is keyed on `site_id`,
    so its rows are built by `kelpcompare features` from the landing plus
    `polygons.geojson` -- which also means re-running the build after a registry
    edit needs no second download (docs/02, docs/03).
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
            "its windows come from the registry, not from the command line"
        )

    if source == kelpwatch.SOURCE:
        return _ingest_kelpwatch(path=path, data_root=data_root, dry_run=dry_run)

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
    "--qc-max-flag",
    type=click.IntRange(FLAG_PASS, FLAG_MISSING),
    default=FLAG_NOT_EVALUATED,
    show_default=True,
    help="Keep rows at or below this QC flag. 1 is the pass-only rerun docs/04 s1 asks for.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be built; write no files, not even the manifest.",
)
def features(source: str | None, data_root: Path | None, qc_max_flag: int, dry_run: bool) -> None:
    """Build the quarterly feature tables and the comparison (docs/04 s2-s4).

    Five tables, in the order they depend on each other. `quarterly_env` and
    `climatology_env` come from the observations zone -- one row per QC series
    per Kelp Watch quarter, plus the fixed baseline its anomalies were taken
    against. `quarterly_kelp` and `climatology_kelp` come from the Kelp Watch
    landings plus `polygons.geojson`, on the same calendar and through the same
    climatology code. `comparison` is the join of the two at lags 0-4.

    The environmental half replaces exactly the sources it built, so
    `--source ndbc` after one station's backfill leaves every other source's
    rows alone. The comparison is regenerated wholesale from the two tables as
    they stand on disk afterwards, which is what keeps it a pure function of its
    inputs rather than something that accumulates stale pairs.

    An environment-only project is not an error, and neither is a kelp-only one:
    each half builds if it has anything to build, and the comparison is written
    when both exist.

    No `--registry` option: the site registry is ingest's business. What this
    stage reads is the feature configuration and the polygon registry, which
    live beside it in the same zone (ADR-006).
    """
    zones = Zones.at(data_root)
    # Not fail-soft: a configuration that cannot be parsed is a configuration
    # nothing can be built against, so there is no per-source failure to isolate.
    config = load_feature_config(zones.features_json)
    env_sources = _env_sources(zones, source)

    # `sources` records what a run *built*, not what it considered, so kelp is
    # appended below only once there was something to build. A manifest naming a
    # source that produced no row is a false trail through the audit chain.
    run = RunManifest.start(
        "features", argv=_features_argv(source, qc_max_flag), sources=list(env_sources)
    )
    now = pd.Timestamp.now(tz="UTC")
    outcomes: dict[str, BuildOutcome] = {}

    attempted = [
        _build_source(
            name,
            zones=zones,
            config=config,
            run=run,
            qc_max_flag=qc_max_flag,
            now=now,
            outcomes=outcomes,
        )
        for name in env_sources
    ]
    kelp = _build_kelp(zones, config, run, now=now) if _wants_kelp(source) else None
    if kelp is not None:
        run.sources.append(kelpwatch.SOURCE)

    if not any(attempted) and kelp is None:
        click.echo("nothing to build" + (f" for {source!r}" if source else ""))
        return

    written = () if dry_run else _write_feature_tables(outcomes, kelp, zones, run)
    if not dry_run:
        written += _write_comparison(zones, config, run)
    _report_features(run, zones, written, dry_run=dry_run)


def _env_sources(zones: Zones, source: str | None) -> list[str]:
    """Which observation sources this run builds the environmental half from.

    `--source kelpwatch` names a source with no observations at all -- its rows
    live in `raw/` and are read by the kelp builder -- so it selects no
    environmental source rather than one that would come back empty.
    """
    if source == kelpwatch.SOURCE:
        return []
    return [source] if source else list(stored_sources(zones))


def _wants_kelp(source: str | None) -> bool:
    return source is None or source == kelpwatch.SOURCE


def _build_source(
    name: str,
    *,
    zones: Zones,
    config,
    run: RunManifest,
    qc_max_flag: int,
    now: pd.Timestamp,
    outcomes: dict[str, BuildOutcome],
) -> bool:
    """Build one source's rows. Returns whether there were any to build.

    Never raises, for the reason `_qc_source` never raises: one source whose
    stored rows cannot be read or aggregated must not cost the run the sources
    that are fine (docs/02 fail-soft rule). It does set the exit code, because a
    zone that silently went unbuilt is worse than a loud failure.
    """
    try:
        frame = read_observations(zones, name)
        if frame.empty:
            return False

        # Storage keeps timestamps tz-naive UTC (docs/03); the quarter calendar
        # refuses anything that does not carry its zone, so put it back first.
        frame["timestamp"] = frame["timestamp"].dt.tz_localize("UTC")
        outcome = build_features(frame, config, qc_max_flag=qc_max_flag, now=now)

        for warning in outcome.warnings:
            run.note_warning(f"{name}: {warning}")
        for series in outcome.series:
            run.add_series(
                source=series.source,
                site_id=series.site_id,
                parameter=series.parameter,
                depth_m=series.depth_m,
                rows=series.rows,
                quarters=series.quarters,
                quarters_usable=series.quarters_usable,
                first_quarter=series.first_quarter,
                last_quarter=series.last_quarter,
            )
        outcomes[name] = outcome
    except Exception as error:  # noqa: BLE001 -- one bad source must not end the run
        run.note_warning(f"{name}: {type(error).__name__}: {error}")
    return True


def _build_kelp(zones: Zones, config, run: RunManifest, *, now: pd.Timestamp) -> KelpOutcome | None:
    """Build both kelp tables from the landed exports, or None if there are none.

    None rather than an empty outcome: an environment-only project is not an
    error (docs/03), and writing an empty `quarterly_kelp` would replace a real
    table with nothing the first time somebody ran `features` on a machine whose
    exports had not been landed.

    Never raises. One export that will not parse must not cost the run the ones
    that are fine (docs/02 fail-soft rule) -- but it does set the exit code,
    because a polygon that silently went unbuilt vanishes from a table written
    wholesale, which is worse than a loud failure.
    """
    # No polygon registry at all is not a failure -- it is a project that has
    # not drawn any, which docs/03 says must not make this command fail. A
    # registry that exists and cannot be read is a different thing entirely.
    if not zones.polygons_geojson.exists():
        return None
    try:
        polygons = load_polygons(zones.polygons_geojson)
    except Exception as error:  # noqa: BLE001 -- a bad registry must not lose the env half
        run.note_warning(f"{zones.polygons_geojson.name}: {type(error).__name__}: {error}")
        return None

    landings = _kelp_landings(zones, polygons, run)
    if not landings:
        return None

    frames = []
    for polygon, path in landings:
        try:
            frames.append(kelpwatch.parse(path, polygon).frame)
        except Exception as error:  # noqa: BLE001 -- one export must not end the run
            run.note_warning(f"{polygon.polygon_id}: {path.name}: {type(error).__name__}: {error}")
    if not frames:
        return None

    try:
        outcome = build_kelp(
            pd.concat(frames, ignore_index=True),
            config,
            source=kelpwatch.SOURCE,
            revision=polygons.kelp_watch.revision,
            now=now,
        )
    except Exception as error:  # noqa: BLE001 -- report it, keep the manifest
        run.note_warning(f"{kelpwatch.SOURCE}: {type(error).__name__}: {error}")
        return None

    for warning in outcome.warnings:
        run.note_gap(f"{kelpwatch.SOURCE}: {warning}")
    for polygon in outcome.polygons:
        run.add_series(
            source=kelpwatch.SOURCE,
            polygon_id=polygon.polygon_id,
            rows=polygon.rows,
            quarters=polygon.quarters,
            quarters_usable=polygon.quarters_usable,
            quarters_observed=polygon.quarters_observed,
            first_quarter=polygon.first_quarter,
            last_quarter=polygon.last_quarter,
        )
    return outcome


def _kelp_landings(zones: Zones, polygons: Polygons, run: RunManifest) -> list[tuple]:
    """The one landed export per polygon, at the revision the registry pins.

    Only that revision is read. A newer revision may revise history as well as
    extend it, so reading two as one series would silently mix them -- the
    registry says which one is the source of record, and landings are laid out
    so honouring it is a directory choice rather than a filter.

    Two different landings for one polygon at one revision is a contradiction
    the run cannot resolve: both claim to be that bed's record at that version.
    The polygon is skipped and reported. Raw is append-only (hard rule 1), so
    the fix is not to delete one but to bump the revision and re-ingest, which
    segregates them.
    """
    if polygons.kelp_watch is None:
        return []
    root = zones.raw_source(kelpwatch.SOURCE) / polygons.kelp_watch.label
    if not root.is_dir():
        return []

    found = []
    for polygon in polygons:
        directory = root / polygon.polygon_id.replace(":", "_")
        landed = sorted(directory.glob("*")) if directory.is_dir() else []
        if not landed:
            continue
        if len(landed) > 1:
            run.note_warning(
                f"{polygon.polygon_id}: {len(landed)} different exports landed at "
                f"{polygons.kelp_watch.label} ({', '.join(p.name for p in landed)}); "
                "both claim to be this bed at that revision. Bump kelp_watch.revision and "
                "re-ingest rather than deleting one -- raw is append-only."
            )
            continue
        found.append((polygon, landed[0]))
    return found


def _write_feature_tables(
    outcomes: dict[str, BuildOutcome], kelp: KelpOutcome | None, zones: Zones, run: RunManifest
) -> tuple[Path, ...]:
    """Write the four quarterly tables, each superseding what its build covered.

    Guarded like the build itself: a table that cannot be written is a disk
    failure, and letting it escape would abort the run and take its manifest
    with it (hard rule 7). A source that failed keeps its previous rows rather
    than losing them to a run that never looked at it.

    The kelp tables are scoped by source like the environmental ones, so a
    future second route to the same product -- the published data package, if an
    account ever arrives -- would replace its own rows and not this one's.
    """
    tables: list[tuple[str, tuple[str, ...], pd.DataFrame, tuple[str, ...]]] = []
    if outcomes:
        replacing = tuple(sorted(outcomes))
        tables += [
            (
                "quarterly_env",
                QUARTERLY_KEY,
                _stack([o.quarterly for o in outcomes.values()]),
                replacing,
            ),
            (
                "climatology_env",
                CLIMATOLOGY_KEY,
                _stack([o.climatology for o in outcomes.values()]),
                replacing,
            ),
        ]
    if kelp is not None:
        tables += [
            ("quarterly_kelp", QUARTERLY_KELP_KEY, kelp.quarterly, (kelpwatch.SOURCE,)),
            ("climatology_kelp", CLIMATOLOGY_KELP_KEY, kelp.climatology, (kelpwatch.SOURCE,)),
        ]

    written: list[Path] = []
    for table, key, frame, replacing in tables:
        try:
            written.append(write_features(frame, zones, table=table, key=key, replacing=replacing))
        except Exception as error:  # noqa: BLE001 -- report the failure, keep the manifest
            run.note_warning(f"{table}: {type(error).__name__}: {error}")
    return tuple(written)


def _write_comparison(zones: Zones, config, run: RunManifest) -> tuple[Path, ...]:
    """Regenerate `comparison` from the two quarterly tables as they now stand.

    Read back from disk rather than taken from this run's outcomes, and that is
    the point: a `--source ndbc` run rebuilds one source's environmental rows,
    and the comparison must still reflect every polygon and every other source
    beside them. Reading the zone is what makes the table a function of the zone
    rather than of which arguments the last run happened to carry.

    Written wholesale, so a pair the registry no longer declares loses its rows
    instead of keeping them forever.
    """
    try:
        kelp = read_features(zones, "quarterly_kelp")
        env = read_features(zones, "quarterly_env")
        if kelp.empty or env.empty:
            return ()
        polygons = load_polygons(zones.polygons_geojson)
        frame = build_comparison(
            kelp,
            env,
            polygons,
            kelp_anomalies=anomaly_columns(MEASURED)[1:],
            env_anomalies=anomaly_columns(feature_columns(config)[0])[1:],
        )
        run.add_series(
            source=COMPARISON_SERIES,
            rows=len(frame),
            quarters=int(frame[["polygon_id", "year", "quarter"]].drop_duplicates().shape[0]),
        )
        return (replace_features(frame, zones, table="comparison", key=COMPARISON_KEY),)
    except Exception as error:  # noqa: BLE001 -- report it, keep the manifest
        run.note_warning(f"comparison: {type(error).__name__}: {error}")
        return ()


def _stack(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate, ignoring empties so they cannot widen a column's dtype."""
    populated = [frame for frame in frames if not frame.empty]
    if not populated:
        return frames[0]
    return populated[0] if len(populated) == 1 else pd.concat(populated, ignore_index=True)


def _quarters(count: int) -> str:
    return f"{count} quarter" if count == 1 else f"{count} quarters"


def _features_argv(source: str | None, qc_max_flag: int) -> list[str]:
    """`--qc-max-flag` always recorded: two tables built at different strictness
    are otherwise indistinguishable from the manifest."""
    return [*_argv(source), f"--qc-max-flag={qc_max_flag}"]


def _report_features(
    run: RunManifest, zones: Zones, written: tuple[Path, ...], *, dry_run: bool
) -> None:
    built = [series for series in run.series if series.source != COMPARISON_SERIES]
    for series in built:
        span = f"{series.first_quarter}..{series.last_quarter}" if series.quarters else "-"
        # A kelp series is a polygon and has no parameter. `canopy` names what it
        # measures, so both halves line up in one column without the report
        # having to pretend a polygon is a site.
        what = series.polygon_id or series.site_id or "-"
        measured = series.parameter or "canopy"
        click.echo(
            f"{what:>22}  {measured:<22}  {_quarters(series.quarters or 0)}  "
            f"{series.quarters_usable} usable  {span}"
        )
    for warning in run.warnings:
        click.echo(f"     warning  {warning}")
    for gap in run.gaps:
        click.echo(f"         gap  {gap}")

    quarters = sum(series.quarters or 0 for series in built)
    usable = sum(series.quarters_usable or 0 for series in built)
    click.echo(f"\n{len(built)} series built -- {_quarters(quarters)}, {usable} usable")
    for series in run.series:
        if series.source == COMPARISON_SERIES:
            click.echo(f"comparison: {series.rows} rows over {series.quarters} polygon-quarters")
    for path in written:
        click.echo(f"wrote: {path}")

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
# Kelp Watch exports: landed, never normalized into observations
# --------------------------------------------------------------------------


def _ingest_kelpwatch(*, path: Path | None, data_root: Path | None, dry_run: bool) -> None:
    """Land every dropped export whose polygon the registry claims (docs/02).

    Two things make this unlike the other file-drop source. It writes nothing
    into `observations/`, because a canopy value belongs to a polygon and that
    zone is keyed on `site_id`. And the file's identity is not in the file: a
    Kelp Watch export names the geometry it describes nowhere, so which polygon
    it belongs to comes from `polygons.geojson` by filename, and an export the
    registry does not claim is quarantined rather than attributed by guesswork
    (hard rule 5).

    The run refuses outright if the registry pins no dataset revision. That is
    not fail-soft, and deliberately so: the export carries no version of its own,
    so a landing made without one could never be traced to a citable dataset
    afterwards, and "whatever was current that day" would have become the source
    of record (docs/02).
    """
    zones = Zones.at(data_root)
    polygons = load_polygons(zones.polygons_geojson)
    if polygons.kelp_watch is None:
        raise SystemExit(
            f"{zones.polygons_geojson} pins no `kelp_watch.revision`; a Kelp Watch export "
            "carries no version of its own, so a landing without one could never be traced "
            "to a citable dataset. Record the revision the export's citation names (docs/02)."
        )

    inputs = _discover(path or zones.raw_source(kelpwatch.SOURCE) / INCOMING)
    if not inputs:
        click.echo(f"nothing to ingest for {kelpwatch.SOURCE!r}")
        return

    run = RunManifest.start(
        "ingest",
        argv=[f"--source={kelpwatch.SOURCE}"],
        sources=[kelpwatch.SOURCE],
    )
    for candidate in inputs:
        _ingest_export(candidate, zones=zones, polygons=polygons, run=run, dry_run=dry_run)

    _report(run, zones, dry_run=dry_run)


def _ingest_export(
    path: Path, *, zones: Zones, polygons: Polygons, run: RunManifest, dry_run: bool
) -> None:
    """One export, start to finish. Never raises (docs/02 fail-soft rule)."""
    if not kelpwatch.sniff(path):
        run.add_file(path, "skipped", reason="not a Kelp Watch export (header does not match)")
        return

    entry = run.add_file(path, "failed", fetcher=kelpwatch.FETCHER_NAME)
    entry.dataset_revision = polygons.kelp_watch.revision

    polygon = polygons.for_file(path.name)
    if polygon is None:
        entry.outcome = "quarantined"
        entry.reason = (
            f"no polygon in {polygons.path} declares source_file {path.name!r}; "
            "the export says nothing about which geometry it describes"
        )
        entry.quarantined_to = _as_text(_quarantine(path, zones, dry_run=dry_run))
        return
    entry.polygon_id = polygon.polygon_id

    try:
        parsed = kelpwatch.parse(path, polygon)
        entry.rows_in = parsed.rows_in
        entry.rows_out = len(parsed.frame)
        # Recorded on the entry but not promoted to a run-level warning. Both
        # fire on every well-formed export -- every file has `max` rows and most
        # have cloud gaps -- and a warning that always fires stops being read.
        # What belongs at run level is the gap below, which is an upstream hole.
        entry.warnings.extend(parsed.warnings)
        if parsed.quarters_unobserved:
            run.note_gap(
                f"{kelpwatch.SOURCE}: {polygon.polygon_id}: {parsed.quarters_unobserved} "
                f"quarter(s) with no cloud-free observation, stored as null"
            )

        entry.landed = _as_text(_land_export(path, zones, polygons, polygon, dry_run=dry_run))
        entry.outcome = "ingested"
    except Exception as error:  # noqa: BLE001 -- one bad export must not end the run
        entry.outcome = "failed"
        entry.reason = f"{type(error).__name__}: {error}"
        run.note_warning(f"{path.name}: {entry.reason}")


def _land_export(path: Path, zones: Zones, polygons: Polygons, polygon, *, dry_run: bool) -> Path:
    """Copy the export into `raw/kelpwatch/{revision}/{polygon}/`, content-addressed.

    Segregated by revision, because a newer revision may revise history as well
    as extend it -- so two revisions must never be read as one series, and the
    directory is what makes mixing them impossible rather than merely discouraged.

    Content-addressed and never overwritten, like every other landing:
    re-dropping identical bytes is a no-op, and two different exports of one
    polygon at one revision cannot collide on a name.
    """
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    target = (
        zones.raw_source(kelpwatch.SOURCE)
        / polygons.kelp_watch.label
        / polygon.polygon_id.replace(":", "_")
        / f"{digest}__{path.name}"
    )
    if dry_run or target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target


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
    # One session for the whole run rather than one per window: a nineteen-year
    # backfill would otherwise open nineteen TLS connections to say the same
    # thing. Built here rather than in the fetcher so the tests keep driving the
    # same seam they already do.
    session = _session()
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
                session=session,
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
    session=None,
    dry_run: bool,
) -> None:
    """One station, one window. Never raises (docs/02 fail-soft rule).

    Three outcomes besides success, told apart deliberately. A source that did
    not answer is `skipped` and recorded as a manifest gap -- docs/01 s5 requires
    a missing NDBC month not to block the rest of a run, so it does not set the
    exit code. A source that says we already hold this version is `unchanged`,
    which is not a gap and not a failure: the bytes are in `raw/` and the rows
    are in `observations/`, so there is nothing left to do. Anything else is
    `failed`, which does set the code: a payload that arrived and could not be
    parsed is a bug or a format change, and both need a human.

    Re-parsing landed bytes after a parser or registry change is `rebuild`'s job,
    not this one's -- which is what makes skipping an unchanged window correct
    rather than merely cheap.
    """
    label = f"{site.station_code} {year if year is not None else 'realtime'}"
    entry = run.add_file(label, "failed", fetcher=getattr(fetcher, "FETCHER_NAME", source))
    entry.site_id = site.site_id

    url = (
        fetcher.archive_url(site.station_code, year)
        if year is not None
        else fetcher.realtime_url(site.station_code)
    )
    entry.path = url

    try:
        # What a previous run recorded about this URL, if anything. Looked up
        # here rather than inside the fetcher, which docs/02 restricts to its own
        # raw zone and which has no other use for storage.
        validators = cache.validators_for(zones, url)
        payload = (
            fetcher.fetch_archive(site.station_code, year, session=session, validators=validators)
            if year is not None
            else fetcher.fetch_realtime(site.station_code, session=session, validators=validators)
        )
    except NotModified:
        entry.outcome = "unchanged"
        entry.reason = "the source says this version is already ingested"
        return
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
            measured_parameters=site.measured_parameters,
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

        # Last, and only on success. The stored validator means "this URL was
        # fully ingested at this version", so recording it any earlier -- at the
        # landing, say -- would let the next run step past a window whose rows
        # never made it out of the parser.
        if not dry_run:
            cache.remember(zones, url, etag=payload.etag, last_modified=payload.last_modified)

    except Exception as error:  # noqa: BLE001 -- one window must not end the run
        entry.outcome = "failed"
        entry.reason = f"{type(error).__name__}: {error}"
        run.note_warning(f"{label}: {entry.reason}")


def _session():
    """One `requests.Session` for a run, or None if requests is not installed.

    None is what every fetcher already treats as "make your own", so a missing
    import costs a connection per window rather than the run -- and the offline
    tests, which pass their own fake, never reach this.
    """
    try:
        import requests
    except ImportError:  # pragma: no cover -- requests is a declared dependency
        return None
    return requests.Session()


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

    Two families. `QUARANTINE_CHECKS` is docs/06 s5 check 4 in both its halves:
    a deployment record must exist for the serial, and its series map must name
    something this file actually carries. Its order is owned by the adapter
    contract rather than repeated here, so a new blocking check reaches this
    caller by being declared once.

    The timezone cross-check joins them because the normalizer places timestamps
    using a fixed offset taken from the registry zone: if the header disagrees
    with that zone, or the deployment straddles a DST transition HOBOconnect
    handles in an unverified way (docs/06 s6), the honest answer is that we do
    not know the offset.

    Everything else -- a statistics mismatch, a cadence gap, a renamed file, a
    single unmapped series on a multi-series logger -- is recorded and ingested.
    Those are facts about the data, not reasons to refuse it.
    """
    for name in QUARANTINE_CHECKS:
        check = report.check(name)
        if check is not None and check.status == "fail":
            return check

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

    # Warnings and gaps were manifest-only here, while `qc` and `features` both
    # printed them. An upstream hole an operator has to open a JSON file to find
    # is one they will not find -- and for Kelp Watch the hole *is* the result.
    for warning in run.warnings:
        click.echo(f"     warning  {warning}")
    for gap in run.gaps:
        click.echo(f"         gap  {gap}")

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
