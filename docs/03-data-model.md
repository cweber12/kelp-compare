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
    kelpwatch/              # incoming/ then ver{n}/{polygon_id}/{digest}__{name}
    ndbc/  coops/  sccoos/  cdip/  gis/  project_sensors/
    sd_rtoms/               # source={name} for every pulled source
    sio_shore_stations/     # incoming/ then {archived}/{digest}__{name}
    _manifests/             # one JSON manifest per ingest run
  observations/             # partitioned: source={name}/year={yyyy}/part-{run_id}.parquet
  features/
    quarterly_env.parquet     climatology_env.parquet
    quarterly_kelp.parquet    climatology_kelp.parquet
    comparison.parquet
  quarantine/               # files the registry gate turned away (doc 06 §5)
  cache/                    # NOT a zone — see below. Deleting it is always safe
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

### `cache/` is not a zone

`data/cache/` holds one file, `http-validators.json`: the `ETag` and
`Last-Modified` each pulled URL was last fully ingested at, so a re-run can ask
whether anything changed instead of downloading it again (doc 02).

**It is a cache, not a record, and nothing about the project depends on it.** No
derived table reads it, no number traces through it, and deleting it costs
exactly one re-download. It is excluded from the reproducibility argument
entirely: `rebuild` neither reads nor writes it, and a `data/` directory restored
without it produces identical output at a slightly higher bandwidth bill.

That is why it is not in `raw/`. Hard rule 1 makes that zone append-only and
reserves it for landings and manifests, and a mutable lookup table is neither.
Nor is it in the run manifests: those are write-only audit records, and reading
them here would make a deleted manifest change *behaviour* rather than merely
lose history.

Every failure reading it is soft — absent, truncated, unparseable, or written by
a format this version does not know all mean "know nothing about this URL", and
cost a download. A cache that can fail a run is worse than no cache.

### Partition files and idempotence

One file per partition per run, named `part-{run_id}.parquet`. A write
rewrites its partition wholesale — read what is there, merge, dedupe, write,
drop the superseded file — because ADR-001 chose a store with no row-level
updates. Duplicate rows are collapsed on
`(site_id, parameter, timestamp, depth_m)` with the newest `fetch_run_id`
winning; run IDs sort chronologically by construction, which is what makes
the tie-break deterministic. That is how overlapping readouts of a running
logger (doc 06 §5 check 5) resolve to one row per measurement.

**Two of the four key fields come from the registry, which makes both immutable
once rows have landed.** `site_id` and `depth_m` are read from the deployment
record, not from the instrument's file, so editing either in `sites.json` —
renaming a site, or filling a null depth — makes the re-ingested rows key
differently from the ones already on disk. They collide with nothing, so nothing
is superseded and nothing raises: both copies survive in the partition, and the
features layer builds them as two series for one instrument — the stale one
under a `site_id` the registry no longer contains, which no join through the
registry can resolve. Treat a landed `site_id` or `depth_m` as one-way until
`rebuild` exists to repair it.

**A window correction is not in this class**, and is safe. `window_local` sets
the `deployment_window` verdict and so moves `qc_flag`, which is not part of the
key; re-dropping the file supersedes the rows in place. The distinction matters
because deployment notes reach for the same phrase for both.

**A key field typed wrong is refused rather than landed.** The merge dedupes on
the key *before* it normalises types, so a `depth_m` carried as the string
`"8.23"` would key differently from the `8.23` already on disk and leave both
copies in the file — the duplication a raw glob reader would then count twice.
`storage.validate_frame` therefore checks the dtype of `site_id`, `parameter` and
`depth_m` — and of `value` — before a partition is read or written, so a depth
that is not a number is refused at the boundary instead of aborting the run
inside the writer's cast, after the fetch, parse, normalize and QC work is
already done. It is types that are checked, not whether a field is populated: a
`depth_m` null on every row is the documented shape for a met parameter and
passes, and so does a column that is a depth for one parameter and null for
another.

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
| `source` | VARCHAR | `kelpwatch, ndbc, coops, sccoos, cdip, project, sd_rtoms, sio_shore_stations, oisst, ...` |
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
| `archive` | Hand-downloaded stations only: which snapshot the landings came from, and how to cite it. See below |
| `sensor_depths_m` | For public stations: `{parameter: depth}`, positive down. A number is what the fetcher writes into `depth_m`; a **list** means the payload carries the depth and these are the ones seen so far |
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

