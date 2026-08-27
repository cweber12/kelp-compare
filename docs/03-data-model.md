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
    quarterly_env.parquet     # built
    climatology_env.parquet   # built
    quarterly_kelp.parquet    # planned
    comparison.parquet        # planned
  quarantine/               # files the registry gate turned away (doc 06 §5)
  registry/
    sites.json  parameters.json  features.json
    polygons.geojson  station_map.json
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

**A part file is never half-written.** The new file is written under a staging
name that deliberately does not match `part-*.parquet` — so a leftover from a
crashed run is invisible to every reader of this zone, DuckDB globs included —
and then moved into place. An interrupted write leaves the partition exactly as
it was, rather than leaving a truncated Parquet that every later read raises on.

**A partition can still transiently hold more than one file.** The move and the
drop are two steps, not one: the new file lands and only then are the ones it
supersedes removed, so a crash between them — or a drop that fails because a
reader still holds the file open — leaves both behind. They overlap completely,
the newer being a rewrite of the older, so anything that reads a partition must
dedupe what it finds rather than assume one file: `storage.read_observations`
does, per partition and by the same rule the write applies, which is what keeps
a doubled series out of the QARTOD tests
(doc 04 §1 — they read a row's neighbours, so a duplicated row changes the
verdict on its neighbours too).

**A DuckDB query over the raw `part-*.parquet` glob does not get this**, and
would count those rows twice. The condition is self-healing — the next write to
that partition merges the strays away — but a notebook that queries the zone
directly between an interrupted run and the next one is reading duplicates.
Until analysis reads through a helper that dedupes, check for a partition
holding more than one file before trusting a query written against a zone whose
last run did not finish cleanly.

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
recorded as a non-answer. Both neighbour-reading tests do this: the spike test
at the two ends of a series and either side of a gap, the rate-of-change test at
the first row and wherever the preceding row's value is absent. A rate compared
against nothing is not a pass.

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
| `sensor_depths_m` | For public stations: `{parameter: depth}`, positive down. What a fetcher writes into `depth_m` |
| `measured_parameters` | For public stations: the controlled parameters this station carries an instrument for. A fetcher stores only these |
| `same_platform_as[]` | Other `site_id`s that are the same physical instrument package under another provider's identifier |

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

Public stations carry no `deployments[]`. What they need instead is the
identifier their provider knows them by and the geometry of their sensors:
`sensor_depths_m` is where a water-temperature intake depth is declared, and it
is what the fetcher writes into each row's `depth_m`. A parameter absent from
that map gets a null depth — correct for a met parameter, and equally correct
for a water parameter whose depth the provider has not published. Neither is
guessed.

