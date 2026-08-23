# Kelp Forest Environmental Comparison Project — Planning Documents

This document set describes the planned architecture for a system that compares
Kelp Watch quarterly kelp canopy data against environmental time series from
independently placed sensors (temperature first, other types later),
supplemented by public data from NDBC, NOAA CO-OPS, SCCOOS/CalOOS, CDIP, and
CDFW/marineBIOS.

The system runs entirely on a single local machine and is operated by one
researcher. These documents exist so the design can be reviewed and understood
by a small research team before any code is written.

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
ADRs are open for team input. Nothing has been implemented yet.
