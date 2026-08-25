"""The three data zones and the observation writer (docs/03).

Every zone path in the project comes from `Zones`, and every write to
`observations/` goes through `write_observations`. Both take a root, defaulting
to `data/` under the cwd like `registry.DEFAULT_REGISTRY_PATH`, so tests drive
the real code against a `tmp_path` instead of a mock -- and never touch the
append-only raw zone (CLAUDE.md hard rule 1).

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

    def raw_source(self, source: str) -> Path:
        """The raw landing zone for one source.

        Note the deliberate asymmetry for project sensors: the docs/03 source
        vocabulary calls them `project`, the raw directory is `project_sensors/`.
        """
        return self.raw / source

    def partition(self, source: str, year: int) -> Path:
        return self.observations / f"source={source}" / f"year={year}"


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
    """Read a zone back as one frame. For tests and small local inspection.

    Analysis reads through DuckDB (ADR-001); this exists so a test can assert on
    what actually landed without reimplementing the layout.
    """
    pattern = f"source={source}" if source else "source=*"
    files = sorted(zones.observations.glob(f"{pattern}/year=*/part-*.parquet"))
    if not files:
        return pd.DataFrame(columns=list(OBSERVATION_COLUMNS))
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


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


def _write_partition(
    part: pd.DataFrame, zones: Zones, *, source: str, year: int, run_id: str
) -> Path:
    """Rewrite one partition wholesale: read what is there, merge, dedupe, write.

    Wholesale rewrite rather than append because ADR-001 chose a store with no
    row-level updates -- zones rebuild, they do not mutate -- and because a
    duplicate can only be resolved against the rows already on disk.
    """
    directory = zones.partition(source, year)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"part-{run_id}.parquet"
    existing = sorted(directory.glob("part-*.parquet"))

    frames = [pd.read_parquet(path) for path in existing]
    frames.append(part)
    merged = _dedupe(pd.concat(frames, ignore_index=True))
    merged.astype(OBSERVATION_DTYPES).to_parquet(target, index=False)

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
