# 02 — Data Source Catalog

**Status:** Draft for review

One entry per source. Each entry is the contract the fetcher module must
implement: where the data comes from, how it is accessed programmatically,
what arrives, and the quirks that will bite us if undocumented. Access
details should be re-verified against provider documentation during
implementation, since public endpoints and formats drift.

## Summary table

| Source | Role | Access | Native cadence | Format |
|--------|------|--------|----------------|--------|
| Kelp Watch | Baseline response variable | Hand-downloaded CSV export, dropped in `raw/kelpwatch/incoming/` | Quarterly | CSV |
| Project sensors | Primary predictors under evaluation | Local files from loggers | ~minutes | Vendor CSV |
| NDBC | Reference met/ocean observations | HTTPS text files | 6 min – 1 hr | Fixed-width text |
| NOAA CO-OPS | Water level, coastal water temp | REST API (JSON/CSV) | 6 min / hourly | JSON, CSV |
| SCCOOS / CalOOS | Shore stations, HABs, currents | ERDDAP (tabledap/griddap) | Varies | CSV, NetCDF, JSON |
| City of San Diego RTOMS | Depth-resolved reference temperature | CeNCOOS ERDDAP (tabledap) | 10 min | CSV |
| SIO Shore Stations | Century-scale reference temperature | Hand-downloaded CSV, dropped in `raw/sio_shore_stations/incoming/` | Daily | CSV |
| CDIP | Wave climate | THREDDS/ERDDAP NetCDF | 30 min | NetCDF |
| CDFW / marineBIOS | GIS context, historical kelp surveys | Downloaded shapefiles/services | Static / annual | Shapefile, GeoJSON |
| CNRA open data | Ecological context, independent canopy | CKAN portal downloads | Static (2011–2012) | CSV, PDF |
| Supplementary (SST, indices) | Gap-filling, regional drivers | ERDDAP / flat files | Daily / monthly | NetCDF, text |

## Kelp Watch

The response variable. Quarterly kelp canopy at 30 m resolution for the west
coast since 1984, aggregated to a selected geometry.

Everything below the next two subsections was verified against real exports on
2026-08-26; the files are recorded whole in `tests/fixtures/kelpwatch/`.

### The route: a hand-downloaded CSV export

**The operator selects a kelp bed on kelpwatch.org, exports its quarterly CSV,
and drops the file in `raw/kelpwatch/incoming/`.** This is a file-drop source
like the project sensors, not a pulled one — there is no fetcher, and
`ingest --source kelpwatch` lands the file and writes a manifest exactly as the
HOBO ingest does.

**There is a network path, though, and this section used to say there was not.**
kelpwatch.org's own download button calls a public, unauthenticated endpoint
that takes a geometry and returns the same CSV, and the site serves the
classified cells the CSV is summed from as vector tiles. Both are described
under "The cells behind the export" below. Nothing in the pipeline uses either
yet — whether a fetcher is built on them is
`https://github.com/cweber12/kelp-compare/issues/85`. They were used once, as
measurement instruments, to reconstruct and verify the bed outlines now recorded
in `polygons.geojson`.

This is a deliberate retreat from the published data package, and the reason is
access rather than preference. The source of record *ought* to be SBC LTER
`knb-lter-sbc.74` on the Environmental Data Initiative, which is the dataset the
kelpwatch.org platform is built on and which carries a revision number and a
DOI. **Every PASTA REST method returns HTTP 403 to an anonymous caller as of
2026-08-26** — reads and listing calls alike, and the denial is global rather
than specific to this package — because EDI has required authentication for all
API access since 2026-07-30, following a denial-of-service attack. The API
documentation at `pastaplus-core.readthedocs.io` still shows anonymous examples
and is stale on this point; do not take it as evidence the endpoint is open. A
credential cannot live in `data/registry/`, which is committed and public.

The cost of the retreat, stated rather than discovered:

| | the data package | this export |
|---|---|---|
| species | giant and bull kelp separately | **not distinguished** |
| biomass | giant kelp canopy biomass | **absent** |
| resolution | per 30 m pixel | already summed to the selected geometry |
| refresh | one command | a human, once a quarter |
| geometry | drawn in this repository | chosen in a browser session |

`quarterly_kelp` is therefore a narrower table than it would otherwise be, and
the polygon geometry recorded in `polygons.geojson` is provenance rather than an
input to any number. When an EDI account arrives this becomes a second route to
the same product rather than a replacement — the schema, the polygon registry
and the shared climatology are unchanged by which route the numbers took.

### The revision is pinned in the registry

The CSV carries no version of any kind, so the dataset revision is recorded in
`polygons.geojson` and an ingest refuses to run without one. It is the export's
own recommended-citation download that names it:

> Bell, T., K. Cavanaugh, D. Siegel. 2024. SBC LTER: Time series of quarterly
> NetCDF files of kelp biomass in the canopy from Landsat 5, 7 and 8, since 1984
> (ongoing) **ver 23**. Environmental Data Initiative.
> `https://doi.org/10.6073/pasta/2c1218b7ebe6967da52000adf02f6a8b`

Note that a newer revision may revise history as well as extend it — the
upstream product recalibrates between sensors and fills scan-line-corrector gaps
— so bumping it is a change to numbers already published, not only an append.
That is why it is a reviewable `data(registry)` change and why every kelp row
carries the revision it came from.

### Layout

One header line, LF endings, no preamble, six columns:

```
year,quarter,kelp_area_m2,count_cells_kelp,count_cells_no_clouds,count_cells_historic_footprint
1984,1,3663,13,8309,8309
1984,2,44812,306,8309,8309
1984,3,0,0,0,8309
1984,4,112905,314,5426,8309
1984,max,112905,314,8309,8309
```

- `kelp_area_m2` — emergent canopy area summed over the selected geometry. It is
  fractional-cover weighted, not `cells × 900`: a cell counted as kelp averages
  about 126 m² of the 900 m² it could hold, so the area column carries
  information the counts do not.
- `count_cells_kelp` — 30 m cells containing canopy this quarter.
- `count_cells_no_clouds` — cells in the footprint with a cloud-free
  observation. **The published field dictionary calls these cells "within the
  unoccupied kelp habitat"**, which would exclude the kelp-bearing ones. It does
  not: across both fixtures this count is never below `count_cells_kelp` and
  never above the footprint. Read it as the observed fraction of the whole.
- `count_cells_historic_footprint` — cells that held canopy at least once over
  the whole record, i.e. the maximum historical extent. Constant within a file,
  and the denominator every coverage fraction is taken against.

**There is no identifier for the selected geometry anywhere in the file.** Which
bed an export describes lives only in its filename, which is why
`polygons.geojson` declares the filename each polygon's export arrives under and
why a file the registry does not claim is quarantined rather than guessed at
(hard rule 5).

### A quarter nobody could see is written as a zero

**This is the quirk the whole parser turns on, and the published field
dictionary is wrong about it.** The dictionary says "cells with no numerical
value correspond to instances when the scene was either obstructed by clouds
and/or no clear observation of the area was available". There is not one blank
cell in either fixture. An unobserved quarter is written `0,0,0,<footprint>` —
identical to a genuine empty quarter in every column except
`count_cells_no_clouds`.

Read naively that fabricates a zero-canopy measurement, which is exactly what
hard rule 3 exists to prevent, and it fabricates them where they do most harm:

- Across the six beds exported, **44 quarters have no cloud-free observation and
  329 are genuinely empty** — every one of the 373 written as `0`.
- The blind quarters lean winter (9.1% of Q4 and 5.8% of Q1, against 0.8% of
  Q3), so fabricated zeros would not scatter. They would pile into the winter
  quarters and read as a seasonal signal, on top of the seasonal cycle doc 04 §3
  already removes.
- The damage is worst in a **marginal bed**. Del Mar is genuinely empty for 112
  of its 170 quarters and unobservable for 8 more; there, zero is the normal
  reading and eight invented ones would look like nothing at all.

The rule the parser applies: **`count_cells_no_clouds == 0` means the quarter
was not observed, and its value is null.** Checked rather than assumed — a
quarter with zero observed cells always reports zero area and zero kelp cells in
both fixtures, so nulling the value discards no measurement.

### The `max` row is derived, and is not a quarter

Every year but the last carries a fifth row whose `quarter` is the literal token
`max`. The dictionary describes it as the growing-season maximum, useful for
year-over-year comparison.

**It is a column-wise maximum, not the peak quarter's row.** La Jolla 1984 shows
why that matters: the `max` row reports 112,905 m² — Q4's area — beside 8,309
observed cells, which is Q1's and Q2's figure, not the 5,426 Q4 actually had. So
it cannot be read as "the best quarter" any more than as a fifth quarter.

It is skipped at parse and the skip is reported in the run manifest, in the same
posture the NDBC parser reports a token it read as missing. Ingesting it would
add a fifth quarter to the Kelp Watch calendar, double-count every year's peak,
and pull every climatology built from it upward.

Two smaller consequences of the same row: the last year carries no `max` row at
all while it is in progress, so the `max` rows cannot be used to enumerate the
years either; and the token makes `quarter` a text column on the way in, so it
must not be parsed as an integer before the row is dropped.

### The cells behind the export

Verified 2026-08-30. Everything here is a development-time finding used to
reconstruct the bed outlines; **no pipeline code calls either endpoint**, and
tests never reach the network.

**The aggregate endpoint takes our geometry.**

```
GET https://kelp-production-agg.kelpwatch.org/aggregate?geom=<WKT or GeoJSON>
```

Both geometry encodings are accepted. `start` / `end` / `source` are ignored
here — it always returns the whole 1984→present record, in the exact layout
above. They belong to the `/aggregate/id` upload path instead. Send a
`User-Agent` naming the project, per the cross-cutting rules at the end of this
document.

**The tiles carry the unaggregated cells.**

