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
is the signal, and 25-40 km of coastline is the confound.

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

### Distance is the standing caveat

These moorings are 25-40 km south of La Jolla and sit on outfall diffusers.
They are a **depth reference, not a neighbor**: doc 04's neighbor validation
compares an instrument against a nearby one, and nothing here is nearby. What
they can support is the question `sites.json` currently answers in prose — how
far a 3.4 m shore reading sits from water at thermocline depth in this region,
and how that gap moves with season. They also cannot validate the project's own
loggers, whose deployments begin in July 2026, after every RTOMS record ends.

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
