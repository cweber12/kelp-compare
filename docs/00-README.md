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
| 05 | [Architecture Decision Records](05-architecture-decisions.md) | Why DuckDB+Parquet, why no scheduler daemon, why Streamlit, why QARTOD |
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

Not built yet: the climatology and flat-line tests and neighbor validation
(doc 04 §1 records why each waits), the remaining public-source fetchers
(doc 02), quarterly features and anomalies (doc 04 §2–3), the comparison table,
the notebooks, and the dashboard.