```
GET https://data-production.kelpwatch.org/california/latest.tiles/{z}/{x}/{y}.pbf
```

Mapbox Vector Tiles, tippecanoe-built, maxzoom 13, single layer `cog`. Despite
the `latest` in the path, a z13 tile carries the **whole quarterly record**, not
the current quarter: each feature is one 30 m cell whose properties are 170 keys
`1984_01` … `2026_02`, and whose value is that cell's canopy area in m² for that
quarter, or **`-1` for a quarter with no cloud-free observation**. That is the
same missing-versus-empty distinction the CSV hides inside
`count_cells_no_clouds`, made explicit per cell.

Four facts a reader of those tiles needs, each measured rather than assumed:

- **The cells are a UTM 11N grid at 30 m.** Cell corners land on a 30 m lattice
  in EPSG:32611 to within 0.9 m; in California Albers or Web Mercator they
  scatter across the full 30 m. So a cell has an exact integer grid index, which
  is what makes an inventory of them exact rather than approximate.
- **A cell straddling a tile boundary appears in both tiles**, clipped
  differently in each, so deduplicating by geometry or by centroid double-counts
  it. Snapping each fragment's centroid to the UTM grid index collapses the two
  onto one cell: 36,732 fragments over 324 tiles became 32,294 cells.
- **Every cell in the layer is a footprint cell.** All 32,294 have at least one
  quarter with canopy, which is the same population
  `count_cells_historic_footprint` counts.
- **The four aggregate columns are reproducible from the cells**, and this is
  the check that the tiles really are what the export is summed from. For a
  selection of cells, `kelp_area_m2` is the sum of the positive values,
  `count_cells_kelp` the count of them, `count_cells_no_clouds` the count of
  values ≥ 0, and `count_cells_historic_footprint` the size of the selection.
  Reconstructed that way the La Jolla selection reproduces `kelp_lajolla.csv`
  **line for line — all 213 lines, the derived `max` rows included, zero
  differences.**

### How the six bed outlines were reconstructed

The originals are not recoverable. Kelp Watch has no named-bed catalogue — its
`/db/region` endpoint returns continental regions only, and the bed names appear
nowhere in the site's JS bundle — so area selection there is draw-on-map or
upload-a-file, and the six outlines existed only in a browser session that is
gone.

What replaces recovery is derivation. Over the harvested region the footprint
cells form spatially isolated clusters, and **six of those clusters carry exactly
the cell counts the six landed exports record**, with no fitting involved:

| Bed | Latitude band of the cluster | Cells | Recorded footprint |
|---|---|---|---|
| Del Mar | 32.94436 – 32.96031 | 130 | 130 |
| Solana Beach | 32.97897 – 33.00305 | 1,040 | 1,040 |
| Encinitas | 33.00818 – 33.03927 | 1,239 | 1,239 |
| Imperial Beach | 32.55160 – 32.58842 | 3,019 | 3,019 |
| La Jolla | 32.80009 – 32.85611 | 8,309 | 8,309 |
| San Diego (Point Loma) | 32.63806 – 32.74411 | 14,635 | 14,635 |

Each cluster is separated from the nearest cell of any other bed by at least
677 m, so the divisions the operator drew by judgement fall in real gaps rather
than cutting a continuous strip. Del Mar, Solana Beach and Encinitas are three
clusters with 2.1 km and 0.6 km of empty water between them, not one bed divided
three ways. Bounding-box probes that returned 119 and 161 cells for Del Mar were
cutting at 32.96 and 32.98 — either side of a cluster that ends at 32.96031 —
and were also picking up a single isolated cell 2.3 km further south that the
bed does not include.

The recorded outline is each cluster's dissolved 30 m footprint **dilated by
90 m and simplified to 40 m**, which trades an exact but 3,153-vertex cell
boundary for a 23–191 vertex one that a human can review in a diff. The dilation
is what makes the outline robust rather than merely convenient: every cell of
the bed ends up at least 49 m inside it and every cell of every other bed at
least 537 m outside, so no plausible point-in-polygon rule — cell centre, cell
corner, intersects — can select a different set.

**Verification, all six beds, against the aggregate endpoint on 2026-08-30:**
the recorded outline returns the recorded `count_cells_historic_footprint`, and
the 213-line response is identical to the landed export in
`raw/kelpwatch/incoming/`. Zero differing lines, on every bed. The per-bed
record lives in each feature's `_verified` key in `polygons.geojson`.

Two costs of this route, stated rather than left to be discovered. The outline
is a **reconstruction, not the original**: it selects the same water, which is
the only property any number depends on, but it is not the shape the operator
drew and must not be cited as one. And the derivation reads `latest.tiles`,
which carries no revision — that it agreed line for line with the ver 23 export
is evidence the tiles were at ver 23 on 2026-08-30, not a promise about any
later day.

### Other quirks

Canopy area moves seasonally by nature; all analysis uses the anomaly transform
(doc 04 §3). The Landsat product does distinguish giant kelp from bull kelp, but
this export does not, and in the San Diego region it is effectively giant kelp
either way. The record begins in 1984Q1 and runs to the last completed quarter —
2026Q2 as recorded — so an export is a whole record every time rather than an
increment, and re-ingesting one is a rewrite rather than an append. Report card
PDFs are methodology references, not a data source.

## Project sensors

The reason the system exists. Temperature loggers now, potentially other
parameters later. Data arrives as vendor CSV exports copied off the
instruments. The fetcher is a file-drop parser: it watches
`raw/project_sensors/incoming/`, parses vendor format(s), and normalizes.
Requirements unique to this source: a site registry entry with deployment
metadata (position, depth, instrument model, calibration dates, deployment
intervals) is mandatory before data is accepted; QA/QC (doc 04) is applied
in full; and every sensor is validated against its nearest public neighbor
(NDBC/CO-OPS/SCCOOS station and satellite SST) with bias and RMSE reported
per deployment. Clock drift between downloads is a known logger failure
mode — the parser should detect timestamp irregularities and the registry
should record time-sync events.

## NDBC (National Data Buoy Center)

**Implemented** — `src/kelpcompare/fetchers/ndbc.py`, `kelpcompare ingest
--source ndbc`. Everything below the first paragraph was verified against real
LJAC1 payloads on 2026-08-25; the excerpts are recorded in
`tests/fixtures/ndbc/`.

Reference meteorological and oceanographic observations. Realtime data for
roughly the last 45 days per station at
`https://www.ndbc.noaa.gov/data/realtime2/{STATION}.txt`; multi-year archives
as annual standard meteorological files at
`https://www.ndbc.noaa.gov/data/historical/stdmet/{station}h{year}.txt.gz`,
gzipped, one calendar year each. Candidate stations near the study area: LJAC1
(La Jolla shore station) and nearby buoys such as Scripps Nearshore; the final
station list lives in the site registry, chosen by distance and parameter
coverage.

### The two layouts are not the same file shape

Both open with two `#`-prefixed header lines — column names, then units — but
they differ in four ways at once, which is why the fetcher parses them
separately rather than treating realtime as a short archive:

| | realtime | stdmet archive |
|---|---|---|
| row order | **newest first** | oldest first |
| missing token | `MM` | the column's all-nines fill |
| `PTDY` column | present, signed (`+0.4`) | absent |
| `VIS` unit | `nmi` | `mi` |

The last row is the important one in principle: the same station reports the
same quantity under two different unit tokens depending on which file was
asked for. The fetcher therefore reads each column's unit from the file's own
units line and refuses to store a column whose unit is not the one it expects,
rather than trusting the column name. A wind speed in knots stored as m/s is
the kind of error that survives into a publication.

### Sentinels are numeric, and differ per column

NDBC fills a missing value with nines to that column's own width and precision.
Observed in the 2023 LJAC1 archive: `999` (WDIR, MWD), `99.0` (WSPD, GST, VIS),
`99.00` (WVHT, DPD, APD, TIDE), `999.0` (ATMP, WTMP, DEWP), `9999.0` (PRES).
Read naively, a missing water temperature becomes a 999 °C measurement. The
fetcher compares numerically rather than as text, so a change in printed
precision cannot smuggle one through, and maps them to null at parse time.

Realtime uses `MM` throughout instead and never the numeric form. Any token
that is neither a number nor `MM` is read as missing **and reported** in the run
manifest: a token nobody has verified must not be indistinguishable from a
sentinel NDBC documents.

### What is mapped, and what deliberately is not

`WTMP` → `sea_water_temperature`, `ATMP` → `air_temperature`, `WVHT` →
`wave_significant_height`, `DPD` → `wave_peak_period`, `WSPD` → `wind_speed`.
These arrive in degC, m, sec and m/s; only the unit *tokens* need folding to
the registry's canonical spellings, which happens in the normalizer.

Two columns are read and stored by nobody, on purpose:

- **`PRES`** has no `parameters.json` entry. Adding one is a registry decision,
  not a parsing one.
- **`TIDE`** carries no declared datum. `water_level` is MLLW by definition
  (doc 03), and folding an undeclared datum into it would be invisible
  afterwards. Water level comes from CO-OPS, which states its datum on every
  request.

### A station stores only what it has a sensor for

The stdmet format has fixed columns, so a shore station with no wave sensor
still has `WVHT` and `DPD` in every file, filled with the sentinel. **The
fetcher stores only the parameters `sites.json` declares in
`measured_parameters` for that station.**

This reverses an earlier decision recorded here, which stored those rows so that
"this station reported no wave height" and "nobody asked this station for wave
height" would stay distinguishable. Both facts are still distinguishable; the
second one has simply moved to a better home. Across the 2007–2025 LJAC1 archive
plus realtime, storing them meant 3,289,004 rows that carry no measurement — 40%
of the zone for that station — and would have put roughly 76 quarterly feature
rows per wave parameter, every one at zero coverage, into `quarterly_env` for a
station that has never had a wave sensor.

