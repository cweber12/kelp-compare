# Notebooks — analyses of record

The science lives here. The dashboard computes no statistics of record (hard
rule 6) and the pipeline computes no results — it produces
`features/comparison.parquet` and stops. Everything downstream of that is a
notebook, versioned in git beside the package that built its inputs.

## Conventions

**Run top to bottom from `features/comparison.parquet` and nothing else.** No
side reads of `observations/` or `raw/`. A notebook that reaches around the
comparison table is a notebook whose result cannot be reproduced from a stated
input, and it stops being an analysis of record.

**Quote the digest.** Each notebook prints the SHA-256 of the comparison file it
ran against. Put that digest in any figure caption or write-up, so a number in a
document can be traced to the exact table that produced it — and so two
write-ups can be told apart when the table is rebuilt.

*Why a digest rather than the run manifest ID docs/04 §5 asks for:* no feature
table carries the id of the run that built it. `fetch_run_id` is on every
observation row, but putting a build id on a derived table would change its bytes
on every rebuild and cost the zone its reproducibility. A digest is strictly
better anyway — it is verifiable rather than a pointer — but the manifest not
recording what it wrote is a gap worth closing.

**Nothing stochastic without a seed.** Nothing in these notebooks is stochastic
yet; when something is, seed it in the first cell.

**These do not run in CI.** They depend on `data/`, which is gitignored — the
data zones are local by design (docs/01 §2). Re-run them by hand after
`kelpcompare features`, and commit the executed notebook so the outputs are part
of the record.

## Index

| Notebook | docs/04 | What it does |
|---|---|---|
| `01-lag-screen.ipynb` | §4.1 | Kelp anomaly at *t* against every environmental feature anomaly at *t−0…4*, per polygon × series. Ranks candidates; claims nothing |

## Pre-registration

docs/04 §5 requires the handful of relationships carried from the §4.1 screen
into the §4.3 inferential models to be written down **before** they are fitted.
The screen generates several hundred coefficients; without this list, choosing
what to model after seeing the screen is choosing what to model because it
looked good, and the resulting p-values would mean nothing.

**Nothing is pre-registered yet.** The list below is what `01-lag-screen.ipynb`
surfaced, for the operator to choose from — being on it is not registration.

Candidates from the screen at `sha256:7d2c62503276e7be`. The rule, written out
so the table can be checked against the notebook rather than taken on trust:
drop anything the input audit flagged `low_resolution`, keep what rests on an
effective sample of at least 30 with Pearson and Spearman agreeing to within
0.05, and take the **three strongest by |r|**.

That last step is a cut, not a criterion. 262 cells clear the conditions before
it, so what makes these three the list is that they are the top of a ranking —
which is exactly the kind of choice this section exists to make visible.

| Polygon | Series | Feature | Lag | r | n_eff | Why it is interesting |
|---|---|---|---|---|---|---|
| `KELP:ENCINITAS` | LJAC1 sea water temp | `days_below_14c` | 4 | +0.42 | 50.5 | The cold-water/nitrate association docs/04 §2 predicts, at a one-year lag |
| `KELP:SOLANA-BEACH` | LJAC1 sea water temp | `days_below_14c` | 4 | +0.35 | 49.6 | The same relationship in a neighbouring bed, at the same lag |
| `KELP:IMPERIAL-BEACH` | LJAC1 wind speed | `variance` | 2 | −0.38 | 50.0 | Not predicted by docs/04; treat with more suspicion, not less |

Two things to weigh before registering any of them:

- **The two `days_below_14c` cells are the same station against two adjacent
  beds**, so they are not independent evidence. They agreeing is mild
  reassurance that the signal is not one bed's noise; it is not two findings.
- **Every candidate here is against `NDBC:LJAC1`**, the public station. The
  project sensor has three weeks of record, so docs/04 §4.5 — whether the
  project's own sensors beat the public station — cannot be attempted yet.
- **`n_eff` is a ceiling, and it is loosest exactly here.** It corrects for
  lag-1 persistence only, and the kelp anomaly is still autocorrelated at ≈0.32
  four quarters out. Under a higher-order (Bartlett) correction the two
  `days_below_14c` cells fall to 40.7 and 35.5. Register them on the
  understanding that their evidence is nearer 36–41 quarters than 50.
- **Three cells clear every condition above and were cut by the top-three
  rule**, named here so the cut is visible rather than silent: `KELP:ENCINITAS`
  sea water temp `min` at lag 4 (r −0.31), `KELP:IMPERIAL-BEACH` wind speed
  `mean` at lag 2 (r −0.34), and — dropped one step earlier, for
  `low_resolution` — `KELP:SOLANA-BEACH` wind speed `p05` at lag 3 (r +0.35).
  The first is the cold-water association the two listed `days_below_14c` cells
  already carry and the second is the same station, lag and quarter-set as the
  wind speed row above it, so on this reading neither adds an independent
  candidate. That is a judgement, and it is the operator's to overturn.

Excluded from consideration on inspection rather than on their coefficients:

- Anything on `wind_speed` `min`, whose quarterly minimum takes two values at
  LJAC1 (the anemometer's resolution floor). It produced the largest
  coefficients in the grid, |r| up to 0.74, and they are artefacts. The screen
  now excludes it and says why.
- `air_temperature` `p95` at Imperial Beach, lag 2: r = +0.38 but ρ = +0.04.
  A rank gap that size means a handful of points, which at n ≈ 50 is the
  common failure rather than an unusual one.

## Known gaps that shape what can be asked

- **`air_temperature` and `wind_speed` have no Q2 anomaly at any polygon** —
  their Q2 baseline holds 9 years against the 10-year minimum
  ([#30](https://github.com/cweber12/kelp-compare/issues/30)).
- **Every environmental anomaly is against `NDBC:LJAC1` alone.** No other
  public-source fetcher exists (docs/02), so there is no second reference.
- **docs/04 §4.5, the project's key question, cannot run.** It needs polygon
  geometry, which `polygons.geojson` records as `null` for all six beds, and the
  yellow buoy's position, which `sites.json` records as unverified.
- **The 2007–2019 baseline contains the 2014–2016 marine heatwave** on both
  sides of every comparison, damping the anomalies the analysis most wants to
  detect (docs/04 §3).
