# Kelp Forest Environmental Comparison Project — Planning Documents

This document set describes the planned architecture for a system that compares
Kelp Watch quarterly kelp canopy data against environmental time series from
independently placed sensors (temperature first, other types later),
supplemented by public data from NDBC, NOAA CO-OPS, SCCOOS/CalOOS, CDIP, and
CDFW/marineBIOS.

The system runs entirely on a single local machine and is operated by one
researcher. These documents exist so the design can be reviewed and understood
by a small research team, and they stay ahead of the code: a change to the
observation schema, a storage zone, or a feature definition updates the matching
document in the same commit.

## Reading order

| # | Document | What it covers |
|---|----------|----------------|
| 01 | [System Architecture](01-system-architecture.md) | Goals, constraints, the three-layer design, component and data-flow diagrams |
| 02 | [Data Source Catalog](02-data-source-catalog.md) | Every external source: access method, format, cadence, units, quirks |
| 03 | [Data Model](03-data-model.md) | Storage layout, common observation schema, site registry, quarterly feature tables |
| 04 | [Analysis Methods](04-analysis-methods.md) | QA/QC plan, quarterly feature definitions, statistical methods and their assumptions |
| 05 | [Architecture Decision Records](05-architecture-decisions.md) | Why DuckDB+Parquet, why no scheduler daemon, why Streamlit, why QARTOD, where feature configuration lives |
| 06 | [Project Sensor Ingest Spec](06-project-sensor-ingest-spec.md) | HOBO export anatomy, vendor adapter pattern, deployment-window policy, ingest validation |

## One-paragraph summary of the design

A Python package (`kelpcompare`) ingests data from each source through a
per-source fetcher, normalizes everything into a single long-format
observation schema, and lands it as partitioned Parquet files queried through
DuckDB. A QA/QC stage applies QARTOD-style tests so the project's own
(non-NOAA, non-SCCOOS) sensors are defensible. An aggregation stage collapses
high-frequency observations into quarterly ecological features (heat stress
days, minimum temperature as a nitrate proxy, wave-event counts, anomalies)
aligned to Kelp Watch's Q1–Q4 calendar. Analysis lives in versioned Jupyter
notebooks; a local Streamlit dashboard presents linked maps and time series.
Everything is reproducible from raw files with one command.

## Status

All documents are **drafts for review**. Decisions marked *Proposed* in the
ADRs are open for team input.

Implementation has started, one vertical slice at a time. Built so far: the
`hobo_xlsx` vendor adapter and its validation checks (doc 06), the site and
parameter registries, the three storage zones with the observation writer and
run manifests (doc 03), the normalizer, and `kelpcompare ingest --source
project` joining them end to end — a HOBO export becomes queryable
UTC/SI observations with a manifest recording every check.

Also built: the QARTOD QC stage, `kelpcompare qc` (doc 04 §1, ADR-004). It
re-derives `qc_flag` and `qc_tests` in place from gross-range, spike, and
rate-of-change tests whose thresholds live in `parameters.json`, never
relaxing a verdict ingest already recorded and never deleting a row. On the
reviewed deployment it passes all 3,022 in-water readings and catches the
install transient with two independent tests, as doc 06 §5 check 6 predicted.

Also built: the first public-source fetcher, `kelpcompare ingest --source
ndbc` (doc 02). It pulls NDBC realtime and annual stdmet archives, lands the
untouched payload, maps five columns to controlled parameters, and writes
observations that the existing QC stage evaluates with no code of its own —
the first evidence that the doc 03 schema generalizes past one vendor file.

Also built: the quarterly feature builder, `kelpcompare features` (doc 04
§2–3, ADR-006). It turns a stream of 10-minute and hourly readings into one
row per QC series per Kelp Watch quarter — the distribution, the ecological
threshold features, and the coverage bookkeeping that says whether a quarter
is worth believing — plus the fixed 2007–2019 climatology and the anomalies
taken against it, both written as a pure function of the observations zone and
`registry/features.json`. A quarter below the coverage floor is flagged
unusable rather than dropped, the same discipline QC applies to rows.

Also built: the analysis polygon registry (doc 03) and the climatology
generalised over its series key. A canopy value belongs to a polygon rather
than to a site, so the kelp half of the features zone keys on `polygon_id`;
`polygons.geojson` is where those areas are declared, along with the export
each one's rows arrive in and the upstream dataset revision they came from.
The climatology and anomaly code now takes its series key from its caller, so
kelp and environmental anomalies come from one implementation and cannot drift
apart — the environmental tables are byte-identical across the change.

Also built: the Kelp Watch ingest, `kelpcompare ingest --source kelpwatch`
(doc 02). The response variable finally enters the pipeline. Exports are
downloaded by hand from kelpwatch.org and dropped in `raw/kelpwatch/incoming/`,
because the published data package they are a view of now sits behind an
authentication wall; the ingest lands them content-addressed under their
revision and polygon, and writes a manifest. It is the one ingest that writes
no observations — a canopy value belongs to a polygon, and that zone is keyed
on `site_id`.

Its parser is where the source's headline quirk is handled: **a quarter nobody
could see is written as a zero, not as a blank**, contradicting the product's
own published field dictionary. Only the cloud-free cell count tells a cloud gap
from a genuinely empty bed, and reading the value column alone would fabricate
canopy measurements — in winter, where the gaps are, and worst in marginal beds,
where zero is the normal reading. Across the six San Diego county beds exported,
that is 44 fabricated zeros avoided against 329 real ones kept.

Also built: the kelp half of the features zone and the comparison table
(doc 03, doc 04 §2–4). `kelpcompare features` now writes five tables:
`quarterly_kelp.parquet` — one row per polygon per quarter, with the coverage
bookkeeping that says whether to believe it — `climatology_kelp.parquet`, and
`comparison.parquet`, the join of kelp at quarter *t* against every
environmental feature anomaly at *t−lag* for lags 0–4. The environment leads and
kelp responds; the lagged quarter is written on every row so the direction is
checkable rather than assumed.

**This is the first table in the project with real anomalies in it.** The Kelp
Watch record runs 1984–2026, so every polygon clears the ten-year minimum with
all thirteen baseline years contributing, while no environmental series yet
spans the window at all. It is therefore also the first exercise of the shared
climatology against a series long enough to have one.

On the six exported San Diego county beds: 1,020 polygon-quarters, 961 usable,
44 carrying null for a cloud gap, 976 anomalies computed, and 15,300 comparison
rows.

Not built yet: the climatology and flat-line QC tests and neighbor validation
(doc 04 §1 records why each waits), the remaining public-source fetchers
(doc 02), `kelpcompare rebuild`, the notebooks, and the dashboard. The analysis
ladder (doc 04 §4) can now start: the lag screen is a query against
`comparison.parquet` rather than a script.

Not yet possible: doc 04 §4.5's spatial signal test, the project's key question.
It needs polygon geometry, which `polygons.geojson` records as null for all six
beds — they are Kelp Watch's own named bed selections and their outlines have
not been recorded here — and it needs the yellow buoy's position, which
`sites.json` records as unverified.

Deferred rather than abandoned: fetching the published SBC LTER data package
directly. It carries giant and bull kelp separately plus biomass, at per-pixel
resolution, and could be refreshed by `rebuild` without a human in the loop —
none of which the UI export offers. It needs an EDI account (issue #25). When
one arrives it becomes a second route to the same product rather than a
replacement: the schema, the polygon registry and the shared climatology do not
care which route the numbers took.