`measured_parameters` is a separate field for that exact reason: absence from
`sensor_depths_m` means "no depth published", never "no sensor", so the two
cannot be one map. It records what the station carries an instrument for, and a
fetcher stores only those — which is what stops a fixed-column source format
landing rows for a sensor the station does not have (doc 02, "A station stores
only what it has a sensor for"). An **absent** `measured_parameters` means
undeclared, not empty: the fetcher stores everything it recognises and the run
warns, because an unrecorded fact must not quietly become missing data.

`same_platform_as` records that two site records describe one instrument
package. `NDBC:LJAC1` and `COOPS:9410230` are the same NOS platform, NDBC
redistributing the NOS observations, and the doc 04 neighbor validation must not
count them as two independent references for the same sensor.

## Analysis polygon registry: `polygons.geojson`

**Implemented** — `src/kelpcompare/polygons.py`. A fourth registry file, holding
the areas the analysis compares kelp over: the polygons drawn around sensor
sites, the control polygons away from them, and the wider regional beds. A
polygon is not a site — it has no instrument, no timezone and no deployment —
which is why the kelp half of the features zone is keyed on `polygon_id` and not
on `site_id`.

The geometry lives in the repository so that *which water a number describes* is
a reviewable data change, diffable line by line, rather than a drawing made in a
browser session nobody can reproduce (doc 01 §2 requires regenerating everything
from raw with one command).

One GeoJSON `Feature` per polygon. Properties:

| Property | Notes |
|----------|-------|
| `polygon_id` | Required, non-empty. The key every kelp and comparison row joins on |
| `purpose` | Required, one of `near_site`, `control`, `regional` |
| `site_ids[]` | Required, non-empty. The sites this polygon is compared against |
| `name`, `notes` | Optional human labels |

Geometry must be a `Polygon` or a `MultiPolygon` — a point or a line has no
interior for a pixel centroid to fall inside. `_`-prefixed properties are
comments, as in `features.json`, and are ignored.

**Declared in WGS84, and said so out loud.** GeoJSON is WGS84 by definition
(RFC 7946), so a file carrying the superseded 2008-draft `crs` member naming
anything else is **refused rather than reprojected**: a silent CRS mismatch
shifts a polygon by hundreds of metres, which against a 30 m Landsat pixel is
tens of pixels of the wrong ocean, and the resulting series looks exactly like a
correct one. The loaded frame carries `EPSG:4326` explicitly.

The parser takes the same posture as the parameter and feature registries:
**refuse rather than ignore**, naming the file and the offending polygon. An
unknown purpose, an unknown property, a missing or repeated `polygon_id`, an
empty `site_ids`, and a geometry that is null, empty, of the wrong kind or
self-intersecting all raise. That strictness is not stylistic — none of those
failures is visible downstream. A malformed polygon does not produce an error;
it aggregates the wrong pixels, or none, and produces a plausible series.

What the loader deliberately does **not** check is that each `site_ids` entry
exists in `sites.json`. No registry loader here makes a cross-file claim —
`neighbor_refs` has the same property — and a typo there costs the comparison
table the rows for that pair rather than producing wrong ones, so it belongs to
the stage that builds them.

An **empty** `FeatureCollection` loads cleanly and yields no polygons. That is
the state the repository ships in and it is not an error: a project with
environmental data and no polygons drawn yet is a project mid-way through.
Adding a polygon is a `data(registry)` change needing no code.

## Kelp series

`quarterly_kelp.parquet`:

| Column | Notes |
|--------|-------|
| `polygon_id` | FK to polygons |
| `year`, `quarter` | Kelp Watch calendar (Q1=Jan–Mar … Q4=Oct–Dec) |
| `canopy_area_m2` | Null means no valid observation (clouds), **not** zero |
| `n_valid` / coverage metric | Whatever the export provides about observation quality |
| `canopy_anom` | Anomaly vs. that polygon's quarterly climatology |

## Quarterly environmental features: `quarterly_env.parquet`

One row per **`source × site_id × parameter × depth_m × year × quarter`** —
the QC series key (doc 04 §1) plus time, so every feature row traces to
exactly one QC series. `depth_m` is in the key because a shallow and a deep
logger at one site are not one series, and averaging them across a
thermocline would corrupt precisely the quarterly minimum and cold-day
counts §2 of doc 04 makes the nitrate proxy. Quarters are assigned in **UTC**
(hard rule 2), with the consequence doc 04 §2 states.

The table is wide and sparse: one row carries whichever features its
`feature_set` defines and null elsewhere. `feature_set` is a column rather
than something to infer from which columns happen to be null, so
"not applicable" is readable off the row.

| Column | Type | Notes |
|--------|------|-------|
| `source`, `site_id`, `parameter`, `depth_m` | — | The QC series key |
| `year`, `quarter` | INT | Kelp Watch calendar, UTC |
| `feature_set` | VARCHAR | Which set was applied: `statistics`, `temperature` |
| `n_obs` | BIGINT | Rows kept by the QC filter |
| `n_days_observed` | INT | Distinct UTC days with at least one kept row |
| `cadence_s` | DOUBLE | Median observed inter-sample interval; null under two rows |
| `expected_obs` | DOUBLE | Quarter duration ÷ `cadence_s` |
| `pct_coverage` | DOUBLE | `n_obs / expected_obs`, clamped to 1 |
| `usable` | BOOLEAN | `n_obs ≥ 2` and `pct_coverage ≥` the configured floor |
| `quarter_complete` | BOOLEAN | Whether the quarter had ended when the run happened |
| `qc_max_flag` | TINYINT | The filter the table was built at (default 2) |
| measured features | DOUBLE | See below; counts are doubles so null is representable |
| `max_spell_above_{t}_gap_interrupted` | BOOLEAN | Whether a gap ended the longest spell |
| `baseline_years` | INT | Contributing quarters behind this row's anomalies |
| `{feature}_anom` | DOUBLE | One per measured feature |

Measured features under the default configuration: `mean`, `min`, `max`,
`p05`, `p95`, `variance` for every parameter, plus — for the `temperature`
set — `days_above_20c`, `days_above_23c`, `max_spell_above_20c_days`,
`degree_days_above_18c`, `days_below_14c`. **Those column names are derived
from the configured thresholds**, so retuning one renames its column rather
than silently changing what an existing column means; a fractional threshold
renders its decimal point as an underscore (`20.5 → 20_5c`). Doc 04 §2
defines what each one counts.

**Coverage is measured against the series' own cadence**, so an hourly
station and a 10-minute logger are judged on the same scale without either
being penalised for its sampling rate. `n_obs`, `cadence_s` and
`expected_obs` are stored beside the fraction so it is auditable rather than
a bare number to be trusted. Two consequences are handled rather than left
to emerge: a series whose cadence changed mid-quarter can compute above full
coverage, so the value is clamped and the series is named in a manifest
warning; and a quarter with fewer than two observations has no interval to
take a median of, so its cadence is null, its coverage zero, and it is
unusable.

Coverage counts the same QC-filtered rows the features are computed from — a
quarter that sampled perfectly and failed QC on every row scores **zero**
coverage, not full. Quarters are enumerated from stored rows and computed
from filtered ones, so such a quarter still gets a row, at `n_obs = 0`. A
quarter with no stored rows at all gets no row.

**A quarter below the floor is flagged, never imputed and never dropped** —
hard rule 4's discipline applied one layer up, which also leaves the floor a
sensitivity knob rather than a filter already applied. `usable` is the single
gate: anomalies are computed for unusable quarters too, because two
mechanisms expressing one warning is worse than one.

`quarter_complete` exists because an in-progress quarter is otherwise
indistinguishable from a station outage: both present as under-covered, for
entirely different reasons. The coverage denominator stays the full quarter,
so an unfinished quarter is honestly under-covered; incomplete quarters are
excluded from the climatology regardless of coverage.

Wave and water-level feature sets (doc 04 §2: `n_events_hs_above_3m`,
`max_event_hours`, mean observed-minus-predicted) are not built — their
fetchers do not exist, and the configuration parser refuses to declare them
until they do.

## Quarterly climatology: `climatology_env.parquet`

The fixed baseline the `_anom` columns were taken against, written to its own
table so that "the anomalies did not shift" is checkable by diffing two runs
rather than a promise. One row per `source × site_id × parameter × depth_m ×
quarter × feature` — long on features, because the table is a lookup keyed by
feature and a wide form would need a `_mean` and `_std` column for every
feature in `quarterly_env`.

| Column | Notes |
|--------|-------|
| `source`, `site_id`, `parameter`, `depth_m` | The QC series key |
| `quarter` | Quarter of the year, 1–4; **not** a year-quarter |
| `feature` | Which measured feature this baseline is for |
| `baseline_start_year`, `baseline_end_year` | The window applied, inclusive |
| `n_years` | Contributing years for this feature |
| `baseline_mean`, `baseline_std` | Sample convention; `std` null below two years |

`baseline_std` is here so a standardised anomaly is a join rather than a
second recomputation that could drift from the one that produced the
anomalies. `n_years` is per feature and is what gates each anomaly;
`baseline_years` on the feature row is the series-level count of contributing
quarters, which is the same number except where a feature is null in an
otherwise-usable quarter.

Only **usable, complete** quarters inside the window contribute. A cell with
no contributing year gets no row — an empty row would claim a baseline
exists. Doc 04 §3 records the window and why it is what it is.

## Feature configuration: `features.json`

The coverage floor, the climatology baseline, and the per-parameter feature
set and its thresholds live in `registry/features.json`, not in code and not
in `parameters.json` (ADR-006). A parameter **declares** which feature set
applies to it; it is never inferred from the parameter's unit, for the reason
this document already gives for parameters themselves — `degC` is equally
`sea_water_temperature` and `air_temperature`, and only one of them gets kelp
stress thresholds.

```json
"policy": {
  "coverage_floor": 0.6,
  "baseline": {"start_year": 2007, "end_year": 2019, "min_years": 10}
},
"parameters": {
  "sea_water_temperature": {
    "feature_set": "temperature",
    "thresholds": {
      "days_above": [20.0, 23.0], "max_spell_above": [20.0],
      "degree_days_above": [18.0], "days_below": [14.0]
    }
  },
  "air_temperature": {"feature_set": "statistics"}
}
```

The parser takes the same posture as the parameter registry: an unknown key,
an empty block, thresholds on a set that takes none, or a feature set the
builder does not implement all raise, naming the file and the key. A
parameter with **no** entry is skipped, named in a manifest warning, and sets
the run's exit code.

### Writing the features zone

One file per table, rewritten wholesale — this zone is not partitioned, the
row count is in the thousands, and a partitioned table could not stay a pure
function of its inputs, which is what makes `rebuild` mean anything. The file
is written under a staging name and moved into place, exactly as a partition
is, so an interrupted run leaves the previous table intact.

A run replaces exactly the rows of the sources it built. Scoped by source
rather than merged row by row, so a site later removed from the registry does
not keep its feature rows forever; scoped rather than wholesale, so a
`--source` rerun is not silent data loss for every other source. A source
that *failed* mid-run keeps its previous rows rather than losing them to a
run that never looked at it — the cost is that a source whose observations
have been removed entirely keeps stale feature rows until the zone is
rebuilt.

Retained rows are reindexed onto the incoming frame's columns, so the table's
schema always follows the current configuration: a threshold retuned since
the last run renames its column, and a source not yet rebuilt shows null
there rather than the old column lingering beside the new one.

## Comparison table: `comparison.parquet`

The analysis-ready join, one row per
`polygon_id × site_id × year × quarter × lag` for lags 0–4 quarters:
kelp anomaly at t against environmental feature anomalies at t−lag, plus
event covariates for the lagged quarter (marine heatwave days, ENSO
state, wave events). Notebooks and the dashboard read this table almost
exclusively; it is regenerated wholesale by `kelpcompare features`.

Not built, along with `quarterly_kelp.parquet`: both are blocked on a Kelp
Watch fetcher that does not exist and on analysis polygons that have not been
drawn, so a comparison table today would be a join against an empty one.
`kelpcompare features` currently builds the environmental half of the zone.

## Run manifests

Every ingest/QC/feature run writes
`raw/_manifests/{run_id}.json`: command, code version (git SHA), sources
touched, date windows, row counts in/out, QC flag histogram, warnings, and
upstream gaps encountered. Each input records either an `adapter` or a
`fetcher`, never both — how a row was obtained is part of its provenance, and a
pulled station-window has no adapter to name. Manifests are how any number in a notebook
traces back to specific fetches — required for publication-grade
reproducibility.

The per-series entry is shared between the stages that read a zone rather than
recorded as files, because such a run has no input files. Each fills the fields
it has: `qc` the flag histogram and the tests it ran, `features` the quarters
produced, the quarters usable, and the first and last quarter covered — which
is what makes coverage attrition readable without opening the Parquet.

## Integrity rules

Raw zone is append-only. `observations` is rebuildable from raw;
`features` and `comparison` are rebuildable from observations; a single
`kelpcompare rebuild` regenerates derived zones from scratch. All joins go
through registry keys — no string-matching station names in analysis code.
Timestamps UTC everywhere; local time exists only at presentation.
