# kelpcompare

Compares Kelp Watch quarterly kelp canopy data against environmental time
series from project-owned sensors and public sources (NDBC, NOAA CO-OPS,
SCCOOS ERDDAP, CDIP, CDFW), on a single local machine.

**Status: planning complete, implementation not started.** The design lives
in `docs/` (start at `docs/00-README.md`); repo conventions for humans and
for Claude Code live in `CLAUDE.md`; per-topic depth for Claude Code lives
in `.claude/skills/`.

## Quick start

```bash
uv sync --all-extras        # or: pip install -e ".[dev,dashboard]"
pytest                      # fixture sanity tests pass with zero app code
```

`tests/fixtures/` contains a real HOBO TidbiT export and its hand-edited
copy; `tests/test_fixtures_hobo.py` pins the format findings from
`docs/06-project-sensor-ingest-spec.md` and defines what the first adapter
must reproduce.

## Suggested first implementation steps

1. `hobo_xlsx` adapter + registry gate (docs/06) — the fixtures are its tests
2. Normalizer + observation schema (docs/03)
3. One public fetcher end-to-end (CO-OPS is the simplest API)
4. Quarterly feature builder (docs/04 §2–3)
