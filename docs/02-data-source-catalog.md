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
| CDIP | Wave climate | THREDDS/ERDDAP NetCDF | 30 min | NetCDF |
| CDFW / marineBIOS | GIS context, historical kelp surveys | Downloaded shapefiles/services | Static / annual | Shapefile, GeoJSON |
| Supplementary (SST, indices) | Gap-filling, regional drivers | ERDDAP / flat files | Daily / monthly | NetCDF, text |

## Kelp Watch

The response variable. Quarterly kelp canopy at 30 m resolution for the west
coast since 1984, aggregated to a selected geometry.

Everything below the next two subsections was verified against real exports on
2026-08-26; the files are recorded whole in `tests/fixtures/kelpwatch/`.

### The route: a hand-downloaded CSV export

**The operator selects a kelp bed on kelpwatch.org, exports its quarterly CSV,
and drops the file in `raw/kelpwatch/incoming/`.** This is a file-drop source
like the project sensors, not a pulled one — there is no fetcher and no network
path, and `ingest --source kelpwatch` lands the file and writes a manifest
exactly as the HOBO ingest does.

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

## CDIP (Coastal Data Information Program)

Wave climate — significant wave height, period, direction — from
Scripps-operated buoys, served as NetCDF via THREDDS/ERDDAP. Waves matter
because large winter swell events physically remove canopy, a mechanism
completely separate from heat stress; without wave data, storm-driven kelp
loss would be misattributed to temperature. Ingested per-station like NDBC;
the feature builder derives event counts above height thresholds and
maximum event duration per quarter.

## CDFW / marineBIOS

GIS context rather than time series: MPA boundaries, administrative kelp
bed designations, and CDFW's historical aerial/multispectral kelp canopy
surveys (irregular years). Landed once as shapefiles/GeoJSON into
`raw/gis/`, loaded with geopandas. Two uses: spatial joins (which MPA/kelp
bed contains each polygon and sensor) and an independent cross-check of
Kelp Watch canopy in overlapping years — agreement there strengthens any
claim built on the Landsat product.

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