The declaration is what keeps the two facts apart, and it is why this is a
registry statement rather than a per-payload judgement:

| | `measured_parameters` | file holds | stored |
|---|---|---|---|
| No instrument | omits the parameter | sentinel | nothing |
| Instrument, outage | includes it | sentinel | rows, flagged missing |
| Nobody has checked | absent entirely | either | everything recognised, plus a warning |

Skipping any column that is entirely sentinel in a payload was rejected for
exactly this reason: a sensor that failed for one whole year would look
identical to a sensor that does not exist, and the rows recording the outage
would vanish. A station with no `measured_parameters` at all is *undeclared*
rather than empty — everything recognised is stored and the run warns, because
an unrecorded fact must not quietly become missing data. A declared parameter
that is not in `parameters.json` is reported too: it matches no column, so the
typo subtracts a real series rather than adding a fictional one.

LJAC1 declares `sea_water_temperature`, `air_temperature` and `wind_speed`, so
its ingest lands three parameters per timestamp rather than five.

Rows already stored under the old behaviour stay where they are — re-ingesting
merges and dedupes, it does not delete. Retiring them means deleting
`observations/source=ndbc/` and re-ingesting, which the zone is designed to
support.

### Other quirks

Station instrumentation changes over the years. Pre-2005 archives use a
different time layout with no minute column; the fetcher refuses them by name
rather than misreading the columns that follow. Water temperature depth differs
by platform (shore station intake vs. buoy hull) and **must** be recorded in the
site registry as `sensor_depths_m`, since comparing a 1 m buoy temp to a 10 m
logger temp is a real analysis error we have to prevent structurally. LJAC1's
intake is 3.4 m below MLLW.

### LJAC1 is CO-OPS 9410230

The NDBC station page titles this platform `LJAC1 - 9410230 - La Jolla, CA`,
owned and maintained by NOAA's National Ocean Service. NDBC redistributes the
NOS observations: the two site records describe one instrument package, not two
stations. `sites.json` records this as `same_platform_as`, because the doc 04
neighbor validation must never count them as two independent references for the
same sensor — both project sites list both in `neighbor_refs`.

## NOAA CO-OPS (Tides & Currents)

Water level and coastal water temperature via the CO-OPS Data API
(`api.tidesandcurrents.noaa.gov`), JSON or CSV, with products including
`water_level`, `water_temperature`, and predictions. Local station:
9410230 (La Jolla). Quirks: every water-level request requires an explicit
datum — we standardize on MLLW for storage and record the choice; datum
pages and benchmark sheets are reference material, not time series. The
API enforces span limits per request (on the order of a month for
high-frequency products), so the fetcher paginates by date window. Water
level enters the analysis mainly as anomalies (departures from predicted
tide), which index regional oceanographic events like El Niño-elevated sea
level, not as raw tide state.

## SCCOOS / CalOOS

The richest programmatic source for nearshore Southern California. The
SCCOOS ERDDAP (`erddap.sccoos.org`) serves shore station automated sensors,
CalHABMAP harmful algal bloom monitoring (Scripps Pier is a local site),
and model/current products, with per-dataset access forms and RESTful
queries returning CSV, JSON, or NetCDF; the CalOOS portal
(`data.caloos.org`) is the discovery layer over the same holdings. The
fetcher uses `erddapy` with dataset IDs pinned in the site registry.
Quirks: dataset IDs and variable names occasionally change — pin them in
config, not code; some feeds carry their own QC flag columns which we map
into our flag scheme rather than discard; HAB counts are discrete sampling
(roughly weekly), not continuous, and are treated as event/covariate data;
HF-radar surface currents are gridded and only pulled if the analysis
reaches transport questions (deferred).

## City of San Diego RTOMS

Two moored strings run by the City of San Diego Public Utilities Department
with Scripps, near the terminal ends of the Point Loma and South Bay ocean
outfalls, carrying `sea_water_temperature` at many depths on one string at a
10-minute cadence. **This is the only depth-resolved temperature in the study
region**, and that is the whole reason it is here: every other environmental
series in this project comes from `NDBC:LJAC1`, a single sensor 3.4 m below
MLLW, and `sites.json` already declines to report bias for `PROJ:TIDBIT-2`
against it because that reference sits 13.4 m above the logger and above the
summer thermocline.

Reached through the **CeNCOOS ERDDAP** (`erddap.cencoos.org`), not through the
City's open data portal, and the choice is deliberate — see "The portal CSVs
are the same data, worse" below.

| Site | Dataset ID | Position | Temperature depths |
|---|---|---|---|
| `SDRTOMS:PLOO` | `point-loma-ocean-outfall-real-ti` | 32.66996, -117.32676 | 11: 1, 9, 10, 20, 30, 45, 60, 75, 85, 87, 90 m |
| `SDRTOMS:SBOO` | `south-bay-ocean-outfall` | 32.53171, -117.18631 | 6: 1, 10, 18, 20, 25, 26 m |

Both were verified on 2026-08-28 as internally consistent — the dataset's
`geospatial_lat/lon` attributes agree with the `latitude`/`longitude` columns
of the rows themselves — and both agree with the depth each mooring reaches,
which is the independent check that matters: Point Loma's string runs to 90 m
off a 94 m outfall terminus, South Bay's to 26 m off a much shallower one.

### The QARTOD vocabulary is already ours

Each parameter arrives with a `_qc_agg` aggregate flag and a `_qc_tests`
per-test flag, both declaring
`flag_values: 1, 2, 3, 4, 9` and
`flag_meanings: PASS NOT_EVALUATED SUSPECT FAIL MISSING`.

That is the doc 03 `qc_flag` vocabulary exactly, value for value, so this is a
pass-through and not a mapping — there is no translation table to get wrong.
`_qc_agg` becomes `qc_flag` and `_qc_tests` becomes the `qc_tests` record.
Ingest does not overwrite these with its own verdict: the provider ran QARTOD
with knowledge of its own instruments, and doc 03's rule that flags are
attached rather than rows deleted applies to a flag that arrived just as much
as to one this project computed.

### Depth is on the payload, not in the registry

A mooring measures one parameter at many depths at once, so `sensor_depths_m`
declares a **list** for these sites and the fetcher reads `depth_m` per row
(doc 03, "A source may be self-describing on depth"). The list is not a value
the fetcher consumes; it is what a payload depth is checked against, so a
mooring back from a refit with a sensor at a new depth is reported rather than
landed silently as a series nobody has seen.

**A nominal depth that drifts between deployments is two depths, not one.** The
string reports 9 m on one deployment and 10 m on the next for what is
physically the same position, and both are declared and both land. Rounding
them together would write a depth the mooring never reported into `depth_m`,
which is part of `OBSERVATION_KEY` and therefore permanent. The cost is real
and accepted: the record for that position splits at the deployment boundary,
so per-series quarterly coverage is thinner than the raw row count suggests.

### `z` is altitude, and the sign flips

ERDDAP serves the vertical coordinate as `z`, positive **up** from the surface,
so every value is negative or zero. Doc 03 `depth_m` is positive **down**. The
fetcher negates. A sign error here is not subtle in its consequences — it puts
every reading above the water — but it is entirely silent in a Parquet file, so
it is checked at the boundary rather than trusted.

### Temperature lives only on the discrete mooring depths

`z` takes 41 distinct values on the South Bay feed, most of them 1 m apart.
Those are ADCP velocity bins: the datasets are `TimeSeriesProfile` and flatten
every instrument on the string into one vertical axis, so `eastward_sea_water_velocity`
contributes a bin every metre while temperature sits on a handful of fixed
positions. Filtering to rows where the temperature is not null recovers the 11
and 6 depths tabled above. A fetcher that took the depth axis at face value
would land dozens of series per station, nearly all of them empty.

### What is deliberately not read

The feeds also carry salinity, dissolved oxygen, pH, chlorophyll, CDOM,
turbidity, xCO2, BOD and current velocity. Only `sea_water_temperature` is
stored, because it is the only one of those with a `parameters.json` entry, and
adding one is a registry decision about SI units and QC bounds rather than a
parsing convenience.

**`mole_concentration_of_nitrate_in_sea_water` is the interesting omission.**
Doc 04 records that temperature and nitrate proxies are anti-correlated by
regional oceanography and that separating thermal stress from nutrient
limitation "is not available from this data at all"; this is measured nitrate,
not the BEUTI proxy recommended above. It is not simply switched on because
these moorings sit on wastewater outfall diffusers, so nitrate here carries an
anthropogenic component that a kelp-nutrient reading must account for. Tracked
separately; do not add it as a one-line parameter entry.

### Three things a real ingest turned up

Measured on `SDRTOMS:SBOO` for 2023 — 88,307 rows stored from a payload of
107,831 — and each would have been silent.

**A second instrument reports at a temperature depth, on its own clock.** The
depth filter above catches the ADCP bins because they sit where no temperature
sensor does. It cannot catch the same thing happening *at* 20 m, which is a
declared depth: something else on the string reports there a minute off the
10-minute grid, and ERDDAP emits a row for every `(time, depth)` any instrument
reported at. That was **17,755 rows** — a series that is essentially complete
read as 40% missing, which would have carried into `pct_coverage` and every
quarterly feature built on it.

The provider separates them itself, and exactly: across that ingest **every row
carrying a value had a `_qc_tests` verdict, without exception**. An empty
verdict therefore means no temperature test was ever run on that row, which is
not a sensor that failed but a row that was never about this sensor. The
fetcher drops those and keeps every gap the provider did evaluate, which stays
in the record flagged `9`. Both are dropped silently — this is the normal shape
of every payload, and the manifest's `rows_in` against `rows_out` already shows
the attrition.

