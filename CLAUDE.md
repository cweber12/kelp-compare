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
- `src/kelpcompare/registry.py` — `sites.json`: which instrument was where, when, in what timezone
- `src/kelpcompare/parameters.py` — `parameters.json`: controlled names, SI units, QC ranges
- `src/kelpcompare/storage.py` — the docs/03 zones and the only writer into `observations/`
- `src/kelpcompare/manifest.py` — one run manifest per pipeline run (docs/03)
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
   window + series map in `data/registry/sites.json` — quarantine instead.
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
- Claude Code permissions split by scope: portable, team-useful allows go in
  the committed `.claude/settings.json`; anything machine- or session-specific
  goes in `.claude/settings.local.json`, which is gitignored. At a permission
  prompt, choose the local file unless the rule would also help a teammate on
  a fresh clone.

## Commit format

Conventional Commits: `type(scope): imperative summary` — subject ≤72 chars,
imperative mood, no trailing period. Body explains why, not what, when the
diff alone isn't obvious.

- Types: `feat`, `fix`, `test`, `docs`, `refactor`, `chore`, and `data`
  (reserved for `data/registry/` changes — versioned scientific metadata).
  There is no `merge` type: branches land through GitHub's merge button, which
  writes its own subject. Commits up to `a25b38a` carry `merge:` from the
  earlier local-merge workflow; leave them as they are.
- Scopes match the repo map: `adapters`, `fetchers`, `normalize`, `qc`,
  `features`, `cli`, `registry`, `dashboard`, `docs`, `skills`, `agents`, and
  `ci`. `agents` covers this file, `docs/agents/`, and `.claude/` configuration
  — the instructions and permissions agents work under. `ci` covers
  `.github/`. `docs` stays reserved for the numbered specs in `docs/`.
- When a commit implements or changes documented behavior, cite the doc
  section in the body (e.g. `per docs/06 §5`). If it changed a doc in the
  same commit (required when behavior and docs move together), say so.
- One concern per commit. Registry/data changes never mix with code changes.

Example:

    feat(adapters): implement hobo_xlsx adapter per docs/06 contract

    Parses Data/Events/Details sheets; tz and unit read from headers.
    Edited files tolerated and marked provenance=edited.

## Branching and finishing a task

Every change reaches `main` through a branch and a reviewed pull request —
features, fixes, docs, and registry edits alike. Never commit on `main` itself,
and never merge a branch locally: `main` moves only when the operator presses
the button on GitHub.

**Branch.** Branch from an up-to-date `main`, named `<type>/<slug>` reusing the
commit types above: `feat/hobo-csv-adapter`, `fix/quarter-boundary-utc`,
`docs/data-model-zones`, `data/site-registry-buoy-coords`.

**Slice.** Break an implementation into vertical slices, each its own commit on
that branch. A slice is the smallest change that leaves the repo green —
`pytest` and `ruff check .` pass at *every* commit, not only the last. Each
slice keeps the one-concern rule and cites the doc section it implements.
Slices are never squashed away at merge; they are the reviewable record of how
the change was built.

**Land it — automatically.** Committing, pushing and opening the PR are not
gated. Do not wait to be asked, do not propose them a step at a time, and do
not summarise a commit for approval before making it. The operator has exactly
one gate: the merge button on GitHub.

A slice is committable only once `pytest` and `ruff check .` are green. A red
run is never committed — that is what makes "every commit was green" a true
statement about the history rather than an aspiration. Stage the files the
slice is about, by name; untracked session artifacts are never swept in.

Push once, after the last slice, so the PR arrives finished and CI runs on a
complete branch rather than flickering red and green while work is still going
on:

    git push -u origin <branch>
    gh pr create --base main --head <branch> --title '<type>(<scope>): ...' --body-file <file>

**Stop and ask anyway** if the work turns out to touch the observation schema,
a storage zone, a feature definition, or to need a new dependency — the changes
this file already says need plan mode. Discovering that mid-task is the case
the removed gates used to catch by accident, and it is the one thing still
worth an interruption. Nothing else is.

**Write the PR body for an auditor**, not for someone who already sat through
the work. It is the durable record of why the change looks the way it does —
the commits say what moved, the PR says why that was the right thing to move.
Cover, in whatever order suits the change:

- what it delivers and what problem that solves, naming the doc section or ADR
  it implements;
- the decisions taken **and the alternatives rejected**, with the reason each
  was rejected — a reviewer cannot audit a choice they cannot see was a choice;
