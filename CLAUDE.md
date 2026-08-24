# CLAUDE.md — kelpcompare

Compares Kelp Watch quarterly kelp canopy data against environmental time
series from project-owned sensors and public sources (NDBC, NOAA CO-OPS,
SCCOOS ERDDAP, CDIP, CDFW). Single-machine, single-operator scientific
pipeline. Results may feed publications — reproducibility and data
provenance are non-negotiable.

## Read before making changes

- Architecture and layer boundaries: @docs/01-system-architecture.md
- Source quirks (units, sentinels, formats): docs/02-data-source-catalog.md
- Schema and storage zones: docs/03-data-model.md
- Statistical methods and their assumptions: docs/04-analysis-methods.md
- Why the stack is what it is (ADRs): docs/05-architecture-decisions.md
- Sensor file ingest (HOBO) and adapter contract: docs/06-project-sensor-ingest-spec.md

Any change that touches the observation schema, a storage zone, or a
feature definition: propose in plan mode first and update the matching
doc in the same PR. Docs and code must not drift.

## Repo map

- `src/kelpcompare/fetchers/` — one module per source; the ONLY code that knows source formats
- `src/kelpcompare/adapters/` — vendor file parsers for project sensors (see docs/06)
- `src/kelpcompare/normalize/` — UTC + SI + controlled parameter names
- `src/kelpcompare/qc/` — QARTOD flags via ioos_qc; flags, never deletions
- `src/kelpcompare/features/` — quarterly aggregation and anomalies
- `src/kelpcompare/cli.py` — `kelpcompare ingest|qc|features|rebuild`
- `data/` — gitignored except `data/registry/`; zones per docs/03
- `notebooks/` — analyses of record; must run top-to-bottom from `comparison.parquet`
- `dashboard/` — Streamlit; reads Layer-2 Parquet only, computes no statistics of record
- `tests/fixtures/` — includes the two reference HOBO files (original + hand-edited)

## Commands

- Setup: `uv sync` (or `pip install -e ".[dev]"`)
- Tests: `pytest` — run before reporting any task complete
- Lint/format: `ruff check . && ruff format .`
- Pipeline: `kelpcompare ingest --source <name>`, `kelpcompare rebuild`

## Hard rules (also enforced by tests/hooks where possible)

1. Never write to `data/raw/` except through a fetcher/adapter landing; never
   modify or delete existing raw files. Raw is append-only, forever.
2. Timestamps are UTC and units are SI everywhere past the normalizer.
   Local time and °F exist only inside adapters and at presentation.
3. Missing kelp quarters (cloud gaps) are null, never zero. Do not fillna(0).
4. QC produces flags, not row deletions. Filtering happens at query time.
5. No ingest of project-sensor files without a matching serial + deployment
   window in `data/registry/sites.json` — quarantine instead.
6. The dashboard never computes statistics of record; notebooks own the science.
7. Every ingest/feature run writes a manifest. Don't bypass the CLI to write
   Parquet directly.
8. Don't add dependencies casually; this stack is deliberately small
   (DuckDB, pandas, xarray, geopandas, erddapy, ioos_qc, streamlit). New
   deps need a one-line rationale in the PR.

## Workflow conventions

- Plan mode for schema/architecture changes; direct edits fine for bugfixes
  and adapter tolerance improvements.
- Small PRs, one concern each. Reference the doc section a change implements.
- New external-source code needs a recorded fixture (sample payload in
  `tests/fixtures/`) so tests never hit the network.
- Network access in tests: forbidden. Fetchers are tested against fixtures.
- When a source format surprises you (new column, changed sentinel), update
  docs/02 in the same change.

## Commit format

Conventional Commits: `type(scope): imperative summary` — subject ≤72 chars,
imperative mood, no trailing period. Body explains why, not what, when the
diff alone isn't obvious.

- Types: `feat`, `fix`, `test`, `docs`, `refactor`, `chore`, and `data`
  (reserved for `data/registry/` changes — versioned scientific metadata).
- Scopes match the repo map: `adapters`, `fetchers`, `normalize`, `qc`,
  `features`, `cli`, `registry`, `dashboard`, `docs`, `skills`.
- When a commit implements or changes documented behavior, cite the doc
  section in the body (e.g. `per docs/06 §5`). If it changed a doc in the
  same commit (required when behavior and docs move together), say so.
- One concern per commit. Registry/data changes never mix with code changes.

Example:

    feat(adapters): implement hobo_xlsx adapter per docs/06 contract

    Parses Data/Events/Details sheets; tz and unit read from headers.
    Edited files tolerated and marked provenance=edited.

## Finishing a task

Work happens on a branch, never directly on `main`. Two confirmation gates —
never chain them into a single step.

1. **Confirm the commit.** A task is finishable only once `pytest` and
   `ruff check .` are green. Report that result, then propose the commit —
   branch, subject, body, and exactly which files are staged — and wait for
   a go-ahead. Session artifacts (e.g. `.claude/settings.json`) are never
   swept in.
2. **Confirm the merge.** After committing, propose the merge into `main`
   and wait. Re-run the tests on `main` afterwards and report the result.
3. **Clean up.** Once the merge is green, delete the merged branch — this
   step needs no separate confirmation. Pushing to `origin` is a separate
   ask, never implied by a merge.

## Project skills

Per-source and per-task depth lives in `.claude/skills/` (data-source-access,
vendor-adapters, quarterly-features, analysis-review). Prefer consulting the
relevant skill over guessing API syntax or feature math from memory.
