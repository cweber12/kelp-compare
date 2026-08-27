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
| Kelp Watch | Baseline response variable | EDI data package `knb-lter-sbc.74` — **authentication required** | Quarterly | NetCDF |
| Project sensors | Primary predictors under evaluation | Local files from loggers | ~minutes | Vendor CSV |
| NDBC | Reference met/ocean observations | HTTPS text files | 6 min – 1 hr | Fixed-width text |
| NOAA CO-OPS | Water level, coastal water temp | REST API (JSON/CSV) | 6 min / hourly | JSON, CSV |
| SCCOOS / CalOOS | Shore stations, HABs, currents | ERDDAP (tabledap/griddap) | Varies | CSV, NetCDF, JSON |
| CDIP | Wave climate | THREDDS/ERDDAP NetCDF | 30 min | NetCDF |
| CDFW / marineBIOS | GIS context, historical kelp surveys | Downloaded shapefiles/services | Static / annual | Shapefile, GeoJSON |
| Supplementary (SST, indices) | Gap-filling, regional drivers | ERDDAP / flat files | Daily / monthly | NetCDF, text |

## Kelp Watch

**Not implemented, and currently blocked on access** —
https://github.com/cweber12/kelp-compare/issues/25.

The response variable. Quarterly kelp canopy at 30 m resolution for the west
coast since 1984.

### The source of record is the published dataset, not the website

SBC LTER **`knb-lter-sbc.74`** on the Environmental Data Initiative — "Time
series of quarterly NetCDF files of kelp biomass in the canopy from Landsat 5,
7 and 8, since 1984 (ongoing)". It is the data the kelpwatch.org platform is
built on, with a revision number and a DOI attached.

The kelpwatch.org UI export was rejected as the source of record: it is
hand-driven and unversioned, so `kelpcompare rebuild` cannot re-derive it, the
polygon geometry lives in a browser session rather than in the repository, and
there is no revision to cite. Doc 01 §2 requires regenerating everything from
raw with one command, and that breaks at the first step. The undocumented
backend API the map UI consumes was rejected too — unversioned, free to change
without notice, and nothing to cite. Report card PDFs are methodology
references, not a data source.

The revision is pinned in the registry rather than in code, so moving to a newer
one is a reviewable data change and "whatever is current today" can never
quietly become the source of record.

### Access requires authentication (verified 2026-08-26)

**Every PASTA REST method returns HTTP 403 to an anonymous caller** — reads,
and the calls that merely list what exists:

| Request | Response |
|---|---|
| `GET https://pasta.lternet.edu/package/eml/knb-lter-sbc/74` | 403, `listDataPackageRevisions` |
| `GET .../package/eml/knb-lter-sbc/74/18` | 403, `readDataPackage` |
| `GET .../package/metadata/eml/knb-lter-sbc/74/18` | 403, `readMetadata` |
| `GET .../package/data/eml/knb-lter-sbc/74/18` | 403, `listDataEntities` |
| `GET .../package/eml` | 403, `listDataPackageScopes` |

The denial is global rather than specific to this package, and
`portal.edirepository.org` serves a login page for `mapbrowse`,
`metadataviewer` and `archiveDownload` alike. This is policy, not an outage:
EDI requires authentication for all REST API access from **2026-07-30**, as a
deterrent to denial-of-service attacks and scraping. The API documentation at
`pastaplus-core.readthedocs.io` still shows anonymous examples and is stale on
this point — do not take it as evidence the endpoint is open.

Consequence for this repository: the pinned revision belongs in the registry as
planned, but a credential cannot — `data/registry/` is committed and the repo is
public. How the credential reaches the fetcher is an open decision recorded in
issue #25.

### What the dataset holds (from its published metadata, 2026-08-26)

Retrieved from DataCite for DOI `10.6073/pasta/93b47266b20bc1782c8df9c36169e372`,
which resolves to **revision 16**:

- **A single NetCDF file**, holding the quarterly area and biomass means for
  each Landsat pixel across the three sensors.
- **Three measured quantities**: canopy area of giant kelp (*Macrocystis
  pyrifera*), canopy area of bull kelp (*Nereocystis luetkeana*), and canopy
  biomass of giant kelp (wet weight, kg).
- **Different spatial extents per quantity.** Area covers all coastal Baja
  California, California and Oregon including offshore islands; biomass covers
  Año Nuevo, CA south to the southern range limit in Baja — the range where
  giant kelp is the dominant canopy former. A polygon north of Año Nuevo
  therefore has area and no biomass, which is a property of the product, not a
  gap in the record.
- **Per-pixel metadata in the same file**: the number of Landsat estimates each
  quarterly mean was derived from, the number from each sensor, the standard
  error of the estimate, spatial coordinates, and date.
- **Irregular temporal coverage by design.** Each instrument repeats every 16
  days, but cloud cover, instrument failure and mission length (TM 1984–2011,
  ETM+ 1999–present, OLI 2013–present) make the actual coverage uneven.
  ETM+ scan-line-corrector gaps were filled by a synchrony-based method
  upstream.

**Not verified, and not to be guessed at:** the file's variable names, its
dimensions and whether it is a pixel list or a raster, its time encoding, its
missing-value encoding, its size, its licence, and which revision is current.
All of those need the payload, and the payload needs a credential. Issue #24
records that landing the real file and recording a fixture from it is the first
slice of the ingest, precisely so none of this is assumed.

Recorded in issue #24 from the literature but **not** verified here: the product
treats a whole 10 × 10 km cell as missing for a quarter when more than 25% of
its pixels lack a cloud-free acquisition.

### Quirks to encode when the parser is written

Quarters with insufficient cloud-free Landsat coverage are **missing, not
zero** — the parser must distinguish "no kelp" from "no observation", and
winter quarters are the most affected (hard rule 3). Canopy area and biomass
move seasonally by nature, so all analysis uses the anomaly transform (doc 04
§3). The product does distinguish giant from bull kelp, but in the San Diego
region it is effectively giant kelp; which quantity is the response variable is
the notebook's choice, so all three are stored.

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
same sensor — `PROJ:YELLOW-BUOY` lists both in `neighbor_refs`.

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