- evidence, with real numbers from a real run, not a claim that it works;
- any deliberate divergence from an upstream library's or a standard's
  behavior, called out explicitly rather than left to be discovered;
- the risks and costs accepted, especially anything provisional or tuned on
  thin data;
- a slice-by-slice review guide, and which hard rules the change touches.

**The repo is public, and pushing is now automatic.** The rule in
`docs/agents/issue-tracker.md` about issue bodies binds PR bodies, commit
messages, and every file on a pushed branch just as hard: no unpublished
results, no site coordinates that are not already in `data/registry/sites.json`,
nothing embargoed. That rule used to be backed by the operator eyeballing
everything before it left the machine. Nothing eyeballs it now, so it has to be
obeyed at the point of writing.

**One closing keyword per PR.** GitHub fires `closes`/`fixes` from commit
messages on the default branch as well as from PR bodies, and surrounding prose
does not disarm them — "this closes #148 open question 2" closes #148. Every
issue other than the single one a PR is closing goes in by full URL, which
cross-references without being parsed. This matters more now that issues are
filed automatically alongside the PRs that reference them; a PRD closed by
accident takes its unshipped children with it.

**Landing.** Merging is the operator's, on GitHub, with **Create a merge
commit** — never *Squash and merge*, which would collapse the slices into one
and destroy the per-commit record that the repo was green at every step.
Once the operator reports the merge, pull `main` back down and delete the
merged branch without asking — that is cleanup, not a decision. Pushing
anything else to `origin` remains a separate ask.

## Reporting work

The operator works in short, frequently interrupted sessions. A report he has
to read from the top to find the decision has already failed, however accurate
it is. Lead with the decision; everything under it is optional reading.

**While working.** One plain sentence per slice commit, so a session can be
rejoined at a glance. Nothing else — the tool output is already on screen.

**When a branch is ready to merge**, and only then, write the full report.
A blank line between every section; adjacent bold headings scan no better
than prose:

    **Ready to merge — [PR #N](url) ✅ green.** One sentence: what it does,
    and what it does not touch.

    **What I did** — the change in plain terms.

    **Problems found** — each one filed, with its number. Or "none".

    **Next step** — what to do once it is merged.

- **It fits one screen without scrolling.** Top line at most two sentences;
  each section at most three sentences or three bullets. This is the one rule
  with a failure condition the operator can check in half a second, which is
  why it is a cap and not an encouragement to be brief.
- **Overflow goes to the PR body**, which is written for an auditor and has no
  cap. The cap routes detail; it does not delete it.
- **Plain language, not no language.** Domain vocabulary stays — QARTOD,
  anomaly, quarter, climatology cost the operator nothing and replacing them
  makes sentences longer and less precise. Implementation vocabulary is
  translated into what changed and whether it is safe: not "each partition
  writes through a staging file with an atomic rename", but "a crashed run can
  no longer leave a half-written file behind".
- **No code blocks** unless asked; they cost a screen and say nothing about
  whether to merge. Tables are fine and scan faster than prose.
- **This governs the chat report only.** PR bodies, commit messages and docs
  stay technical and thorough — same facts, different reader.

**When a problem is found, file it.** Create the issue or PRD, apply a triage
label per `docs/agents/triage-labels.md`, and report the number. Never ask
"want me to file it, or leave it?" — naming a problem and handing back the
decision converts a finding into homework. For each one, state what is wrong,
the concrete fix as real work, roughly how big it is, and whether to do it now
or in a new session — with a recommendation, not an open either/or.

Fix it in the current branch instead only when it is in scope, small, and does
not break the one-concern rule. If it is cosmetic, or you are not confident it
is real, say so in prose and file nothing. A PRD rather than an issue when the
work needs design decisions before it can start.

**Two standing signals** mean this section should be edited, not just that one
report redone. **"too long"** — the one-screen rule broke. **"why did you
stop"** — a stop fired on something that was not really a schema, storage,
feature or dependency change.

## Project skills

Per-source and per-task depth lives in `.claude/skills/` (data-source-access,
vendor-adapters, quarterly-features, analysis-review). Prefer consulting the
relevant skill over guessing API syntax or feature math from memory.

## Agent skills

### Issue tracker

Issues live as GitHub issues in `cweber12/kelp-compare`, managed with the `gh`
CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles use their default label strings
(`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`,
`wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo. ADRs live in `docs/05-architecture-decisions.md`, not in a
`docs/adr/` directory. See `docs/agents/domain.md`.