**`_qc_agg` and `_qc_tests` disagree on absent readings.** The provider writes
`qc_agg = 2` (not evaluated) on rows whose own `_qc_tests` records the gross
range test as `9` (missing) — all seven 26 m gaps in the recorded fixture do
this. doc 03 decides it: an absent value is `9`, because there is nothing in an
absence to judge. Landing them at `2` would put holes through the default
`qc_flag <= 2` analysis filter as though they were data. The fetcher therefore
overrides the aggregate wherever the value is null, and this is a deliberate
divergence from the provider rather than an accident.

**Conditional requests are not honoured.** ERDDAP answers `If-Modified-Since`
with `200` and the whole body, verified 2026-08-28. This source has no cheap
"has it changed" and never raises `NotModified`; a re-run is made cheap by
asking for a narrower time window instead, which suits a growing time series
better than an ETag would anyway.

### What it is worth, measured

One year of South Bay, stored and aggregated:

| Depth | Mean 2023 temperature |
|---|---|
| 1 m | 18.44 °C |
| 10 m | 16.13 °C |
| 20 m | 14.97 °C |
| 25 m | 12.38 °C |

Six degrees over twenty-five metres. That is the number the whole entry is for:
`NDBC:LJAC1` measures 3.4 m and nothing else, `PROJ:TIDBIT-2` sits at 16.76 m,
and `sites.json` currently explains the ~5 °C gap between them in prose. It is
also why these moorings are a depth reference and not a neighbor — the gradient
is the signal, and 21-38 km of coastline is the confound.

**None of this reaches `comparison.parquet`, by construction.** The features
run that landed these series left the comparison table byte-identical
(`sha256:7d2c62503276e7be`, 15,300 rows). Two independent reasons, and both
should be understood before anyone expects a lag screen to change: no polygon
lists these sites in `site_ids`, and the climatology baseline is fixed at
2007-2019 while these records begin in 2021, so every series lands with
`baseline_years = 0` and every anomaly null. A four-year record cannot support
a ten-year climatology, and no amount of backfill from the portal CSVs would
change that — they start in 2020.

### `south-bay-ocean-outfall-historic` disagrees with itself about where it is

Two further datasets exist, covering 2020-01 to 2023-01 (`point-loma-ocean-outfall-histori`)
and 2020-01 to 2022-11 (`south-bay-ocean-outfall-historic`). **Neither is
ingested**, and the second must not be without an upstream fix.

Checked on 2026-08-28, `south-bay-ocean-outfall-historic` gives three different
answers to where its instrument was:

| Source | Says |
|---|---|
| `geospatial_lat/lon` attributes | 32.66996, -117.32676 — Point Loma's position, byte-identical to both PLOO datasets |
| the `latitude`/`longitude` columns in the rows | 32.86917, -117.24674 — La Jolla, ~37 km north |
| its `z` range, -26.0 to -1.0 m | South Bay's mooring, which is what the title claims |

The depths are the only one of the three that matches the title, so the data is
probably South Bay's and both positions are wrong. That is a guess, and a site
record is not built on a guess — position is a reviewed registry fact here
(`sites.json`, `PROJ:TIDBIT-1`), and a station whose provider contradicts
itself twice has not supplied one. `point-loma-ocean-outfall-histori` is
self-consistent and agrees with its real-time sibling; it is left out only to
keep the first landing to one concern, and backfilling it is ordinary work.

### The portal CSVs are the same data, worse

`data.sandiego.gov` publishes the same measurements as per-year CSVs on
`seshat.datasd.org`, and they were the obvious route until the ERDDAP feeds
turned up. Recorded so the comparison is not redone:

- **They stop at 2023.** ERDDAP runs to 2024-12 (PLOO) and 2025-06 (SBOO).
- **They carry a qualifier flag of 1-5 and 9**, including a `5` for
  "value changed / drift-corrected in post-processing" that has no doc 03
  equivalent and would need a judgement call. The ERDDAP feeds use the
  five-value QARTOD set that needs none.
- **They are 3-34 MB per file with no server-side subsetting**, so an
  incremental re-run downloads the year again and a recorded fixture is a
  large one. `tabledap` constrains by time and variable at the server: one hour
  of South Bay is 17 KB, which is what makes the recorded fixture possible.
- **No new dependency either way.**

Unlike the SCCOOS entry above, this fetcher builds its `tabledap` URLs directly
rather than through `erddapy`. The URL is the thing `raw/` records and the run
manifest reports, so constructing it in one visible place means a landed payload
can be re-requested by copying a string out of the manifest. `erddapy` remains
the right tool for SCCOOS, where dataset discovery and variable introspection
are the hard part; here the query is four constraints and a fixed variable
list.

They remain the only route to 2020 and most of 2021 for South Bay, since that
window exists on ERDDAP only in the dataset described above.

### Distance is the standing caveat — for the loggers, not for every bed

These moorings are 21-38 km south of the La Jolla instruments and sit on
outfall diffusers. They are a **depth reference, not a neighbor**: doc 04's
neighbor validation compares an instrument against a nearby one, and neither
mooring is near a project logger. What they can support is the question
`sites.json` currently answers in prose — how far a 3.4 m shore reading sits
from water at thermocline depth in this region, and how that gap moves with
season. They also cannot validate the project's own loggers, whose deployments
begin in July 2026, after every RTOMS record ends.

**That caveat is about the loggers, not about the study area**, and this entry
used to state it as though it were about both. Measured against the bed
outlines now recorded in `polygons.geojson` — UTM 11N, nearest point on the
outline — `SDRTOMS:SBOO` is **2,968 m** from `KELP:IMPERIAL-BEACH` and
`SDRTOMS:PLOO` **5,264 m** from `KELP:SAN-DIEGO`. Each is the nearest site of
any kind to that bed, and Imperial Beach has nothing else within 17 km. Neither
mooring is in those polygons' `site_ids`, and after the correction to
`sites.json` the reason is the diffuser rather than the range: whether outfall
water represents the bed's is a question about representativeness, and it is
the pairing rule filed as
https://github.com/cweber12/kelp-compare/issues/86.

## SIO Shore Stations — La Jolla, Scripps Pier

**Implemented** — `src/kelpcompare/fetchers/sio_shore_stations.py`,
`kelpcompare ingest --source sio_shore_stations`. Everything below was verified
against the fourteen downloaded snapshots on 2026-08-28; excerpts of two of them
are recorded in `tests/fixtures/sio_shore_stations/`.

**The longest in-situ temperature record in the study area: one daily grab
sample at Scripps Pier, surface and bottom, since 22 August 1916.** 40,034
consecutive days to 31 March 2026 in the pinned snapshot, with no gap in the
calendar — a day nobody sampled is a row with a null reading, not an absent row.
That is 110 years against Kelp Watch's 42 and NDBC:LJAC1's 19, which is the
whole reason it is here.

Citation is required wherever these data appear, in the words the pinned
snapshot's own header uses:

> Carter, Melissa L.; Flick, Reinhard E.; Terrill, Eric; Beckhaus, Elena C.;
> Martin, Kayla; Fey, Connie L.; Walker, Patricia W.; Largier, John L.;
> McGowan, John A. (2022). Shore Stations Program — La Jolla, Scripps Pier
> (La Jolla Archive, 2026-06-30). In *Shore Stations Program Data Archive:
> Current and Historical Coastal Ocean Temperature and Salinity Measurements
> from California Stations*. UC San Diego Library Digital Collections.
> `https://doi.org/10.6075/J06T0K0M`
>
> Funding for the Shore Stations Program provided by the California Department
> of Parks and Recreation, Natural Resources Division, Award# C22820005.

The award number is **not** constant across snapshots — the five oldest name
`C1670003` — so the citation is a property of the pinned archive rather than of
the program, and is recorded on the site record beside the pin.

### The route: a hand-downloaded CSV, pinned like a Kelp Watch revision

**The operator downloads the archive by hand and drops
`LaJolla_TEMP_1916-YYYYMM.csv` in `raw/sio_shore_stations/incoming/`.** There
is no fetcher, no URL and no `SourceUnavailable` path, for the same reason Kelp
Watch has none: the source cannot be pulled. Access needs a Google Form
registration, and the UCSD Library repository sits behind Anubis proof-of-work
bot protection (endpoint `/.within.website/x/cmd/anubis/`, confirmed
2026-08-28). Automating past a bot wall is not a fetcher, it is an evasion, and
this project does not write one.

It is also not on SCCOOS ERDDAP: only the HAB and SPATT datasets exist there for
Scripps Pier.

Each download is a **cumulative snapshot of the whole record**, so re-ingesting
one is a rewrite rather than an append, exactly like a Kelp Watch export.
`sites.json` therefore pins one — `archive.archived` plus the file it arrived
as — and an ingest refuses to run without a pin, quarantines a file whose header
declares a different archive date, and lands under
`raw/sio_shore_stations/{archived}/`, so two archives cannot interleave.

**Unlike Kelp Watch, the file declares its own version**, on preamble line 4:
`Data provided are subject to revision and were archived 2026-06-30.` So the pin
is *checked* rather than merely asserted, which is the one thing the Kelp Watch
pin cannot do.

**Also unlike Kelp Watch, no snapshot has yet revised history.** Comparing the
pinned 2026-06-30 archive against the 2020-12, 2023-12 and 2025-12 ones, across
38,118, 39,213 and 39,944 shared days, **not one value, flag or time differs** —
every newer snapshot is a pure append. That is measured, not promised: the
header says the data are "subject to revision", so the pin stays.

### Layout

A preamble, then the column header, then one row per calendar day:

```
YEAR,MONTH,DAY,TIME_PST,TIME_FLAG,SURF_TEMP_C,SURF_FLAG,BOT_TEMP_C,BOT_FLAG,,,,,
1916,8,22,NaN,NaN,19.5,0,NaN,NaN,,,,,
...
2026,3,31,1343,0,18.7,0,18.7,0,,,,,
```

