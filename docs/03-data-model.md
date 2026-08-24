# 03 — Data Model

**Status:** Draft for review

Storage is a three-zone local lakehouse: `raw/` (immutable landings),
`observations/` (normalized long-format Parquet), `features/` (quarterly
aggregates and comparison tables). DuckDB queries Parquet in place; GeoJSON
plus geopandas handles geometry. Everything below `raw/` is derived and
rebuildable.

## Directory layout

```
data/
  raw/                      # immutable; exactly what the source sent
    kelpwatch/  ndbc/  coops/  sccoos/  cdip/  gis/  project_sensors/
    _manifests/             # one JSON manifest per ingest run
  observations/             # partitioned: source={name}/year={yyyy}/part-{run_id}.parquet
  features/
    quarterly_env.parquet
    quarterly_kelp.parquet
    comparison.parquet
  quarantine/               # files the registry gate turned away (doc 06 §5)
  registry/
    sites.json  parameters.json  polygons.geojson  station_map.json
```

`quarantine/` is deliberately outside `raw/`: raw is the record of what the
project chose to trust and keep forever, and a file with no deployment
record has not earned that. It stays in the operator's drop directory too,
so fixing the registry and re-running picks it up (doc 06 §5 check 4).

`station_map.json` is planned, not yet created — the pinned source
identifiers currently live on the site records in `sites.json`.

### Partition files and idempotence

One file per partition per run, named `part-{run_id}.parquet`. A write
rewrites its partition wholesale — read what is there, merge, dedupe, write,
drop the superseded file — because ADR-001 chose a store with no row-level
updates. Duplicate rows are collapsed on
`(site_id, parameter, timestamp, depth_m)` with the newest `fetch_run_id`
winning; run IDs sort chronologically by construction, which is what makes
the tie-break deterministic. That is how overlapping readouts of a running
logger (doc 06 §5 check 5) resolve to one row per measurement.

Timestamps are stored **tz-naive UTC**. The column is UTC by invariant, and
a naive column reads back as a plain DuckDB `TIMESTAMP` that displays as UTC
for every reader, whereas a tz-aware one becomes `TIMESTAMPTZ` and renders
in the reader's session timezone — presentation-time local time leaking into
storage, which the integrity rules below forbid. The conversion happens at
the storage boundary only; everything upstream of it carries an explicit
UTC-aware timestamp so the invariant is machine-checkable rather than
assumed.

## Core table: `observations`

Long format — one row per measurement. Long beats wide here because sources
report different parameter sets at different cadences, and a new sensor type
becomes new rows, not a schema migration.

| Column | Type | Notes |
|--------|------|-------|
| `timestamp` | TIMESTAMP (UTC) | Measurement time, always UTC |
| `site_id` | VARCHAR | FK to site registry (e.g. `NDBC:LJAC1`, `PROJ:PT-01`) |
| `parameter` | VARCHAR | Controlled vocabulary (CF-style), see below |
| `value` | DOUBLE | SI units per parameter registry |
| `depth_m` | DOUBLE | Positive down; null for met parameters |
| `qc_flag` | TINYINT | QARTOD roll-up: 1 pass, 2 not eval, 3 suspect, 4 fail, 9 missing |
| `qc_tests` | VARCHAR | Compact per-test results for audit, `name:status` joined by `;` |
| `source` | VARCHAR | `kelpwatch, ndbc, coops, sccoos, cdip, project, oisst, ...` |
| `fetch_run_id` | VARCHAR | FK to run manifest |

Partitioned by `source` and `year(timestamp)`. Rule: rows are never
deleted for QC reasons — analysis queries filter on `qc_flag <= 2` (or
stricter) at read time.

Ingest writes `qc_flag = 2` (not evaluated) for rows it has no verdict on,
`9` for a row whose value is absent, and `4` for a project-sensor reading
outside its declared deployment window — the one test ingest can decide,
recorded as `deployment_window:fail` in `qc_tests` (doc 06 §3). The QARTOD
tests themselves run later, in `kelpcompare qc`, and roll up into the same
two columns.

### How the two QC columns are derived

`qc_tests` is the record; `qc_flag` is its summary. A row's flag is always the
roll-up of exactly the verdicts recorded beside it, and the two are written
together so they cannot drift apart. Statuses are `pass`, `suspect`, `fail`,
`missing`; a test that reached no verdict for a row is **omitted** rather than
recorded as a non-answer, which is what a spike test does at the two ends of a
series.

Roll-up precedence, applied across every recorded verdict:

| Flag | Wins when | Notes |
|------|-----------|-------|
| `9` missing | any test reports missing | Highest. There is nothing in an absent value to judge |
| `4` fail | any test fails | |
| `3` suspect | any test is suspect, none failed | Excluded by the default `qc_flag <= 2` filter |
| `1` pass | at least one test reached a verdict, all passed | |
| `2` not evaluated | no test reached a verdict | The state every row is ingested in |

Missing ranking highest is a deliberate divergence from
`ioos_qc.qartod_compare`, which ranks it lowest. A verdict about where the
instrument was must not turn a missing reading into a measurement that failed.
Every other step matches QARTOD.

`kelpcompare qc` **re-derives** the flag rather than upgrading it: it reads the
verdicts already stored, replaces only those of the tests it just ran, and rolls
the whole set up again. Two consequences worth stating plainly. A row failed for
being outside its deployment window stays at `4` however much a gross-range test
likes its value — a plausible temperature measured in air is still not a
measurement of the sea. And running `qc` twice over unchanged data is a no-op,
which is what makes the flag a function of the data and the registry rather than
of the order in which commands were run.

