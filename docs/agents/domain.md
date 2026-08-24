# Domain Docs

How the engineering skills should consume this repo's domain documentation when
exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/05-architecture-decisions.md`** — the ADRs. This repo keeps all
  architectural decisions in that single numbered file as sections (ADR-001,
  ADR-002, ...). There is **no `docs/adr/` directory**; don't go looking for one
  and don't create one. New ADRs are appended to `docs/05`.

If `CONTEXT.md` doesn't exist, **proceed silently**. Don't flag its absence;
don't suggest creating it upfront. The producer skill (`/grill-with-docs`)
creates it lazily when terms actually get resolved. As of setup it does not
exist.

## File structure

Single-context repo:

```
/
├── CONTEXT.md                          ← not yet created
├── docs/
│   ├── 00-README.md                    ← reading order for the doc set
│   └── 05-architecture-decisions.md    ← all ADRs live here
└── src/kelpcompare/
```

This repo is **not** multi-context — there is no `CONTEXT-MAP.md` and no
per-package `CONTEXT.md`.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal,
a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift
to synonyms the glossary explicitly avoids.

Until `CONTEXT.md` exists, the numbered doc set is the de facto glossary — in
particular `docs/03-data-model.md` for the observation schema and controlled
parameter names, and `docs/04-analysis-methods.md` for feature and anomaly
terminology. Use those spellings exactly (e.g. `sea_water_temperature`, not
"water temp").

If the concept you need isn't documented anywhere yet, that's a signal — either
you're inventing language the project doesn't use (reconsider) or there's a real
gap (note it for `/grill-with-docs`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than
silently overriding:

> _Contradicts ADR-001 (DuckDB + Parquet, no database server) — but worth
> reopening because…_

Note that every ADR in `docs/05` is currently marked **Proposed**, not Accepted.
Treat them as load-bearing anyway; challenge in the open rather than assuming
they're provisional.