Five unnamed trailing columns are present in every row of every snapshot and are
empty in all of them; the parser tolerates them and refuses any that is not.
Missing is spelled `NaN`, everywhere, in both value and flag columns. Line
endings are CRLF.

**The preamble length is not fixed.** Nine of the fourteen snapshots carry 46
lines before the column header and five carry 45. A parser that skipped a
constant would read the column header as data on a third of the archive, so the
header line is *found*, by its `YEAR,MONTH,DAY,TIME_PST` prefix.

**Nor is the encoding.** The five oldest snapshots are Mac Roman (the degree
sign in the position line is byte `0xA1`) and carry no byte-order mark; the nine
newest are UTF-8 with a BOM (`0xC2 0xB0`). Nothing in this project reads that
character — the position is parsed with a pattern that treats any non-digit run
as a separator — so decoding falls back to latin-1, which cannot raise, on the
same reasoning `fetchers.base.Payload.decode` gives.

**The filename does not name the last day of data.** `LaJolla_TEMP_1916-202509`
runs to 2025-10-31. Coverage is read from the rows.

**Trailing filler rows exist.** Five of the fourteen snapshots end with between
30 and 119 rows that are nothing but commas. A row with no date at all is
dropped; a row with a *partial* date is a format surprise and stops the parse.

### Two depths in one file, and `depth_m` is permanent