`qc` rewrites the `observations/` zone in place, preserving each row's
`fetch_run_id` so ingest provenance survives; the partition file is renamed to
the qc run that last touched it. One consequence follows from the partition
rules above: re-ingesting an export that overlaps already-flagged rows brings
those rows back in at `qc_flag = 2` with a newer `fetch_run_id`, and the newest
run wins. That is honest — they have not been evaluated since they were
re-ingested — and re-running `qc` restores them.

### Parameter vocabulary (initial)

`sea_water_temperature`, `air_temperature`, `water_level` (MLLW),
`wave_significant_height`, `wave_peak_period`, `wind_speed`,
`chlorophyll_concentration`, `hab_cell_count:{taxon}`. The parameter
registry (`parameters.json`) records canonical unit, valid range for QC
gross-range tests, and the source-column mappings. Adding a sensor type =
adding registry entries, no schema change.

**QC thresholds live here, not in code** (ADR-004). Each entry may carry a
`qc` block holding the per-test thresholds `kelpcompare qc` needs:

```json
"sea_water_temperature": {
  "unit": "degC",
  "valid_range": [5.0, 35.0],
  "qc": {
    "spike": {"suspect": 1.5, "fail": 3.0},
    "rate_of_change": {"suspect_per_hour": 18.0, "fail_per_hour": 36.0}
  }
}
```

Spike thresholds are in the parameter's own canonical unit. The rate keys
name their unit because the QARTOD implementation takes a rate per *second*,
and a °C/s threshold misread as °C/h is off by 3600 in the direction that
flags nothing. The gross-range test takes its fail span from `valid_range`
rather than repeating it under `qc`, so the hard bounds have one home; an
optional `gross_range.suspect_span` narrows it where there is evidence to.

**An absent `qc` block means those tests do not run for that parameter** —
gross range still does, from `valid_range`. Silence is a decision, and it is
recorded as one: a missing threshold never becomes a default guess, because a
guessed threshold that flags real data is indistinguishable in the stored
flags from a real QC failure.

## Site registry: `sites.json`

One record per station or deployment location.

| Field | Notes |
|-------|-------|
| `site_id` | Stable key, `{NAMESPACE}:{ID}` |
| `name`, `operator` | Human labels; operator ∈ {project, ndbc, coops, sccoos, cdip} |
| `lat`, `lon` | WGS84 |
| `deployments[]` | For project sensors: instrument model, serial, depth_m, start/end, calibration dates, clock-sync events |
| `neighbor_refs[]` | Ordered public stations used for validation of this site |
| `erddap_dataset_id` / `station_code` | Pinned source identifiers |

Each `deployments[]` record carries `tz`, `window_local` (the in-water
window, doc 06 §3), and `series_map` — the mapping from the vendor file's
sensor name to a controlled parameter, e.g.
`{"Tidbit 1": "sea_water_temperature"}`. The map lives on the deployment
rather than in `parameters.json` because vendor series names are a user
setting on the logger, so two teammates' exports of the same instrument
type can disagree. A series with no entry is reported and skipped, never
inferred from its unit — `degC` is equally `sea_water_temperature` and
`air_temperature`.

Deployment metadata is mandatory before project-sensor data is accepted;
this is enforced in the ingest CLI, not by convention.

## Kelp geometry and series

`polygons.geojson` holds the analysis polygons drawn around sensor sites
plus control polygons, each with `polygon_id`, purpose (`near_site`,
`control`, `regional`), and the `site_id`s it is associated with.
`quarterly_kelp.parquet`:

| Column | Notes |
|--------|-------|
| `polygon_id` | FK to polygons |
| `year`, `quarter` | Kelp Watch calendar (Q1=Jan–Mar … Q4=Oct–Dec) |
| `canopy_area_m2` | Null means no valid observation (clouds), **not** zero |
| `n_valid` / coverage metric | Whatever the export provides about observation quality |
| `canopy_anom` | Anomaly vs. that polygon's quarterly climatology |

## Quarterly environmental features: `quarterly_env.parquet`

One row per `site_id × parameter-family × year × quarter`, wide on
features because the feature set is fixed by doc 04:

`mean`, `min`, `max`, `p05`, `p95`, `variance`,
`days_above_20c`, `days_above_23c`, `max_spell_above_20c_days`,
`degree_days_above_18c`, `days_below_14c` (upwelling/nitrate proxy),
`n_obs`, `pct_coverage` (fraction of the quarter with valid data), plus
per-family extras (waves: `n_events_hs_above_3m`, `max_event_hours`).
Each feature also gets an `_anom` twin after climatology subtraction.
Rows with `pct_coverage` below a threshold (default 60%, tunable) are
flagged unusable so a half-empty quarter never poses as a mild one.

## Comparison table: `comparison.parquet`

The analysis-ready join, one row per
`polygon_id × site_id × year × quarter × lag` for lags 0–4 quarters:
kelp anomaly at t against environmental feature anomalies at t−lag, plus
event covariates for the lagged quarter (marine heatwave days, ENSO
state, wave events). Notebooks and the dashboard read this table almost
exclusively; it is regenerated wholesale by `kelpcompare features`.

## Run manifests

Every ingest/QC/feature run writes
`raw/_manifests/{run_id}.json`: command, code version (git SHA), sources
touched, date windows, row counts in/out, QC flag histogram, warnings, and
upstream gaps encountered. Manifests are how any number in a notebook
traces back to specific fetches — required for publication-grade
reproducibility.

## Integrity rules

Raw zone is append-only. `observations` is rebuildable from raw;
`features` and `comparison` are rebuildable from observations; a single
`kelpcompare rebuild` regenerates derived zones from scratch. All joins go
through registry keys — no string-matching station names in analysis code.
Timestamps UTC everywhere; local time exists only at presentation.
