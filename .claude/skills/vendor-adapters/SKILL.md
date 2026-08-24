---
name: vendor-adapters
description: Contract and format knowledge for parsing project-sensor vendor files (currently HOBO/HOBOconnect xlsx and csv exports from Onset TidbiT loggers). Use when writing or modifying anything in src/kelpcompare/adapters/, when an ingest validation check fails or a file is quarantined, when a new logger brand or HOBO model needs support, or when interpreting HOBO Data/Events/Details sheets, deployment windows, or edited files.
---

# Vendor Adapters

Full spec: docs/06-project-sensor-ingest-spec.md. Reference files (an
original HOBOconnect export and a hand-edited copy) are in `tests/fixtures/`
— parse them, don't guess.

## The contract (every adapter)

Implement three functions:
- `sniff(path) -> bool` — cheap check: can I parse this file?
- `parse(path) -> RawSeries` — measurements plus per-series metadata
- `metadata(path) -> dict` — serial, model, interval, events, export stats

Adapters never convert timezones or units and never trim deployment
windows — that's the shared normalizer's job, driven by the registry.
Adapters only extract faithfully.

## HOBOconnect xlsx format facts (verified against fixtures)

- Three sheets: `Data`, `Events`, `Details`.
- `Data` columns: `#`, `Date-Time (PDT)` — timezone token lives in the
  header text — and one or more series columns named `{name} , {unit}`
  (e.g. `Tidbit 1 , °F`). Unit is user-configurable (°F or °C): parse it,
  never assume.
- `Events` rows: `Host Connected`, `Started`, `End of File` with
  timestamps — the deployment lifecycle.
- `Details`: product/serial/firmware, logging interval, deployment number,
  and export-time series statistics (n, min, max, avg, stddev).
- Filename encodes `{name}__{serial}__{readout-datetime}`.
- Hand-edited files exist: tolerate extra/unnamed columns, formula cells,
  float or missing `#` values, trimmed rows. Only rows with a valid
  datetime are measurements.

## Ingest policy (enforced by the registry gate)

1. Prefer original exports; mark edited files `provenance: edited`.
2. No ingest without a registry deployment record matching the serial,
   with timezone, in-water window, and `series_map` (sensor name from the
   header -> controlled parameter). Otherwise quarantine.
3. Validation on originals: parsed n/min/max/mean must equal the Details
   statistics; first/last samples must match Started/End-of-File events;
   observed spacing must match the configured interval. Skip
   stats/consistency checks on edited files (they fail by construction)
   and warn.
4. Overlapping readouts of the same serial: keep both raw, dedupe
   deterministically downstream.

## Known hazards

- DST: header tz token (PDT/PST) vs. deployments spanning the November
  transition is UNVERIFIED — treat token as fixed offset, warn on registry
  mismatch, and flag the first winter file for manual review.
- Multi-series models (temp + light): iterate all `{name} , {unit}`
  columns.
- CSV exports carry the same logical content; share parsing logic behind a
  different loader.
