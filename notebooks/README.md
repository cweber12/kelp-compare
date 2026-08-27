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

*Importing from `kelpcompare` is not a side read.* The rule is about **data** —
a second source of numbers is what makes a result untraceable. Schema facts are
the opposite case: which columns identify one series, and which anomalies belong
to the kelp half, are defined once in the package that wrote the table (docs/03),
and a notebook that restates them instead of importing them is one that will
keep screening the old schema after the table has moved on. Import the constant;
never open a second file. A notebook doing this should check on load that the
table still matches what it imported, so a package and a table that have drifted
apart say so rather than quietly analysing the overlap.

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

Candidates from the screen at `sha256:7d2c62503276e7be`. The rule, which
`01-lag-screen.ipynb` §6 now applies rather than leaving to be re-derived by
hand: drop anything the input audit flagged `low_resolution`, keep what rests
on an effective sample of at least 30 with Pearson and Spearman agreeing to
within 0.05, and take the **three strongest by |r|**.

That last step is a cut, not a criterion. 233 cells clear the conditions before
it, so what makes these three the list is that they are the top of a ranking —
which is exactly the kind of choice this section exists to make visible.

| Polygon | Series | Feature | Lag | r | n_eff | Why it is interesting |
|---|---|---|---|---|---|---|
| `KELP:ENCINITAS` | LJAC1 sea water temp | `days_below_14c` | 4 | +0.42 | 50.6 | The cold-water/nitrate association docs/04 §2 predicts, at a one-year lag |
| `KELP:SOLANA-BEACH` | LJAC1 sea water temp | `days_below_14c` | 4 | +0.35 | 49.8 | The same relationship in a neighbouring bed, at the same lag |
| `KELP:IMPERIAL-BEACH` | LJAC1 wind speed | `variance` | 2 | −0.38 | 50.0 † | Not predicted by docs/04; treat with more suspicion, not less |

What to weigh before registering any of them:

- **The two `days_below_14c` cells are the same station against two adjacent
  beds**, so they are not independent evidence. They agreeing is mild
  reassurance that the signal is not one bed's noise; it is not two findings.
- **Every candidate here is against `NDBC:LJAC1`**, the public station. The
  project sensor has three weeks of record, so docs/04 §4.5 — whether the
  project's own sensors beat the public station — cannot be attempted yet.
- **`n_eff` is a ceiling, and it is loosest exactly here.** It corrects for
  lag-1 persistence only — measured across calendar-adjacent quarters, so a
  cloud gap or a missing Q2 breaks the pair instead of being counted as one —
  and the kelp anomaly is still autocorrelated at 0.26 to 0.35 four quarters
  out across the six beds. Under a higher-order Bartlett correction, truncated
  at *K* = ⌊*n*/4⌋ with the (1 − *k*/*n*) taper, the three rows above fall to
  **41.7, 41.1 and 36.7**. Register them on the understanding that their
  evidence is nearer 37–42 quarters than 50.
- **Read that as a range, not as three numbers.** `01-lag-screen.ipynb` §6
  computes the figures above rather than restating them, and prints them across
  other truncations too, because they move with *K*: Solana Beach spans 48.7 at
  *K* = 4 down to 36.7 at *K* = 17, and Imperial Beach is not even monotonic in
  *K*. That is a noise-dominated tail, not a precise effective sample size. The
  correction is a note on these cells and deliberately not a column across the
  grid — over all 660 cells it exceeds `n` in 152 of them and is undefined in 6
  ([#35](https://github.com/cweber12/kelp-compare/issues/35)).
- **† The wind speed candidate was never discounted at all.** Its lag-1 product
  came out at or below zero, so `effective_n` handed back the raw quarter
  count: that 50.0 is 50, not a corrected 50, and it sits in the table beside a
  genuinely discounted 50.6 with only the marker separating them. It is the row
  where "ceiling, not an estimate" is doing the most work. The screen's
  `discounted` column carries this, for these three and for the 130 of 660
  cells in the same position.
- **230 cells clear every condition above and were cut by the top-three rule**,
  which is the size of the choice being made here. The two strongest of them,
  named so the cut is visible rather than silent: `KELP:ENCINITAS` sea water
  temp `min` at lag 4 (r −0.31) and `KELP:IMPERIAL-BEACH` wind speed `mean` at
  lag 2 (r −0.34); and — dropped one step earlier, for `low_resolution` rather
  than by the cut — `KELP:SOLANA-BEACH` wind speed `p05` at lag 3 (r +0.35).
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
