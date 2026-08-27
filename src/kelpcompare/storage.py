"""The three data zones and the writers into them (docs/03).

Every zone path in the project comes from `Zones`; every write to
`observations/` goes through `write_observations` and every write to `features/`
through `write_features`. All take a root, defaulting to `data/` under the cwd
like `registry.DEFAULT_REGISTRY_PATH`, so tests drive the real code against a
`tmp_path` instead of a mock -- and never touch the append-only raw zone
(CLAUDE.md hard rule 1).

The features writer lives here rather than in `kelpcompare.features` so the
staging-name-then-rename technique that makes a write atomic has exactly one
home, and a second zone cannot drift from the first about what "never
half-written" means.

Two invariants are enforced here rather than trusted:

* `validate_frame` rejects anything that is not exactly the docs/03 observation
  schema with a timezone-aware UTC timestamp. This is the last place a naive or
  local timestamp could slip into storage (hard rule 2).
* `_dedupe` collapses the overlap that readouts of a running logger produce by
  design (docs/06 s5 check 5). Deterministically: newest `fetch_run_id` wins,
  and run ids sort chronologically by construction (see `manifest.new_run_id`).

Timestamps are stored tz-naive UTC. The column is UTC by invariant, and a naive
column reads back as a plain DuckDB TIMESTAMP that displays as UTC everywhere,
whereas a tz-aware one becomes TIMESTAMPTZ and renders in the reader's session
timezone -- local time at presentation is exactly what docs/03 forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

DEFAULT_ROOT = Path("data")

#: The docs/03 `observations` table, in column order.
OBSERVATION_COLUMNS = (
    "timestamp",
    "site_id",
    "parameter",
    "value",
    "depth_m",
    "qc_flag",
    "qc_tests",
    "source",
    "fetch_run_id",
)

#: Storage dtypes for everything except `timestamp`, which `validate_frame` owns.
OBSERVATION_DTYPES = {
    "site_id": "string",
    "parameter": "string",
    "value": "float64",
    "depth_m": "float64",
    "qc_flag": "int8",
    "qc_tests": "string",
    "source": "string",
    "fetch_run_id": "string",
}

#: docs/03 `qc_flag`: the QARTOD roll-up stored on every row. Part of the schema,
#: so it lives here rather than in the normalizer that first writes it or the qc
#: stage that later re-derives it -- both need exactly the same vocabulary.
FLAG_PASS = 1
FLAG_NOT_EVALUATED = 2
FLAG_SUSPECT = 3
FLAG_FAIL = 4
FLAG_MISSING = 9

#: The one test ingest can decide, named in the docs/03 `qc_tests` description
#: (docs/06 s3). Here for the same reason: the stage that writes it and the stage
#: forbidden from relaxing it must agree on the spelling.
WINDOW_TEST = "deployment_window"

#: What makes an observation the same observation. Not `value`: a re-read of the
#: same instant is the same measurement even if a later export rounds it
#: differently, and not `qc_flag`, which is a judgement about the row.
OBSERVATION_KEY = ("site_id", "parameter", "timestamp", "depth_m")

#: The docs/03 `features/` tables this project writes, each one file rewritten
#: wholesale. Not partitioned: docs/03 names single files, the row count is in
#: the thousands, and a partitioned table could not stay a pure function of its
#: inputs -- which is what makes `rebuild` mean anything.
FEATURE_TABLES = (
    "quarterly_env",
    "climatology_env",
    "quarterly_kelp",
    "climatology_kelp",
    "comparison",
)


@dataclass(frozen=True)
class Zones:
    """The docs/03 directory layout, rooted anywhere.

    `quarantine/` is a zone too: docs/06 s5 check 4 has to put a rejected file
    somewhere, and it must not be `raw/`, which is the record of what we chose to
    trust.
    """

    root: Path = DEFAULT_ROOT

    @classmethod
    def at(cls, root: Path | str | None = None) -> Zones:
        return cls(Path(root) if root is not None else DEFAULT_ROOT)

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def observations(self) -> Path:
        return self.root / "observations"

    @property
    def features(self) -> Path:
        return self.root / "features"

    @property
    def registry(self) -> Path:
        return self.root / "registry"

    @property
    def quarantine(self) -> Path:
        return self.root / "quarantine"

    @property
    def manifests(self) -> Path:
        return self.raw / "_manifests"

    @property
    def sites_json(self) -> Path:
        return self.registry / "sites.json"

    @property
    def parameters_json(self) -> Path:
        return self.registry / "parameters.json"

    @property
    def features_json(self) -> Path:
        return self.registry / "features.json"

    @property
    def polygons_geojson(self) -> Path:
        return self.registry / "polygons.geojson"

    def raw_source(self, source: str) -> Path:
        """The raw landing zone for one source.

        Note the deliberate asymmetry for project sensors: the docs/03 source
        vocabulary calls them `project`, the raw directory is `project_sensors/`.
        """
        return self.raw / source

    def partition(self, source: str, year: int) -> Path:
        return self.observations / f"source={source}" / f"year={year}"

    def feature_table(self, table: str) -> Path:
        return self.features / f"{table}.parquet"


def validate_frame(frame: pd.DataFrame) -> None:
    """Refuse anything that is not the docs/03 schema. Raises, never coerces."""
    columns = tuple(frame.columns)
    if columns != OBSERVATION_COLUMNS:
        missing = [c for c in OBSERVATION_COLUMNS if c not in columns]
        extra = [c for c in columns if c not in OBSERVATION_COLUMNS]
        problem = f"missing {missing}" if missing else ""
        problem += (" and " if problem and extra else "") + (f"unexpected {extra}" if extra else "")
        raise ValueError(
            f"not the docs/03 observation schema: {problem or 'columns out of order'}; "
            f"expected {list(OBSERVATION_COLUMNS)}, got {list(columns)}"
        )

    # A naive dtype has no `tz` at all, so one test covers both "not tz-aware"
    # and "tz-aware but not UTC" -- the last point either could reach storage.
    dtype = frame["timestamp"].dtype
    tz = getattr(dtype, "tz", None)
    if tz is None or str(tz) != "UTC":
        raise ValueError(
            f"'timestamp' must be timezone-aware UTC before storage (hard rule 2), got {dtype}"
        )


def write_observations(
    frame: pd.DataFrame, zones: Zones, *, source: str, run_id: str
) -> tuple[Path, ...]:
    """Write observation rows into `observations/source=/year=/`, deduped.

    Returns the partition files written. An empty frame writes nothing and
    returns `()` -- a source with no rows this run is normal, not an error.
    """
    validate_frame(frame)
    if frame.empty:
        return ()

    staged = frame.copy()
    staged["timestamp"] = staged["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None)

    written = [
        _write_partition(part, zones, source=source, year=int(year), run_id=run_id)
        for year, part in staged.groupby(staged["timestamp"].dt.year, sort=True)
    ]
    return tuple(written)


def read_observations(zones: Zones, source: str | None = None) -> pd.DataFrame:
    """Read a zone back as one frame, deduped per partition.

    Analysis reads through DuckDB (ADR-001); this exists so a test can assert on
    what actually landed without reimplementing the layout.

    A partition should hold exactly one part file, but the write that leaves it
    that way is not atomic: `_write_partition` creates the new file and only then
    removes the ones it supersedes, so an interrupted run -- or an `unlink` that
    fails because a reader still holds the file open -- can leave both behind.
    Those two files overlap by construction, the newer being a rewrite of the
    older, so reading them naively returns every row twice. That is how a doubled
    series reaches the QARTOD tests, which read a row's neighbours and therefore
    come back with different verdicts than the same data produces on its own.

    Deduping per partition rather than across the zone is deliberate:
    `OBSERVATION_KEY` does not include `source`, so a zone-wide pass could
    collapse two sources' rows if they ever agreed on site, parameter, time and
    depth. Within one partition the source is fixed by the directory name.
    """
    pattern = f"source={source}" if source else "source=*"
    files = sorted(zones.observations.glob(f"{pattern}/year=*/part-*.parquet"))
    if not files:
        return pd.DataFrame(columns=list(OBSERVATION_COLUMNS))

    # `sorted` puts each partition's files in run-id order, which is chronological
    # by construction (`manifest.new_run_id`), so `_dedupe` keeping the last of an
    # equal-`fetch_run_id` group keeps the most recently written copy -- the same
    # rule, and the same reliance on stable ordering, that `_write_partition`
    # applies on the way in.
    partitions: dict[Path, list[pd.DataFrame]] = {}
    for path in files:
        partitions.setdefault(path.parent, []).append(pd.read_parquet(path))

    return pd.concat(
        [_dedupe(pd.concat(group, ignore_index=True)) for group in partitions.values()],
        ignore_index=True,
    )


def stored_sources(zones: Zones) -> tuple[str, ...]:
    """Every source with rows in the zone, from the partition layout itself.

    So that a whole-zone command can ask what is there rather than being told,
    and so the `source=` naming stays a fact about this module (docs/03).
    """
    if not zones.observations.exists():
        return ()
    return tuple(
        sorted(
            path.name.removeprefix("source=")
            for path in zones.observations.glob("source=*")
            if path.is_dir()
        )
    )


def read_features(zones: Zones, table: str) -> pd.DataFrame:
    """Read one `features/` table back, or an empty frame if it has never been built."""
    path = zones.feature_table(_known_table(table))
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def write_features(
    frame: pd.DataFrame,
    zones: Zones,
    *,
    table: str,
    key: tuple[str, ...],
    replacing: tuple[str, ...],
) -> Path:
    """Write one `features/` table, superseding the rows of the named sources.

    `replacing` is the set of sources this run rebuilt, and every existing row
    belonging to one of them is dropped before the new rows go in. It has no
    default on purpose: a write that superseded nothing would silently double
    the table.

    Source-scoped rather than wholesale, because a `--source ndbc` rerun after a
    single station's backfill must not be silent data loss for every other
    source. Scoped by *source* rather than merged row by row, because a site
    later removed from the registry would otherwise keep its feature rows
    forever -- rebuilding its source is what retires them.

    The retained rows are reindexed onto the incoming frame's columns, so the
    table's schema always follows the current feature configuration: a threshold
    retuned since the last run renames its column, and a source not yet rebuilt
    shows null there rather than the old column lingering beside the new one.

    Written under a staging name and moved into place, exactly as
    `_write_partition` does and for the same reason: an interrupted run leaves
    the previous table intact rather than a truncated Parquet that every later
    read raises on. The staging name deliberately does not match the table's, so
    a leftover is invisible to a DuckDB query written against the zone.
    """
    path = zones.feature_table(_known_table(table))
    retained = _retained(read_features(zones, table), replacing, frame.columns)
    merged = pd.concat([retained, frame], ignore_index=True) if len(retained) else frame

    ordered = merged.sort_values(list(key), kind="stable", na_position="last")
    _write_table(ordered.reset_index(drop=True).astype(frame.dtypes.to_dict()), path)
    return path


def replace_features(
    frame: pd.DataFrame, zones: Zones, *, table: str, key: tuple[str, ...]
) -> Path:
    """Write one `features/` table wholesale, superseding whatever was there.

    The counterpart to `write_features`, for a table that is a pure function of
    other tables rather than of one source's rows. `comparison` is the case: it
    is a join of both quarterly tables and the polygon registry, so there is no
    source to scope a replacement by, and merging into it would let a pair
    dropped from the registry keep its rows forever.

    A separate function rather than a `replacing=None` mode on `write_features`,
    because "supersede these sources" and "supersede everything" are different
    enough that a caller should have to say which one it means -- the same
    reason `replacing` has no default there.
    """
    path = zones.feature_table(_known_table(table))
    ordered = frame.sort_values(list(key), kind="stable", na_position="last")
    _write_table(ordered.reset_index(drop=True).astype(frame.dtypes.to_dict()), path)
    return path


def _write_table(typed: pd.DataFrame, path: Path) -> None:
    """Stage, then move into place -- so a table is never seen half-written.

    The staging name deliberately does not match the table's, so a leftover from
    a crashed run is invisible to a DuckDB query written against the zone.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".{path.name}.writing"
    typed.to_parquet(staging, index=False)
    staging.replace(path)


