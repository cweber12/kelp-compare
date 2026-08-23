# 05 — Architecture Decision Records

Short records of the load-bearing choices, in ADR format, so the team can
challenge the reasoning rather than the conclusion. All are **Proposed**
pending review.

---

## ADR-001: Storage — DuckDB over partitioned Parquet (no database server)

**Status:** Proposed · **Date:** 2026-08-23 · **Deciders:** project lead + team review

### Context
Single local machine, one operator, analytical (not transactional)
workload, data volume in the low gigabytes, columnar time-series queries,
and a hard requirement that the whole stack be shareable/reproducible by
teammates without ops work.

### Options

**A — SQLite.** Complexity: low. Zero-ops, universally understood.
Cons: row-oriented, weak for analytical scans and window functions at
scale; no native Parquet; wide-aggregation queries get awkward.

**B — DuckDB + Parquet (chosen).** Complexity: low. In-process, zero-ops,
queries Parquet in place, excellent window/aggregation SQL, first-class
pandas/Arrow interop; Parquet doubles as the archival and sharing format —
a teammate can read the same files with pandas alone. Cons: single-writer;
younger tool; team may be less familiar (mitigated: it's just SQL).

**C — PostgreSQL (+ PostGIS/Timescale).** Complexity: medium-high.
Multi-user, mature geospatial. Cons: a running service to install, secure,
back up, and migrate — pure overhead for one local user; geospatial needs
here are light enough for geopandas.

### Decision & consequences
B. Easier: reproducibility (data = files), sharing, backup (copy a
directory), future cloud move (Parquet lifts to S3 unchanged). Harder:
concurrent writes (irrelevant now), row-level updates (we don't do them —
zones rebuild wholesale). Revisit if multi-user querying arrives.

---

## ADR-002: Orchestration — CLI commands, manual/cron, no scheduler daemon

**Status:** Proposed · **Date:** 2026-08-23

### Context
Kelp data updates quarterly; sensor downloads are manual events; nothing
is real-time. The operator runs analyses in sessions, not continuously.

### Options
**A — Airflow/Prefect/Dagster.** Real dependency graphs, retries, UIs —
and a resident service with its own database, upgrades, and failure modes.
Massive overkill for a quarterly cadence on a laptop.
**B — Plain CLI (`kelpcompare ingest|qc|features|rebuild`) run manually,
optionally via cron/launchd (chosen).** Complexity: minimal. Dependency
order is encoded in the CLI itself; manifests provide the audit trail a
scheduler UI would.
**C — Makefile/Snakemake.** Nice rebuild semantics; adds a second tool and
syntax for marginal gain over a well-structured CLI.

### Decision & consequences
B. Easier: onboarding (one command), debugging (run any stage directly).
Harder: no automatic retries/alerting — acceptable because staleness is
measured in weeks and the manifest records gaps. Revisit if ingestion ever
needs to run unattended daily.

---

## ADR-003: Presentation — Streamlit locally, docs/exports for the team

**Status:** Proposed · **Date:** 2026-08-23

### Context
Interactive exploration is single-user on the local machine; the team
consumes documents, figures, and notebooks, not a live app.

### Options
**A — Jupyter only.** Lowest effort; but ad-hoc map/lag exploration in
notebooks gets repetitive and stateful.
**B — Streamlit (chosen).** Pure-Python, launches on demand, pairs
naturally with DuckDB/plotly, trivial site-map + linked time series + lag
explorer pages; if stakeholders ever need it, it deploys behind auth
mostly as-is.
**C — Dash/Panel.** Comparable; more boilerplate (Dash) or smaller
community (Panel) for no benefit at this scope.
**D — Static site (Plotly.js/JS frontend).** Zero server but a second
language and build chain; wrong trade for a single local user.

### Decision & consequences
B, with the rule (doc 01) that the dashboard reads only the feature/
comparison tables and computes no statistics of record. Easier: fast
iteration, shareable screenshots/exports. Harder: not a collaboration
surface — by design; the docs and notebooks are.

---

## ADR-004: QC framework — QARTOD via `ioos_qc`, flags not deletions

**Status:** Proposed · **Date:** 2026-08-23

### Context
The project's scientific credibility rests on non-NOAA/SCCOOS sensors
being defensible. Reviewers in this domain know QARTOD; a bespoke QC
scheme invites doubt.

### Options
**A — Hand-rolled thresholds.** Simple, but non-standard and hard to
defend in review.
**B — QARTOD tests via `ioos_qc` (chosen).** The operational standard for
U.S. ocean observing; open implementation; flag semantics reviewers
recognize; same tests applicable to ingested public feeds for consistency.
**C — Outlier ML / anomaly detection.** Opaque, unnecessary at these
volumes, and validation burden exceeds the problem.

### Decision & consequences
B, with flags stored alongside data and filtering done at query time so QC
choices are reversible and auditable, plus per-deployment neighbor
validation (doc 04 §1). Easier: publication and review. Harder: some
up-front work tuning test thresholds per parameter — recorded in the
parameter registry, not code.

---

## ADR-005: Diagrams and docs — Markdown + embedded Mermaid in git

**Status:** Proposed · **Date:** 2026-08-23

### Context
Documents must be reviewable by a small team, diffable, and kept next to
the code they describe.

### Options
**A — Word/Google Docs.** Familiar; but drifts from the repo, doesn't
diff, and diagrams become pasted images that rot.
**B — Markdown with Mermaid diagrams, versioned in git (chosen).**
Renders on GitHub/GitLab including the diagrams, reviews via PR, diagrams
edited as text so they stay current.
**C — Draw.io/Lucidchart assets.** Prettier diagrams; binary-ish artifacts
that fall out of sync and need extra tooling.

### Decision & consequences
B. Easier: doc review = code review; single source of truth. Harder:
non-technical stakeholders need rendered exports — mitigated by exporting
PDF/HTML from the same markdown when needed.