Surface (~0.5 m) and bottom (~5 m) are two series, and `depth_m` is part of
`OBSERVATION_KEY` (doc 03), so getting the pair wrong is not fixable by a later
run. `sensor_depths_m` therefore declares the **list** form for this site and the
parser checks its two depths against it (doc 03, "A source may be
self-describing on depth"), rather than the registry supplying a single value it
could not have.

The depths come from the file, not from this document: preamble line 2 reads
`Shore Stations Program - La Jolla, Scripps Pier Surface (~0.5m) and Bottom
(~5m) Temperature Data`, and the parser reads the two parenthesised depths out
of it. Identical across all fourteen snapshots. A snapshot that renamed or
re-sounded them is quarantined rather than landed at the old numbers.

They are nominal, and the `~` is the program's own. A pier sampler works from a
fixed platform in about 5 m of water, so the bottom figure is a sounding rather
than a sensor depth, and the surface bucket is wherever the surface was that
morning. Nothing here treats either as better than ±0.5 m.

**The bottom series starts ten years after the surface one**, on 1926-07-21.
Before that the file carries a row per day with `BOT_TEMP_C` *and* `BOT_FLAG`
both `NaN` — the flag column is empty for exactly the 3,620 days before the
first bottom reading and for no others, which is the source saying the series
did not exist yet rather than that a sample was missed. Those rows are dropped;
a null *after* a series starts is a real gap in a running program and lands
flagged missing, as doc 03 requires. This is the same distinction the RTOMS
parser draws between an outage at a declared depth and another instrument's
profile bin, reached by different evidence.

### Times are PST, exist only from 1990, and are otherwise a convention

`TIME_PST` is an integer `HHMM` with no leading zero — `858` is 08:58, and
midnight would be `0`. It is present for 12,473 of 40,034 days, none of them
before 1990, and the header says so: *"Time of sample collection available for
1990-current data only."*

**PST is a fixed −08:00 offset, not `America/Los_Angeles`.** The header names
the zone as Pacific *Standard* Time, and applying a DST-aware zone would move
every summer reading by an hour in the direction that looks like a diurnal
signal. The parser applies −08:00 year-round.

The header also warns that *"Time records between 1990-2004 may vary by up to 1
hour with actual sample time since time zone was not recorded during these
years"* — which is the same DST ambiguity, admitted upstream, over the 5,163
timed days in that span. It is documented and **not flagged**: an hour of
uncertainty on a daily grab sample changes no quarterly feature, and marking
5,163 rows suspect would drop them from the default `qc_flag <= 2` filter to buy
nothing.

**The convention for a day with no time: 10:38 PST.** That is the median
time-of-day of the 12,473 days that do carry one, and using it rather than a
round number is the point — it is an estimate from this program's own sampling
behaviour, not a placeholder. The 2005-onward days, the ones with no DST
ambiguity at all, give 10:43, so the estimate is stable across the two halves of
the timed record. The hourly distribution is tight and unimodal: the modal hour
is 10, the interquartile range is 09:45–12:00, and the whole record spans 05:24
to 20:02.

Alternatives, and why not:

- **Local midnight.** Simple and obviously arbitrary, but it is the worst
  available estimate of when a morning grab sample was taken, off by about ten
  and a half hours in a known direction.
- **Local noon.** Conventional, but still a guess, and it is *later* than this
  program actually samples — which biases every imputed reading toward the
  warmer part of the day rather than toward no bias at all.
- **A null timestamp.** Not available: `timestamp` is not nullable in doc 03 and
  is part of `OBSERVATION_KEY`.

The cost, stated plainly: **27,561 of 40,034 days carry no time**, which is
51,502 of the 76,448 stored observations — 67% — with a timestamp this project
assigned rather than one the observer wrote down. Everything the project does
with these rows is quarterly, and a quarter is 90 days, so the assignment
changes no feature. It would matter to any day-matched or hour-matched
comparison, which is why the imputed rows are identifiable.

**How to tell an imputed timestamp from a measured one.** A row whose `qc_tests`
records a `sample_time` verdict had a time in the file; a row with no
`sample_time` verdict did not. No new column, and it is the honest reading of
doc 03's rule that a test which reached no verdict records nothing: there was no
time to check.

That the convention lands at 18:38 UTC also keeps the UTC date equal to the
local date. Any imputed time at or after 16:00 PST would roll into the next UTC
day and move a 31 December reading into the next quarter — some of the record's
measured times do exactly that, legitimately, and an assigned one must not.

**`TIME_FLAG` cannot be used to detect a missing time.** All 766 post-1990 days
with no `TIME_PST` carry `TIME_FLAG = 0`, "good data", about a time that is not
there. The `TIME_PST` column is the only evidence.

### Flag mapping — the program's own vocabulary, not QARTOD

Declared in the preamble of every snapshot, and read from it: a snapshot whose
legend declares a code this table does not cover is quarantined, on the same
rule the RTOMS parser applies when a provider's flag vocabulary changes.

| Source | Meaning | `qc_tests` | `qc_flag` |
|---|---|---|---|
| value is `NaN` | data not collected | `source_flag:missing` | 9 missing |
| `0` | good data | `source_flag:pass` | 1 pass |
| `1` | illegible entry | `source_flag:suspect` | 3 suspect |
| `2` | differs from other sources | `source_flag:suspect` | 3 suspect |
| `3` | data uncertain | `source_flag:suspect` | 3 suspect |
| `4` | leaky bottle | `source_flag:suspect` | 3 suspect |
| `5` | Pier Chlorophyll Program **or a different location** | `source_flag:fail` | 4 fail |
| flag is `NaN`, value is not | *(never observed)* | no verdict | 2 not evaluated |

**That last column is what this test contributes, not always the stored flag.**
`qc_flag` is the roll-up of every verdict on the row, so a `sample_time:suspect`
beside a `source_flag:pass` stores 3 — and in the one row above where the data
flag is absent, a `sample_time:pass` alone stores 1 rather than 2, because it is
then the only verdict that reached a conclusion. That case has never occurred,
and it costs nothing if it does: both 1 and 2 pass the default `qc_flag <= 2`
filter, so the label differs and the analysis does not.

**An absent reading is `missing` whatever the flag column says.** The source
writes `0` — good data — beside 1,330 absent surface readings and 2,256 absent
bottom ones. Doc 03 gives 9 to a row with no value and there is nothing in an
absence to judge, so the flag column is not read as evidence about a reading
that does not exist, and `qc_tests` records `missing` so the roll-up and the
record agree.

**Flag 5 is the one that needed a decision, and it does not occur.** Across all
fourteen snapshots and both series, the only data flags ever written are
`0, 1, 2, 3`. Flag 4 ("leaky bottle") is a salinity condition and never appears
in a temperature file either. So the two rows of the table above that matter
most are rules for cases that have not arisen, and they are written to fail
safely rather than to be convenient:

- Flag 5 conflates two different things — a sample taken for another program at
  this pier, which is this site's water, and a sample taken *somewhere else*,
  which is not. One code cannot be mapped to both, and there is no other column
  to tell them apart. Mapping it to `fail` keeps the row on the record and out
  of the default analysis filter, which is what doc 06 §3 does with a reading
  taken outside its deployment window, and for the same reason: a real
  measurement that is not a measurement of this site.
- The alternative — stopping the parse — was rejected because it would block a
  110-year record on one row, and because flag 5 is a *documented* code rather
  than a surprise. An **undocumented** code still stops the parse, which is
  where the RTOMS precedent actually applies.
- A run that lands any flag-5 row warns, by date, so the choice is visible the
  first time it is ever exercised rather than settled silently here.

`TIME_FLAG` maps onto a second test, `sample_time`, on the same scale and only
where a time exists: `0` is `pass`, `1`/`2`/`3` are `suspect`. That is 133 days
of 40,034, and it is the source telling us the *time* is illegible or disputed —
which makes the observation suspect, since an observation is a reading and a
time. Use `--qc-max-flag 3` to get them back.

### Data quality, measured

- **39,919 of 40,034 surface readings carry flag 0.** Only 115 surface and 112
  bottom readings are flagged at all, across 110 years.
- Landed: **76,448 observations** — 40,034 surface and 36,414 bottom, from
  40,034 daily rows. By `qc_flag` at ingest: 72,436 pass, 426 suspect, 3,586
  missing. 266 of the suspects are the `sample_time` verdict rather than the
  data flag.
- 30 of 30 years are usable in every quarter, surface and bottom, for both the
  1984–2013 and 1991–2020 baselines (≥60% of days at flag 0).
- Against `NDBC:LJAC1` over 5,822 overlapping days, 2007–2025: **bottom ~5 m
  gives bias −0.07 °C, RMSE 1.02 °C, r 0.918**, with monthly bias never beyond
  ±0.33 °C and no seasonal structure. Surface ~0.5 m gives bias +0.66 °C. The
  bottom series is effectively the same measurement as LJAC1, extended back to
  1916.

### One daily grab sample is not a time series, and some features do not survive it

This is one bottle a day, not a moored sensor. Threshold counts, spell lengths
and degree-days are computed from daily values and are comparable; **`p95` and
variance are not** comparable to anything sampled sub-hourly, because they
measure the tail and the spread of a *sampling* distribution that has one point
per day here and hundreds at LJAC1. Wherever a quarterly feature from this
source is put beside one from a continuous source, that has to be said. Doc 04's
neighbor-validation caveat about depth is the same shape of problem: the number
computes fine and means something different.

**The QARTOD thresholds in `parameters.json` are the same problem, and they are
live.** They are keyed by parameter and were tuned against a 10-minute logger,
so on this daily series `spike` flags **3,043 of 34,158 bottom readings (8.9%)**
as suspect or failed — the threshold of 1.5 °C sits near the 88th percentile of
ordinary day-to-day variation at 5 m, which off this pier is upwelling rather
than instrument error — while `rate_of_change` cannot fail at all and records a
`pass` on 70,462 rows regardless. **Do not run `kelpcompare qc` over
`source=sio_shore_stations`** until that is resolved: the ingest-time flags from
the program's own vocabulary are correct and sufficient, and a qc run would
overwrite 3,043 good readings with a suspect verdict that the default
`qc_flag <= 2` filter then drops. Tracked, with the measured percentiles and the
options, at https://github.com/cweber12/kelp-compare/issues/68.

### Co-located with `NDBC:LJAC1`, but not the same instrument

The header position, `32°52'01.0"N 117°15'25.7"W` → 32.866944, −117.257139, is
about 10 m from the `NDBC:LJAC1` / `COOPS:9410230` platform. It is nonetheless
**not** recorded as `same_platform_as` those, because that field means one
physical instrument package under two identifiers (doc 03), and this is a
hand-taken bottle at 0.5 m and 5 m against an automated NOS sensor 3.4 m below
MLLW — different instruments, different method, different depths, agreeing to
r = 0.918 rather than by construction.

What follows is that doc 04's neighbor validation would count them as two
independent references for a project sensor, and spatially they are one place.
It is not a `neighbor_ref` for either project logger anyway, for a simpler
reason: this record ends 2026-03-31 and both deployments begin in July 2026, so
there is nothing to compare. That is a reprieve rather than an answer — the next
quarterly archive may end the gap, and at 5 m this is the most depth-comparable
reference the project has for `PROJ:TIDBIT-1`. Tracked at
https://github.com/cweber12/kelp-compare/issues/69.

### Temperature only

The same download carries `LaJolla_SALT_*.csv` in the identical format.
Salinity has no `parameters.json` entry, and adding one is a registry decision
about SI units and QC bounds rather than a parsing convenience — the same line
the RTOMS entry draws. `sniff()` accepts the temperature layout only, so a
dropped SALT file is skipped rather than misread.

## CDIP (Coastal Data Information Program)

Wave climate — significant wave height, period, direction — from
Scripps-operated buoys, served as NetCDF via THREDDS/ERDDAP. Waves matter
because large winter swell events physically remove canopy, a mechanism
completely separate from heat stress; without wave data, storm-driven kelp
loss would be misattributed to temperature. Ingested per-station like NDBC;
the feature builder derives event counts above height thresholds and
maximum event duration per quarter.

### It is already reached through NDBC, and that changes what is worth building

Verified 2026-08-31 against `https://www.ndbc.noaa.gov/data/stations/station_table.txt`.
**NDBC redistributes the whole San Diego CDIP nearshore array**, each station
carrying its CDIP number in the NDBC station name, so these buoys arrive through
`fetchers/ndbc.py` with no new code and no new dependency:

| NDBC | CDIP | Name |
|---|---|---|
| 46232 | 191 | Point Loma South |
| 46235 | 155 | Imperial Beach Nearshore |
| 46231 | 093 | Mission Bay |
| 46225 | 100 | Torrey Pines Outer |
| 46273 | 101 | Torrey Pines Inner |
| 46226 | 095 | Point La Jolla |
| 46227 | 091 | Point Loma |
| 46258 | 220 | Mission Bay West |
| 46254 | 201 | Scripps Nearshore — **ingested** |
| 46266 | 153 | Del Mar Nearshore — **ingested** |

Adding one is therefore a `data(registry)` change, not a fetcher. A THREDDS
route to `191p1_historic.nc` would be a second way to the same numbers, and the
argument for building it has to be made on something other than access.

**All three carry exactly the parameters the two ingested Waveriders declare.**
Sampled `46235h2019`, `46231h2012` and `46232h2019`: `WVHT`, `DPD`, `APD`, `MWD`
and `WTMP` are real in essentially every row, while `WDIR`, `WSPD`, `GST`,
`PRES`, `ATMP`, `DEWP`, `VIS` and `TIDE` are sentinel in all of them. So
`measured_parameters` for any of these is the same three
(`sea_water_temperature`, `wave_significant_height`, `wave_peak_period`) already
declared for 46254 and 46266, and no `parameters.json` decision is involved.

### Distance to each polygon, measured

Nearest point on the recorded outline, UTM 11N — the same method the RTOMS entry
uses. Ingested stations in bold.

| Polygon | Nearest today | 46235 | 46231 | 46232 |
|---|---|---|---|---|
| `KELP:LA-JOLLA` | **46254** 1.4 km | 27.3 | 9.3 | 33.7 |
| `KELP:DEL-MAR` | **46266** 0.0 km | 42.5 | 23.7 | 49.4 |
| `KELP:SOLANA-BEACH` | **46266** 2.3 km | 46.3 | 26.9 | 52.9 |
| `KELP:ENCINITAS` | **46266** 5.6 km | 49.7 | 29.8 | 55.8 |
| `KELP:SAN-DIEGO` | **LJAC1** 13.5 km | 10.3 | **8.6** | 21.1 |
| `KELP:IMPERIAL-BEACH` | **LJAC1** 32.3 km | **0.3** | 26.2 | 24.6 |

**`NDBC:46235` sits 300 m off `KELP:IMPERIAL-BEACH`.** docs/04 §4.5 records that
this bed and `KELP:SAN-DIEGO` "have no station in range", keep La Jolla
references 32.3 km and 13.5 km away, and that a §4.5 result there "is partly a
statement about how far away the public station is". That is a statement about
`sites.json` rather than about the coast: two stations closer than anything
currently declared have been sitting in NDBC's archive the whole time.

Neither is free, and the costs are in the record rather than in the access:

- **46235 has a seven-year hole.** Annual stdmet archives exist for 2007–2010
  and 2018–2025 and for no year between, and the gap swallows the 2014–2016
  marine heatwave — the event docs/04 §4.2 most wants. It also serves no
  realtime feed, so there is no `--year`-less path to the current quarter.
- **46231 ends in 2016** and likewise has no realtime feed. It is the better
  neighbour for `KELP:SAN-DIEGO` by 4.9 km, over a decade that stops before the
  kelp record does.
- **46232 is the only one of the three that is both long and live** — archives
  2006–2025 continuous, plus a realtime feed answering today. It is also 21 km
  from the nearest polygon, which is further than `NDBC:LJAC1` already is. The
  bundle reviewed under "Sources considered and set aside" dates this record to
  "~2016/17"; through NDBC it starts in 2006.
- **Pre-2007 years are refused by the parser** on the undocumented header layout
  (`https://github.com/cweber12/kelp-compare/issues/20`), so 46231's 2005 and
  2006 archives cannot be read today even though they exist.

A gappy record is not a disqualification here — `pct_coverage` and the coverage
floor already decide per quarter whether a series is worth believing, and a
quarter below it is flagged unusable rather than dropped. What a hole does is
decide which *quarters* the §4.5 comparison can run in, which is a different
question from whether to declare the station.

## CDFW / marineBIOS

GIS context rather than time series: substrate, kelp persistence, MPA
boundaries, administrative kelp bed designations, and CDFW's historical
aerial/multispectral kelp canopy surveys (irregular years). Landed once as
shapefiles/GeoJSON into `raw/gis/`, loaded with geopandas. Two uses: spatial
joins (which MPA/kelp bed contains each polygon and sensor) and an independent
cross-check of Kelp Watch canopy in overlapping years — agreement there
strengthens any claim built on the Landsat product.

**Nothing here is built.** `raw/gis/` appears in the docs/03 layout and is
empty; no fetcher reads BIOS, and no code in the package performs a spatial
join against anything but `polygons.geojson`. The layers below are recorded so
that which ones get adopted is a reviewable decision rather than whichever a
browser session happened to have switched on.

### The layers, and what each would settle

| Layer | What it would settle |
|---|---|
| Predicted Nearshore Benthic Substrates `[ds3091]` | Which water can hold kelp at all. docs/04 §4.5 fits kelp against polygons at increasing distance from a sensor, and rings drawn without substrate will include sand — which holds no canopy for reasons that have nothing to do with distance, and which therefore reads as decay. |
| Kelp Persistence `[ds3151]` | Whether the six reconstructed outlines enclose real kelp habitat, judged from something that is not Kelp Watch. |
| MPA boundaries | A step change in January 2012, inside the 2007–2019 climatology baseline. Protection status alters urchin and predator dynamics, so it is a confounder no temperature feature can absorb. |
| Administrative kelp beds and harvest status | Point Loma was commercially harvested for decades. Harvest is direct canopy removal and would read as environmental decline in an anomaly series — on `KELP:SAN-DIEGO`, the largest polygon in the registry. |

**The persistence layer corroborates the outlines; it must never redraw them.**
The `_verified` claims in `polygons.geojson` are circular by construction —
outlines derived from Kelp Watch cells, checked against the Kelp Watch aggregate
endpoint — so an independent layer is worth having. But an outline's one
binding property is that it reproduces its landed export line for line, and a
shape nudged toward another publisher's idea of where the bed stops would lose
that while still looking like an improvement.

### What is deliberately not adopted

Eelgrass `[ds1503]`, Shoreline Types `[ds3115]`, Saline Wetlands `[ds2864]` and
Estuarine Biotic Habitat `[ds2793]` describe habitats a giant kelp bed is not
in. They are named here because they sit adjacent in the BIOS habitat tree and
will be offered again to anyone who opens it.

**`EXTERNAL — West Coast Nearshore CMECS Substrate Habitat` is not a CDFW
dataset**, and neither is the DEM Global Mosaic; BIOS surfaces both from other
publishers. A revision pinned against BIOS for either would name the wrong
custodian, and for San Diego the CMECS layer is redundant with `ds3091`
besides. If bathymetry is wanted later, NOAA/NCEI is the custodian to pin.

### Access is unverified for BIOS, and checking it is implementation's first job

BIOS publishes through ArcGIS REST services, which return GeoJSON for a bbox
query — so the existing fetcher contract and `geopandas` cover it with no new
dependency (hard rule 8). No BIOS endpoint is recorded here because none has
been called, and every BIOS access detail above is a claim to check rather than
a finding.

### Two of the four layers are already reachable on CNRA, and were measured

The substrate and persistence layers do not have to come from BIOS. CNRA
publishes both as ArcGIS REST map services, and unlike the BIOS claims above
these were called on 2026-08-31:

| Layer | Service (under `https://gis.cnra.ca.gov/arcgis/rest/services/Ocean/`) |
|---|---|
| Nearshore seafloor substrate | `CSMW_San_Diego_Nearshore_Seafloor_Substrate/MapServer` layer 0 |
| Kelp persistence 1967–1999 | `CSMW_San_Diego_Kelp_Persistence/MapServer` layer 0 |

Both answer `?f=json` with one polygon layer, `maxRecordCount` 2000 and a Web
Mercator (3857) spatial reference, so a full pull paginates on `resultOffset`
and asks for `outSR=4326`. The substrate layer classifies into `descrip`:
Bedrock, Boulder, Cobble, Pebble/Gravel/Granule, Sand, Mud, Artificial
Substrate, Kelp Canopy Obscuring Seafloor, and no data.

**Both layers cover all six polygons**, counted by bbox intersect:

| Polygon | Persistence | Substrate |
|---|---|---|
| `KELP:LA-JOLLA` | 4,433 | 5,648 |
| `KELP:DEL-MAR` | 477 | 1,267 |
| `KELP:SOLANA-BEACH` | 927 | 2,102 |
| `KELP:ENCINITAS` | 1,197 | 2,705 |
| `KELP:SAN-DIEGO` | 9,465 | 14,004 |
| `KELP:IMPERIAL-BEACH` | 959 | 1,532 |

That settles a coverage doubt worth recording, because it will be raised again:
Mack (2022) states the SANDAG surveys did not cover his La Jolla Cove site, which
would put the two largest beds outside the layer. Whatever is true of the Cove,
it does not generalise to these outlines — La Jolla comes back 30.0% Bedrock and
0.1% no data, Point Loma 44.5% Bedrock and 6.7% no data. A count is an intersect
rather than a guarantee of usable classification, and the class breakdown is what
makes it evidence.

**`Kelp Canopy Obscuring Seafloor` is the trap in this layer, and it is large.**
It is a mapping artifact — the backscatter could not see the bottom — and it is
**33.7% of the La Jolla features and 24.3% of the Point Loma ones**. Counting it
as non-rock would strip a third of the kelpiest water out of a rocky-substrate
mask, which is the exact water docs/04 §4.5 is asking about, and would bias the
distance rings in the direction that most looks like a result. It is not rock
either. It is 2009-era evidence of canopy and has to be carried as its own class.

Two costs before either layer is used. Neither publisher exposes a revision, so
"what did this layer say when we used it" has no answer beyond a fetch date —
the pinning question PRD #93 asks. And both are static single-epoch products
(substrate ~2009, persistence 1967–1999), so neither can become a quarterly
feature; they are polygon attributes or they are nothing.

## CNRA open data (data.cnra.ca.gov)

The California Natural Resources Agency's CKAN portal, and a second publishing
route for material that also reaches BIOS rather than a separate body of data —
check whether a layer wanted from here is already covered by the entry above
before writing a second fetcher for it.

Three datasets were vetted, all from the South Coast MPA Baseline Study: diver
transect surveys of shallow rock and kelp forest (Pondella, Vantuna Research
Group), citizen-scientist transects of the same reefs (Freiwald, Reef Check
California), and nearshore substrate mapping from aerial multispectral imagery
(Svejkovsky, Ocean Imaging). None carries in-situ temperature or PAR, so
**nothing in this entry touches the observation schema**, the normalizer, or QC.

### It is 2011–2012, so none of it can be a quarterly feature

All three are baseline snapshots. Against a kelp record screened from 1984 on a
2007–2019 climatology baseline there is no series to build an anomaly from, so
urchin density — the biological driver no sensor in `sites.json` can see —
cannot enter `comparison.parquet` as a predictor, which is the thing it would
most be wanted for.

It also lands *before* the 2014–2016 marine heatwave, so it cannot drive the
docs/04 §4.2 event study either. What it can do is describe the state going in,
which is a docs/04 §6 interpretive limit rather than a model term.

### The San Diego tiles ship no raster

Verified 2026-08-30, by reading each archive's zip central directory from a
ranged GET of its last 256 KB — about 1 MB fetched rather than the 321 MB the
three archives hold.

| Archive | Size | Contents | Flown |
|---|---|---|---|
| `lajolla-pointloma.zip` | 81 MB | 8 PDF (4 Imagery, 4 IntClass) | 2012-06-25 |
| `encinitas-la-jolla.zip` | 183 MB | 24 PDF | 2012-11-12 |
| `imperialbeach.zip` | 57 MB | 8 PDF | 2012-06-25 |
| Anacapa Island `IntClass` (reference) | 1.3 MB | `.tif`, `.tfw`, `.ovr`, `.vat.dbf`, metadata | 2012-10-14 |

Forty PDFs across the three San Diego archives and not one georeferenced file.
The Anacapa archive, listed on the same page as a reference, is a complete
classified raster: GeoTIFF, world file, overviews, and the value-attribute table
carrying the habitat-class legend.

**So the raster exists as a product and was simply not published for this
coast.** That is the difference between a dead end and a request, and it is what
lets the request be specific: the `IntClass` raster set for
`LaJolla_PointLoma_06252012`, `Encinitas_LaJolla_11122012` and
`ImperialBeach_06252012`, in the form `SCR_Aerial_AnacapaIsland_10142012_IntClass.tif`
already takes.

**Two flight dates, two quarters.** June and November fall in 2012Q2 and 2012Q4,
and both `lajolla-pointloma` and `encinitas-la-jolla` cover La Jolla. Canopy is
strongly seasonal, so even once rasters arrive, anything derived across tiles
mixes quarters rather than describing one epoch.

### The canopy tables are annual, and keyed to MPAs rather than to beds

`yearly_kelp_coverage_by_mpa` carries 1999 and 2002–2012 canopy area — the
independent cross-check the CDFW entry above asks for, from a different sensor
and a different classifier than Landsat. Three costs before it can be used: it
is a PDF table and would have to be transcribed, it is annual where every other
kelp number here is quarterly, and it is summarised by MPA and subregion rather
than by the six beds, so joining it to a `polygon_id` needs a correspondence
that may not exist.

A transcribed table is a hand-made landing and takes the treatment the Kelp
Watch export takes — pinned, landed under `raw/`, the transcription itself
reviewable — rather than being typed into a notebook.

### Reef Check runs to the present, and that is the lead worth pulling

The published baseline is 2011–2012, but the RCCA program has run since 2006 and
the landing page offers later years on request. A 2006–present urchin density
series at San Diego sites would span the Blob and would be a real covariate
rather than a snapshot — the one thing in this entry that could change an
analysis rather than annotate one.

### The licence is not the licence every other source here has

MPA Monitoring Program terms: attribution required, and derivative use
encouraged **except commercially**. Every other source in this catalogue is
public domain or equivalent. Results may feed publications, so the constraint is
recorded per-source here rather than discovered at submission.

## Supplementary sources (recommended additions)

Satellite SST (NOAA OISST or NASA MUR via NOAA CoastWatch ERDDAP) provides
spatially continuous temperature to bridge point sensors and kelp polygons
and to validate project sensors. Upwelling indices CUTI and BEUTI (NOAA
SWFSC) summarize upwelling strength and nitrate flux at 1° latitude bins —
BEUTI is the closest available proxy for the nutrient supply that drives
Southern California kelp. ENSO indices (ONI/MEI, monthly text products)
tag El Niño quarters for event studies. Marine heatwave event definitions
follow the standard Hobday framework computed from the SST series (doc 04).
All are small, well-behaved downloads; each still gets a fetcher and a
registry entry like every other source.

## Sources considered and set aside

### SBC LTER reef bottom temperature (`knb-lter-sbc.13`) is the wrong coast

Reviewed 2026-08-28 and not adopted. It is easy to reach for, because it is
published by the same LTER site as the kelp product this project already
depends on and it measures the same quantity on the same instrument family as
the project sensors — continuous bottom temperature from Onset TidbiT loggers,
two per site offset to give 15-minute resolution, retrieved and replaced
bi-annually, ongoing since 2000.

**It covers nine reefs in the Santa Barbara Channel, at about 34.4° N.** Every
polygon and every site in this project is San Diego, at about 32.85° N — some
270 km south, on the other side of Point Conception's influence and in a
different upwelling setting. Joining it to the comparison table would pair kelp
with water that never touched it. That is the whole reason, and it is not a
close call: `sites.json` already declines to report `PROJ:TIDBIT-2` bias
against a reference 2.0 km away, on the narrower ground that the reference sits
13.4 m above the logger. Distance at this scale disqualifies more decisively
than depth did.

**It is not `knb-lter-sbc.74`, and the two are easy to confuse.** That is the
Landsat canopy product pinned in `polygons.geojson`, which spans Pt. Reyes to
Punta Abreojos and therefore does include this coast. A reader who finds an
`knb-lter-sbc` package and assumes it is the kelp one will reach the wrong
conclusion about whether it is usable here.

Two uses were weighed before setting it aside, and both are recorded because
each could look attractive again later:

- **An out-of-region replication of the doc 04 §4.1 lag screen.** Nine sites
  across roughly 24 years would test whether the screen's leading candidate
  survives outside San Diego, which is worth more than it sounds — that
  candidate currently rests on one station against two adjacent beds. Set aside
  on sequencing rather than on merit: it needs a second site registry, polygon
  set and climatology baseline, while doc 04 §4.5 — the project's own key
  question — still cannot run at all.
- **An empirical floor for the `sea_water_temperature` QC thresholds**
  (`https://github.com/cweber12/kelp-compare/issues/4`), whose fallback option
  is literature values. Same instrument at the same depth class over a record
  long enough to contain real internal bores, so it would show the statistics
  directly. Set aside because Santa Barbara bore statistics are not San Diego's:
  it could bound the thresholds but not set them, and that issue's nearer
  options — a SCCOOS/Scripps fetcher, or a winter deployment of the project's
  own logger — answer the question properly rather than approximately.

**No revision is pinned here, deliberately.** The description above was read
from DataONE's mirror, whose newest indexed revision is 19 and whose coverage
stops in 2013; the live package is far ahead of that. EDI itself returns 403 to
an anonymous caller on every method — re-confirmed on 2026-08-28 against
`readMetadata`, `listDataPackageRevisions` and `searchDataPackages`, the same
global lockout described under Kelp Watch above and tracked at
`https://github.com/cweber12/kelp-compare/issues/25`. Pinning a revision from a
stale mirror would be recording a number nobody has verified. If this package is
ever revisited, read it live first.

### Two CNRA South Coast baselines cover the wrong habitat

Reviewed 2026-08-30 and not adopted, both from the same South Coast MPA Baseline
Study as the datasets in the CNRA entry above.

**Mid-Depth Rocky and Soft-Bottom Ecosystems (2011–2012)** is ROV survey work
below the photic zone giant kelp occupies. It describes what is downslope of a
site rather than what is in the bed.

**Rocky Intertidal Ecosystems (2001–2014)** is intertidal, and these beds are
subtidal. Its span is the most tempting thing about it — fourteen years against
the two the adopted baselines carry — and span is not relevance.

### A San Diego source bundle, triaged 2026-08-31

An external document-review hand-off (`kelp_data_bundle_2026-08-31`) catalogued
~20 sources behind the **City of San Diego / SIO kelp forest monitoring
program** — a different project, the same water. Everything in it that this
project should adopt has been filed as an issue or recorded above; what follows
is what it should *not* adopt, so that the same PDFs are not re-triaged later.

**Already held here, and in a better form than the bundle recommends.** Its §4
(Scripps Pier) and §7 (the Landsat canopy product) are the SIO Shore Stations
and Kelp Watch entries above. Its §8 recommends the `seshat.datasd.org` per-year
CSVs for RTOMS; the RTOMS entry above records why the CeNCOOS ERDDAP feeds beat
them and should not be re-litigated. Its §7 processing recipe is bed bounding
boxes, which `polygons.geojson` supersedes.

**Its EDI access recipe does not work anonymously.** §7 marks the PASTA route
`PUBLIC, VERIFIED` and gives programmatic endpoints. Re-checked 2026-08-31:
`GET /package/eml/knb-lter-sbc/74` and `GET /package/eml` both still return
**403**, unchanged from 2026-08-26 and 2026-08-28. The bundle verified a landing
page, not the API. Nothing about
`https://github.com/cweber12/kelp-compare/issues/25` has changed.

**Not public, and with no home in the schema if it arrived.** Its §1b
(quadrat-level City/SIO transects), §1c (the 2002–03 Sea Grant whole-forest
survey), §1f (the 1990– urchin settlement series) and the 2016 canyon moorings
behind §8b are all human-request-only. Access is the smaller problem: these are
event-based, taxon-coded transect and size-frequency records with no
`timestamp / site_id / parameter / value / qc_flag` shape and no QARTOD
vocabulary. That is the same question
`https://github.com/cweber12/kelp-compare/issues/95` raises for Reef Check and
that PRD `https://github.com/cweber12/kelp-compare/issues/93` owns. None of them
lands in `observations/` by default.

**Literature constants, not data.** The bundle ships transcribed tables from
Parnell et al. 2005, Couto et al. 2026 and Mack 2022. They are keyed by reserve
or habitat, never by `site_id` or `polygon_id`, and they belong in a notebook's
interpretation or a citation rather than in a registry. The exception is the
Konotchick/Leichter temperature→nitrate constants, which are a derived-parameter
question rather than a source.

**Wrong subject or wrong coast**, and the bundle excludes them too — recorded
only so they are not re-opened: Carbajal-Martínez 2026 (the "La Jolla Beach" in
it is in Baja California), King 2022 (offshore groundwater), Timmer 2026
(northern Salish Sea), Daly 2026 (kelp biomechanics, no San Diego content),
Som 2015 (an intertidal management plan), Flick (a tidal-pattern explainer).

**Set aside on merit.** USACE NCMP topo-bathy LiDAR (§9c) offers a reproducible
roughness→rocky classifier, but it is calibrated at one site, stated valid only
to −15 m, and its ~1000 m offshore limit does not span the Point Loma bed. The
CNRA substrate layer recorded above answers the same question with less
machinery and was measured over all six polygons. Its §1e mid-depth ROV baseline
is the dataset already rejected under "Two CNRA South Coast baselines cover the
wrong habitat" above.

**The one it leaves genuinely open** is §1a, the Dryad archive for Parnell et al.
2026 (`10.5061/dryad.fttdz096d`, CC0): in-water kelp density at 20 City/SIO
transect sites, 1983–2023 — an independent measure of the *response variable
itself*, over a longer span than anything else here. It is not adopted and not
dismissed, because the sites are not the six Kelp Watch beds, the reports never
publish their coordinates, and it is `.RData`. That is PRD #93's question asked
on the response side, and it should be answered there rather than by a fetcher.

Two of the bundle's own gaps bear on any later use of it: it is missing
`san_diego_kelp_monitoring_site_tables.md`, the `site_code` legend everything in
it joins through, and its Wirewalker `chla` / `par` / `backscatter` channels
carry no units and are blocked behind an unanswered question to the authors.

## Cross-cutting fetcher rules

Every fetcher: writes the untouched payload to `raw/{source}/` before
parsing (re-parseable forever), is idempotent for a given site/date window,
maps source missing-value sentinels to null, converts to UTC and SI at the
normalizer boundary, attaches `source` and `fetch_run_id` to every row, and
fails soft — a source outage is logged in the run manifest and skipped, not
fatal. No fetcher writes anything except its own raw zone and the
observations zone.

### Ask before downloading

**A pulled window is re-fetched conditionally.** Landing is content-addressed,
so re-running on identical bytes has always been a no-op on disk — but the
bytes had to arrive first, which meant every re-run paid for the whole file
again. That is the wrong shape for a pipeline anything might retry.

Where a source supports it, the fetcher sends back the `ETag` and
`Last-Modified` a previous run recorded, and a `304 Not Modified` becomes
`NotModified` — one round trip, no payload, and the window is recorded as
`unchanged`. Verified against NDBC on 2026-08-27: a plain `GET` of the 2015
archive is 933,320 bytes; the same request carrying either validator is `304`
and zero bytes. End to end, that run went from 1.61 s to 0.27 s.

Three rules make this safe rather than merely fast:

- **A stale validator still gets the whole file.** This is a cache check, never
  a promise not to look. NDBC does re-issue an archive year after QC, and a
  mechanism that could hide that would be worse than the download it saves.
- **A validator is recorded only after the window's rows are written**, so it
  means "fully ingested at this version". Recorded at landing time it would let
  the next run step past a window whose parse or write had failed — bytes on
  disk, no rows in the zone.
- **`unchanged` is not `skipped`.** Skipped means a hole in the record and is
  noted as a gap; unchanged means the record is complete and current. Neither
  sets the exit code, but conflating them would put a phantom gap in the
  manifest of every re-run.

Skipping an unchanged window is correct, not just cheap: the bytes are in
`raw/` and the rows are in `observations/`. Re-parsing landed bytes after a
parser or registry change is `rebuild`'s job, not ingest's.

The tokens live in `data/cache/` (doc 03), which is a cache and not a record —
deleting it costs one re-download and nothing else.

### Saying who is asking

Every request carries `User-Agent: kelpcompare/{version} (+{contact})`. NDBC's
`robots.txt` publishes a webmaster address, which is to say they expect to be
able to reach whoever is pulling; `python-requests/2.x` gives them nobody to
reach and is what gets throttled.

The contact comes from the `KELPCOMPARE_CONTACT` environment variable, never
from a source file — **this repository is public, and an address committed to
it is an address that has been published.** With none set the header still
names the project and version, which beats being anonymous and beats a run
that will not start.

NDBC publishes no crawl delay and no disallow for `/data/`, and the whole
2007–2025 LJAC1 archive is about 15 MB. Volume was never the concern; the
re-download was.