def _retained(stored: pd.DataFrame, replacing: tuple[str, ...], columns) -> pd.DataFrame:
    if stored.empty or "source" not in stored.columns:
        return stored
    return stored.loc[~stored["source"].isin(replacing)].reindex(columns=list(columns))


def _known_table(table: str) -> str:
    if table not in FEATURE_TABLES:
        raise ValueError(
            f"{table!r} is not a docs/03 features table; known: {list(FEATURE_TABLES)}"
        )
    return table


def _write_partition(
    part: pd.DataFrame, zones: Zones, *, source: str, year: int, run_id: str
) -> Path:
    """Rewrite one partition wholesale: read what is there, merge, dedupe, write.

    Wholesale rewrite rather than append because ADR-001 chose a store with no
    row-level updates -- zones rebuild, they do not mutate -- and because a
    duplicate can only be resolved against the rows already on disk.

    The new file is written under a temporary name and moved into place, so a
    part file is never seen half-written: an interrupted write leaves the
    partition exactly as it was rather than leaving a truncated Parquet that
    every later read raises on. What it cannot make atomic is the pair of steps
    -- a crash between the move and the drop leaves the superseded file behind,
    which is why `read_observations` dedupes per partition rather than trusting
    the count.

    The staging name deliberately does not match `part-*.parquet`: a leftover
    from a crashed run must be invisible to every reader of this zone, including
    a DuckDB query written against the glob.
    """
    directory = zones.partition(source, year)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"part-{run_id}.parquet"
    existing = sorted(directory.glob("part-*.parquet"))

    # Incoming rows go last and `_dedupe` sorts stably, which is what makes them
    # win. Not `fetch_run_id`: qc rewrites the zone preserving it (docs/03), so
    # both copies of a rewritten row carry the same one and it cannot break the
    # tie. Reorder either and qc writes back the flags it just replaced.
    frames = [pd.read_parquet(path) for path in existing]
    frames.append(part)
    merged = _dedupe(pd.concat(frames, ignore_index=True))

    staging = directory / f".{target.name}.writing"
    merged.astype(OBSERVATION_DTYPES).to_parquet(staging, index=False)
    staging.replace(target)

    for stale in existing:
        if stale != target:
            stale.unlink()
    return target


def _dedupe(frame: pd.DataFrame) -> pd.DataFrame:
    """Newest run wins, then order by time. Same inputs, same file, every time."""
    ordered = frame.sort_values("fetch_run_id", kind="stable")
    deduped = ordered.drop_duplicates(subset=list(OBSERVATION_KEY), keep="last")
    return deduped.sort_values(["timestamp", "site_id", "parameter"], kind="stable").reset_index(
        drop=True
    )
