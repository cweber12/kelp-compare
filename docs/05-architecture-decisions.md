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

---

## ADR-006: Feature configuration — its own registry file, declared per parameter

**Status:** Proposed · **Date:** 2026-08-26

### Context
The quarterly feature builder needs values that are neither code nor
data: the kelp stress temperatures, the coverage floor below which a
quarter is unusable, the fixed climatology baseline window, and which
feature set applies to which parameter. ADR-004 already established that
tunable science belongs in a reviewable registry rather than in code.
The open question was *which* registry.

### Options
**A — Extend `parameters.json`.** One fewer file, and the QC thresholds
already live there. But that file's stated charter is what a measurement
*means* — its canonical SI unit, and the bounds its QC tests need. A kelp
stress temperature is not a property of temperature; it is an ecological
decision about what this analysis does with temperature. Mixing them
would make "what is this parameter" and "what does the analysis do with
it" one question, which is the same conflation that already keeps the
parameter registry separate from the site registry.
**B — Its own registry file, `features.json` (chosen).** Policy that
applies to every series, plus a per-parameter entry naming an implemented
feature set and its thresholds. Loaded by a strict parser that refuses
unknown keys, empty blocks, and unimplemented feature sets.
**C — Constants in code with command-line overrides.** Puts tunable
science in the one place ADR-004 exists to keep it out of, and makes a
retune a code change rather than a reviewable data change.
**D — Infer the feature set from the parameter's unit.** No configuration
at all, and wrong: doc 03 already forbids this for parameters, because
`degC` is equally `sea_water_temperature` and `air_temperature` — and only
one of them gets kelp stress thresholds.

### Decision & consequences
B, with two rules that follow from it.

**A parameter declares its feature set; it is never inferred.** A new
sensor type is a registry entry rather than a code change, and D's mistake
cannot re-enter by the back door.

**Feature column names are derived from the configured thresholds**
(`days_above_20c` from `20.0`). Retuning a threshold renames its column
rather than silently changing what an existing column means — which is
what makes a registry edit safe to review as data.

Easier: retuning thresholds, the coverage floor or the baseline is a
reviewable data change, and a `data(registry)` commit that never mixes
with code. Harder: a third registry file to keep coherent, and a threshold
change renames columns, so a notebook written against the old name breaks
loudly rather than reading a redefined column quietly. That is the
intended trade.