### A hand-downloaded station pins the archive it was downloaded from

`station_code` is enough for a station a fetcher can ask for: the identifier is
the version, because the provider serves whatever is current. A station that can
only be **downloaded by hand** has no such guarantee — what arrives is one
cumulative snapshot of the whole record, and the next one may revise it. So the
site record pins which snapshot the landings came from, in an `archive` block:

```json
"archive": {
  "archived": "2026-06-30",
  "source_file": "LaJolla_TEMP_1916-202603.csv",
  "doi": "10.6075/J06T0K0M",
  "citation": "Carter, Melissa L.; ... Award# C22820005."
}
```

This is the Kelp Watch revision pin (doc 02, "The revision is pinned in the
registry") applied to a site rather than to a polygon, and it earns its place
for the same reason: **an ingest refuses to run without one**, because a landing
made against "whatever was on the site that day" can never be traced to a
citable dataset afterwards. `archived` is the landing directory, so
`raw/{source}/{archived}/` keeps two archives from interleaving the way
`raw/kelpwatch/ver{n}/` does.

Two differences from Kelp Watch, both in this source's favour. The file
**declares its own archive date** in its header, so the pin is checked at ingest
and a file from another snapshot is quarantined rather than silently landed
under the wrong pin. And `citation` is carried here because the funding award in
the citation text changes between snapshots, which makes the citation a property
of the pinned archive rather than of the program.

`source_file` is the name the archive arrives under. Unlike a Kelp Watch export
— which says nothing at all about which geometry it describes — this file names
its own station and position in its header, so the filename is recorded as
provenance rather than used to decide what the file is.

### A source may be self-describing on depth

The paragraph above assumes one depth per parameter, which a shore station
satisfies and a **moored string does not**. A mooring measures one parameter at
many depths at once — City of San Diego RTOMS carries `sea_water_temperature`
at eleven depths off Point Loma — so there is no single value for
`sensor_depths_m` to hold and no way for the registry to supply the depth at
all. The depth is on the payload, per row.

So `sensor_depths_m` takes two forms, and the form *is* the distinction:

| Declared as | Meaning | `depth_for` | `declared_depths` |
|---|---|---|---|
| a number, `3.4` | the registry supplies the depth | `3.4` | `(3.4,)` |
| a list, `[1.0, 10.0]` | the payload supplies it; these are the depths seen | `None` | `(1.0, 10.0)` |
| absent | undeclared | `None` | `()` |

`depth_for` returning `None` for the list form is load-bearing rather than
incidental. A fetcher that fell back to it would write one depth for every
sensor on the string, and because `depth_m` is part of the observation key that
collapses eleven series into one — silently, and not fixably by a later run
(see "Partition files and idempotence" above). `describes_own_depth` is how a
fetcher asks which world it is in, and it must read the depth from the payload
when the answer is yes.

What the registry still owes the reader in the list form is **which depths
anyone has looked at**. That is what the list is: not a value the fetcher
consumes, but a declaration a fetcher checks its payload against, so a mooring
that comes back from a refit with a sensor at a new depth is reported rather
than landed as a twelfth series nobody has seen. It is the depth-side
counterpart of `measured_parameters`, and an absent list means undeclared for
the same reason — the fetcher stores what arrives and the run warns.

A nominal depth that drifts between deployments is **two depths, not one**.
RTOMS reports 9 m on one deployment and 10 m on the next for what is
physically the same position on the string; both are declared, both land, and
the record for that position is split at the deployment boundary. Rounding them
together would write a depth the mooring never reported into a field the schema
treats as ground truth, permanently — see doc 02, "City of San Diego RTOMS".

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
| `source_file` | Required. The Kelp Watch export this polygon's rows arrive in |
| `name`, `notes` | Optional human labels |

`source_file` is the registry gate for this source. A Kelp Watch export names
the geometry it describes **nowhere in the file** (doc 02) — only in its
filename — so the registry is the only thing that can say which polygon a
dropped CSV belongs to, and one nothing claims is quarantined rather than
attributed by guesswork (hard rule 5). It is a bare filename, matched
case-insensitively; where the operator drops the file is not a registry fact.
Getting it wrong is silent: it attributes one bed's forty years to another
polygon, and nothing downstream looks wrong.

`site_ids` is required and non-empty for a related reason. A polygon paired
with no site produces no comparison row at all, so it would sit in the registry
looking like coverage while vanishing from the analysis.

**Geometry is optional and is provenance, not an input.** The export arrives
already summed over the geometry selected in the Kelp Watch UI, so no number in
this project is computed from these outlines. What still needs them is the doc
04 §4.5 distance-decay test, and that is the stage that should refuse a polygon
without one. A polygon whose outline has not been recorded declares
`"geometry": null` and says so; the `geometry` member itself is required,
because omitting it is an unfinished edit while null is a statement.

A geometry that is *present and wrong* is still refused — a point or a line
(no extent), an empty ring, or a ring that crosses itself. "Not drawn yet" and
"drawn wrong" are different facts and must not collapse into one. Drawn
geometry must be a `Polygon` or a `MultiPolygon`. `_`-prefixed properties are
comments, as in `features.json`, and are ignored.

### The pinned dataset revision

The file carries one top-level `kelp_watch` member:

```json
"kelp_watch": {
  "revision": 23,
  "doi": "10.6073/pasta/2c1218b7ebe6967da52000adf02f6a8b"
}
```

The CSV export carries no version of any kind, so this is the only place the
provenance chain from a figure back to a citable dataset can be closed, and
`kelpcompare ingest --source kelpwatch` **refuses to run without it**. That
refusal is deliberately not fail-soft: a landing made without a revision could
never be traced afterwards, and "whatever was current that day" would have
become the source of record.

One revision for the whole registry rather than one per polygon. A newer
revision may revise history as well as extend it — the upstream product
recalibrates between sensors and fills scan-line-corrector gaps — so two
revisions must never be read as one series. Pinning it once means bumping it
obsoletes every landing at the old revision at the same moment, which is loud:
a bed not re-exported produces no rows rather than quietly contributing stale
ones. Landings are segregated by revision on disk, so mixing them is impossible
rather than merely discouraged.

Absent is allowed at load time and reported; it is the ingest that refuses,
because that is the moment a revision would otherwise be invented.

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

## Quarterly kelp: `quarterly_kelp.parquet`

**Implemented** — `src/kelpcompare/features/kelp.py`. One row per
**`source × polygon_id × year × quarter`**, on the same UTC Kelp Watch calendar as the
environmental half. There is no aggregation stage of the kind `quarterly_env`
has: a Kelp Watch export is already one row per quarter, summed over the
geometry selected in the UI (doc 02). What this table adds is the bookkeeping
that makes such a row interpretable and the anomalies that make it comparable.

| Column | Type | Notes |
|--------|------|-------|
| `source` | VARCHAR | Which route the numbers took; `kelpwatch` today |
| `polygon_id` | VARCHAR | FK to `polygons.geojson` |
| `year`, `quarter` | INT | Kelp Watch calendar, UTC |
| `kelp_area_m2` | DOUBLE | Emergent canopy area. **Null means no cloud-free observation, never zero** |
| `n_cells_kelp` | DOUBLE | 30 m cells holding canopy; null wherever the value is |
| `n_cells_observed` | INT | Cells with a cloud-free observation this quarter |
| `n_cells` | INT | The bed's historic footprint — cells that held canopy at least once |
| `pct_cells_observed` | DOUBLE | `n_cells_observed / n_cells` |
| `usable` | BOOLEAN | `n_cells_observed > 0` and `pct_cells_observed ≥` the configured floor |
| `quarter_complete` | BOOLEAN | Whether the quarter had ended when the run happened |
| `kelp_watch_revision` | INT | The upstream dataset revision this row came from |
| `baseline_years` | INT | Contributing years behind this row's anomalies |
| `kelp_area_m2_anom`, `n_cells_kelp_anom` | DOUBLE | One per measured quantity |

**Two measured quantities, both with anomalies.** Area is how much canopy there
was; the cell count is how far it spread. A bed can thin without shrinking and
shrink without thinning, so the notebook chooses which answers its question
rather than this stage choosing. The UI export carries no species split and no
biomass (doc 02), so those are absent rather than null.

**Coverage is the fraction of the bed that was seen**, and it is not the same
kind of number as `pct_coverage` on the environmental side even though it plays
the same role. `n_cells_observed` and `n_cells` are stored beside the fraction
so it is auditable rather than trusted, exactly as `n_obs` and `expected_obs`
are.

**A partially observed quarter is biased low, not merely noisier** — and this
is the one place the two halves genuinely differ. `kelp_area_m2` is a *sum over
the cells that were seen*, so a quarter with two thirds of its bed under cloud
reports roughly two thirds of the canopy that was there. Nothing corrects for
it: scaling by the observed fraction would assume the unseen part of a bed looks
like the seen part, which is exactly what a patchy bed does not do. The quarter
is flagged `usable = false` below the floor and keeps its value. Doc 04 §2
carries the disclosure.

**The coverage floor is the environmental one**, shared rather than duplicated.
It answers the same question on both halves — how much of the thing was actually
observed — and a second knob with no separate evidence behind it would be a knob
nobody could tune. It stays a sensitivity knob either way, since the value
survives the flag.

**`source` is in the series key**, as it is on the environmental side. It is
what the features zone scopes a replacement by, so a table without it cannot be
superseded and would grow by one build's worth of rows every run — and if the
published data package ever becomes a second route to the same polygons, the two
must not merge into one baseline. `write_features` refuses a source-scoped write
of a table that has no `source` column, so this cannot be got wrong quietly
again.

**No `fetch_run_id`.** On an observation row that records which *fetch* landed
it and survives every later rewrite; on a derived table it would be the build
run, which changes on every build and would stop two runs over unchanged inputs
writing the same bytes. `quarterly_env` carries none for the same reason, and
the manifest already records what each run produced.

Two rows for one polygon-quarter **raise**. That can only mean two exports were
read as one series, and averaging them would produce a plausible number from an
incoherent input.

## Quarterly kelp climatology: `climatology_kelp.parquet`

The same table `climatology_env` is, keyed on `source × polygon_id × quarter ×
feature` instead of on the QC series key, and produced by the same code — see "One
climatology implementation, two series keys" below. Same columns, same fixed
window, same rule that only usable and complete quarters contribute.

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

### One climatology implementation, two series keys

The climatology and anomaly code is **generic over the series key**. The caller
passes which columns identify one series and which columns are measured
features; everything else — the fixed window, the contributor rule, the
minimum-years gate, the `_anom` twins — is shared. The environmental half passes
`source, site_id, parameter, depth_m` and the feature set `features.json`
declares; the kelp half passes `polygon_id` and the canopy quantities.

That is a correctness requirement rather than a tidiness one. Kelp and
environmental anomalies sit on the two sides of every correlation the project
computes, so two implementations could diverge and the divergence would present
as a *result*. Sharing the code makes "both sides were treated the same way" a
fact about the program rather than a claim about two of them.

The only columns the shared code requires of either table are the series key,
`year`, `quarter`, `usable` and `quarter_complete`. A series key naming a
column the table does not carry **raises**, rather than producing an empty
climatology that would read as "no baseline yet".

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
`--source` rerun is not silent data loss for every other source. `comparison`
is the exception and is written **wholesale**: it is a pure function of the two
quarterly tables and the polygon registry, so there is no source to scope it by,
and merging into it would let a retired pair keep its rows. A source
that *failed* mid-run keeps its previous rows rather than losing them to a
run that never looked at it — the cost is that a source whose observations
have been removed entirely keeps stale feature rows until the zone is
rebuilt.

Retained rows are reindexed onto the incoming frame's columns, so the table's
schema always follows the current configuration: a threshold retuned since
the last run renames its column, and a source not yet rebuilt shows null
there rather than the old column lingering beside the new one.

## Comparison table: `comparison.parquet`

**Implemented** — `src/kelpcompare/features/comparison.py`. The analysis-ready
join, and the table notebooks and the dashboard read almost exclusively.

One row per **`polygon_id × kelp_source × env_source × site_id × parameter ×
depth_m × year × quarter × lag`**, for lags 0–4 quarters. Earlier drafts of this document keyed
it on `polygon_id × site_id × year × quarter × lag`; that cannot represent a
site carrying several parameters, or one parameter at two depths, and every
station does. The key gained the environmental series key rather than the table
gaining a column per parameter.

| Column | Type | Notes |
|--------|------|-------|
| `polygon_id`, `kelp_source` | VARCHAR | The kelp series key |
| `env_source`, `site_id`, `parameter`, `depth_m` | — | The environmental series key |
| `year`, `quarter` | INT | The **kelp** quarter, *t* |
| `lag` | TINYINT | 0–4 quarters |
| `env_year`, `env_quarter` | INT | *t − lag*, recorded so the lag is auditable from the row |
| `kelp_usable`, `env_usable` | BOOLEAN | Both sides' flags; `env_usable` is null where no environmental row was found |
| `kelp_watch_revision` | INT | Closes the provenance chain in the table figures are made from |
| `{kelp feature}_anom` | DOUBLE | The kelp anomalies at *t* |
| `{env feature}_anom` | DOUBLE | The environmental anomalies at *t − lag*, under the names `quarterly_env` gives them |

**The lag has one direction and it is written down: the environment leads, kelp
responds.** Lag 2 on a 2015Q3 row is kelp in 2015Q3 against the water in
2015Q1. Getting this backwards raises nothing — it produces a correlation matrix
that reads as kelp predicting temperature, which is a *result* rather than an
error. `env_year` and `env_quarter` are on the row so a reviewer can check it by
reading one, and the direction is asserted in the tests on a hand-built pair.

**A row exists wherever kelp does.** The response variable defines the row and
the environment is joined onto it, so a lag reaching back before the
environmental record produces nulls on that side rather than a missing row. That
is what makes "the environmental record does not reach this quarter" a queryable
fact rather than an absence to be inferred. On today's data most of the table is
of that kind: kelp begins in 1984 and the LJAC1 archive in 2007.

**Both usability flags are carried and nothing is filtered.** `usable` stays the
single gate, applied once by the analysis, rather than becoming a hidden
deletion here. A reader who filters on both gets the same answer this stage
would have given, and can see what filtering cost.

**Which polygon pairs with which site comes from `polygons.geojson`**, so no
analysis code string-matches a polygon name against a station name. The *series*
come from `quarterly_env` rather than from the registry, because the registry
names sites and a site carries several series — inventing a pair for a parameter
nobody measured would fill the table with rows that can never be anything but
null.

**Regenerated wholesale** by `kelpcompare features`, from the two quarterly
tables as they stand on disk rather than from the run's own outcomes. A
`--source ndbc` rerun must still reflect every polygon beside it, and reading
the zone is what makes the table a function of the zone rather than of the last
run's arguments. Wholesale rather than source-scoped, because a pair the
registry no longer declares must lose its rows rather than keep them forever.

Event covariates for the lagged quarter — marine heatwave days, ENSO state, wave
events (doc 04 §2) — are not present. Each needs an external source ingested
first.

## Neighbor validation: `validation.parquet`

**Implemented** — `src/kelpcompare/features/validation.py`, written by
`kelpcompare validate`. The standing table doc 04 §1 asks for: the evidence base
for the claim that a project sensor is trustworthy.

One row per **`site_id × serial × deployment_number × parameter × depth_m ×
reference_site_id × reference_depth_m`**. A deployment compared against two
references is two rows; a reference carrying one parameter at two depths is two
rows, because agreement at 0.5 m says nothing about agreement at 5 m.

| Column | Type | Notes |
|--------|------|-------|
| `site_id`, `serial`, `deployment_number` | VARCHAR, VARCHAR, INT | Which instrument, in which deployment |
| `parameter`, `depth_m` | VARCHAR, DOUBLE | The deployment's series |
| `reference_site_id`, `reference_depth_m` | VARCHAR, DOUBLE | The reference series; depth null for a met parameter |
| `source`, `reference_source` | VARCHAR | The observation `source` each side was read from |
| `depth_gap_m` | DOUBLE | `abs(depth_m − reference_depth_m)`; null when either depth is |
| `depth_comparable` | BOOLEAN | Whether the gap is within the configured tolerance |
| `cadence_s` | INT | The bin width both sides were reduced to before comparing |
| `n_pairs` | INT | Bins in which both sides have an observation |
| `overlap_start`, `overlap_end` | TIMESTAMP | UTC bounds of the compared window |
| `correlation` | DOUBLE | Pearson *r*. Always reported |
| `bias`, `rmse` | DOUBLE | Deployment minus reference. **Null unless `depth_comparable`** |
| `collapsed_refs` | VARCHAR | Same-platform references folded into this row, `;`-joined |
| `qc_max_flag` | TINYINT | The strictness both sides were filtered at |

**Bias and RMSE are null against a reference at another depth, and that is a
refusal rather than a gap in the data.** Doc 04 §1 sets the rule: below the
thermocline the depth offset *is* most of the signal, so a bias computed across
one measures stratification and prints it as instrument error. Correlation
survives, because both series still track the same synoptic forcing, and it is
reported with `depth_gap_m` beside it so it is never read as agreement.

**Both sides are binned to a common cadence before comparing.** The bin is the
coarser of the two series' median native intervals — a 10-minute logger against
an hourly station is compared hourly — and each side contributes the mean of its
observations in the bin. Bins where either side has nothing are not pairs.

Two alternatives were rejected. *Joining on exact timestamps* discards five of
every six samples when a 10-minute logger meets an hourly station, and reports
an `n_pairs` that describes the calendar rather than the overlap. *Resampling
both to a fixed cadence* makes the number chosen here rather than by the data,
and would either upsample a daily record or throw away a logger's resolution.

The rejected third option is worth naming because it is not available: **nearest-
neighbour matching within a tolerance** would pair every logger sample with the
same hourly reading several times over, which inflates `n_pairs` and makes the
correlation a statement about the tolerance.

**Where one side is a grab sample the bin mean is not like for like.** A daily
bottle at 10:38 against a day's mean of a continuous logger are different
quantities, and nothing here corrects for it. `cadence_s` and `n_pairs` are on
the row so a reader can see which comparison they are looking at.

**Same-platform references are folded, never counted twice.** `NDBC:LJAC1` and
`COOPS:9410230` are one NOS package under two identifiers, and doc 04 §1 says
they must not count as two independent references. The first named in
`neighbor_refs` that has rows produces the row; the rest are listed in
`collapsed_refs`, so the fold is visible in the table rather than inferred from
its absence.

**Regenerated wholesale** by `kelpcompare validate`, from the observations zone
and the registry as they stand. Wholesale rather than source-scoped, because a
pair the registry no longer declares must lose its row rather than keep it
forever — the same argument as `comparison`, and it is written through
`replace_features` for the same reason.

## Run manifests

Every ingest/QC/feature run writes
`raw/_manifests/{run_id}.json`: command, code version (git SHA), sources
touched, date windows, row counts in/out, QC flag histogram, warnings, and
upstream gaps encountered. An input's outcome is one of `ingested`,
`unchanged` (a pulled window the source says we already hold — doc 02),
`quarantined`, `skipped` (an outage or a file nothing recognised, and the only
outcome that also notes a gap), or `failed` (the only one that sets the exit
code). An input records either a `site_id` or a
`polygon_id`, never both — an observation belongs to a site and a canopy value
belongs to a polygon, and calling a polygon a site to save a field would put a
lie in the audit trail. `dataset_revision` records the upstream version a
landing came from, for the one source whose file carries no version of its own.
Each input records either an `adapter` or a
`fetcher`, never both — how a row was obtained is part of its provenance, and a
pulled station-window has no adapter to name. Manifests are how any number in a notebook
traces back to specific fetches — required for publication-grade
reproducibility.

The per-series entry is shared between the stages that read a zone rather than
recorded as files, because such a run has no input files. Each fills the fields
it has: `qc` the flag histogram and the tests it ran, `features` the quarters
produced, the quarters usable, and the first and last quarter covered — which
is what makes coverage attrition readable without opening the Parquet.

A **kelp** series is a polygon rather than a site and has no parameter or depth,
so it fills `polygon_id` and leaves those empty — the same alternative the file
entry draws. It also fills `quarters_observed` beside `quarters_usable`, because
a bed can be fully observed and mostly unusable, or the reverse, and one number
cannot say which.

## Integrity rules

Raw zone is append-only. `observations` is rebuildable from raw; `features`
and `comparison` are rebuildable from **raw and observations** — the kelp half
is built from the Kelp Watch landings plus `polygons.geojson` and never passes
through `observations`, because a canopy value belongs to a polygon and that
zone is keyed on `site_id`. A single `kelpcompare rebuild` is meant to regenerate
derived zones from scratch, and **is not implemented yet** — the command exits
with a message
(https://github.com/cweber12/kelp-compare/issues/54). Until it is, the rebuildability
above is a property of the design rather than an available operation, which is what
makes a landed `site_id` or `depth_m` one-way. All joins go
through registry keys — no string-matching station names in analysis code.
Timestamps UTC everywhere; local time exists only at presentation.
