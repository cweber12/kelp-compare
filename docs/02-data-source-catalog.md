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
| Kelp Watch | Baseline response variable | Web UI polygon export (CSV); backend API | Quarterly | CSV |
| Project sensors | Primary predictors under evaluation | Local files from loggers | ~minutes | Vendor CSV |
| NDBC | Reference met/ocean observations | HTTPS text files | 6 min – 1 hr | Fixed-width text |
| NOAA CO-OPS | Water level, coastal water temp | REST API (JSON/CSV) | 6 min / hourly | JSON, CSV |
| SCCOOS / CalOOS | Shore stations, HABs, currents | ERDDAP (tabledap/griddap) | Varies | CSV, NetCDF, JSON |
| CDIP | Wave climate | THREDDS/ERDDAP NetCDF | 30 min | NetCDF |
| CDFW / marineBIOS | GIS context, historical kelp surveys | Downloaded shapefiles/services | Static / annual | Shapefile, GeoJSON |
| Supplementary (SST, indices) | Gap-filling, regional drivers | ERDDAP / flat files | Daily / monthly | NetCDF, text |

## Kelp Watch

The response variable. Quarterly kelp canopy observations at 30 m
resolution for the west coast since 1984, downloadable through the site by
selecting a region and time frame; the platform exposes its data through a
backend API that the map UI consumes. For planning purposes the workflow
is: define one or more analysis polygons around the sensor sites (and
control polygons farther away), export the quarterly series per polygon,
and land the CSVs in `raw/kelpwatch/`.

Quirks to encode in the fetcher/parser: quarters with insufficient
cloud-free Landsat coverage are missing, not zero — the parser must
distinguish "no kelp" from "no observation," and winter quarters are the
most affected. Canopy area and biomass move seasonally by nature; all
analysis uses the anomaly transform (doc 04). The Landsat product does not
distinguish giant kelp from bull kelp; in the San Diego region this is
effectively giant kelp. Report card PDFs are methodology references, not a
data source.

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

Reference meteorological and oceanographic observations. Realtime data for
roughly the last 45 days per station at
`https://www.ndbc.noaa.gov/data/realtime2/{STATION}.txt`; multi-year
archives as annual standard meteorological (`stdmet`) files. Both are
fixed-width text with two header rows (names, units). Candidate stations
near the study area: LJAC1 (La Jolla shore station) and nearby buoys such
as Scripps Nearshore; the final station list lives in the site registry,
chosen by distance and parameter coverage. Quirks: missing values are
sentinel codes (`MM`, `999`, `99.0` variants) that must be mapped to null,
units follow the NDBC measurement description page and are not SI
throughout (wind in m/s, pressure in hPa, temp in °C — verify per column),
station instrumentation changes over the years, and realtime vs. historical
files differ slightly in layout. Water temperature depth differs by
platform (shore station intake vs. buoy hull) and must be recorded in the
site registry, since comparing a 1 m buoy temp to a 10 m logger temp is a
real analysis error we have to prevent structurally.

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
