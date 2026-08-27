---
name: data-source-access
description: How to correctly query, download, and parse each external data source in this project — Kelp Watch exports, NDBC realtime/stdmet text files, NOAA CO-OPS Data API, SCCOOS ERDDAP, CDIP, and supplementary sources (OISST/MUR SST, BEUTI/CUTI, ONI). Use when writing or modifying any fetcher, debugging a failed or empty download, adding a new station/dataset ID, or interpreting raw source formats, units, sentinels, or missing-data conventions.
---

# Data Source Access

Authoritative quick reference for fetcher work. The full per-source contract
is docs/02-data-source-catalog.md — read it for anything not covered here.
Verify live details against provider docs when behavior surprises you;
public endpoints drift.

## Cross-cutting rules (from docs/02)

Land the untouched payload in `data/raw/{source}/` before parsing. Map
sentinels to null at parse time. Convert to UTC + SI only in the normalizer.
Fail soft: log outages to the manifest, never crash the run. Pin station and
dataset IDs in `data/registry/`, never in code.

Store only the parameters the station declares in `measured_parameters`
(`sites.json`). A fixed-column format carries every column for every station,
so a station with no wave sensor still reports sentinel wave columns — and
storing those is millions of rows that say nothing. Never infer this from the
payload: a sensor that failed for a year looks identical to one that does not
exist, and only the first should keep its (missing-flagged) rows. A station
with no declaration is *undeclared*, not empty — store everything recognised
and warn.

**Ask before downloading.** Where a source supports conditional requests, send
back the `ETag` / `Last-Modified` a previous run recorded and treat `304` as
`NotModified` -- one round trip, no payload, window recorded as `unchanged`
rather than `skipped` (which means a gap). Three rules keep it safe: a stale
validator still gets the whole file, so this can never mask an upstream
revision; the validator is recorded only *after* the rows are written, so it
means "fully ingested at this version" rather than "bytes landed"; and the
tokens live in `data/cache/`, a cache and not a record, where losing them costs
one re-download. Verified on NDBC: 933,320 bytes and 1.61 s becomes 0 bytes and
0.27 s.

**Say who is asking.** `User-Agent: kelpcompare/{version} (+{contact})`, contact
from `KELPCOMPARE_CONTACT`, never committed -- the repo is public.

## NDBC

- Realtime (last ~45 days): `https://www.ndbc.noaa.gov/data/realtime2/{STATION}.txt`
- Historical: annual stdmet archives per station (gzipped fixed-width)
- Two header rows (names, then units). Sentinels: `MM`, `99.0`, `999`,
  `9999` variants per column — check column-specific conventions.
- Realtime and historical layouts differ slightly; parse them separately.
- Units per https://www.ndbc.noaa.gov/faq/measdes.shtml — do not assume SI.
- Record the measurement depth/height for temperature in the registry;
  buoy hull temp ≠ shore-station intake depth.

## NOAA CO-OPS Data API

- Base: `https://api.tidesandcurrents.noaa.gov/api/prod/datagetter`
- Products used: `water_level`, `water_temperature`, `predictions`
- Always pass an explicit `datum` (project standard: MLLW), `time_zone=gmt`,
  `units=metric`, and paginate — high-frequency products have per-request
  span limits (~1 month). Local station: 9410230 (La Jolla).

## SCCOOS ERDDAP

- Server: `https://erddap.sccoos.org/erddap` (tabledap for stations/HABs,
  griddap for gridded products). Use the `erddapy` package.
- Dataset IDs live in the registry (they occasionally change upstream).
- Time constraints in ISO8601 UTC: `&time>=2026-01-01T00:00:00Z`.
- Some datasets carry native QC columns — map them into our flag scheme
  in the fetcher; do not drop them.
- Discovery/browse: https://data.caloos.org (CalOOS portal over the same holdings).

## Kelp Watch

- Quarterly canopy per user-drawn polygon, exported via the site; land the
  CSV in `raw/kelpwatch/` with the polygon_id in the filename.
- Missing quarter (clouds) ≠ zero canopy. Parser must preserve nulls.
- Q1=Jan–Mar, Q2=Apr–Jun, Q3=Jul–Sep, Q4=Oct–Dec.

## CDIP / supplementary

- CDIP waves: NetCDF via THREDDS/ERDDAP; open with xarray.
- SST: OISST or MUR via NOAA CoastWatch ERDDAP (griddap; subset before
  download, these grids are large).
- BEUTI/CUTI (upwelling/nitrate flux) and ONI (ENSO): small flat files from
  NOAA SWFSC / CPC; still fetch through a fetcher with raw landing.

## Debugging checklist for empty/failed pulls

1. Is the station/dataset ID still valid? (Check the registry note and the
   provider's station page.)
2. Date window inside the dataset's actual coverage? (ERDDAP errors on
   out-of-range constraints rather than returning empty.)
3. For CO-OPS: datum + product + interval combination supported for that
   station?
4. For NDBC realtime: only ~45 days exist; older needs stdmet archives.
5. Rate limiting: back off and retry once; then record the gap and move on.
