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
of the record. How to do that safely is the next section, and it is not
optional reading: the obvious command corrupts the file without saying so.

## Re-running one

    python scripts/run-notebook.py                       # every notebook here
    python scripts/run-notebook.py notebooks/01-lag-screen.ipynb

**Use the script rather than `jupyter execute` directly.** `nbclient` opens the
notebook with the platform's default encoding instead of UTF-8, so on Windows
every em dash and section sign in the markdown comes back as mojibake, the run
succeeds, and `--inplace` writes the damage back with **exit code 0**. The
script sets UTF-8 mode for the child process that actually does the reading,
normalises the line endings afterwards, and then checks the invariant that
catches the whole class of failure: executing a notebook rewrites outputs and
must never change a cell's *source*. If a source changed, it refuses and tells
you to restore the file.

**Or run it in the editor**, which has neither problem. In VS Code a `.ipynb`
opened as text is JSON and has no kernel picker — use *Open in Notebook
Editor*, select the `.venv` interpreter (nothing else has `kelpcompare`
installed), Run All, and save. The outputs only reach the file when you save.

Either way, check afterwards that the digest the notebook prints is the digest
of the `comparison.parquet` you meant to run against. A notebook whose gate
cell reports an older digest did not pick up the rebuilt table.

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

**Only predictors are eligible.** docs/04 §5 makes `air_temperature` and
`wind_speed` **controls**: screened and reported, never pre-registered. The
reasons are mechanistic and prior to any coefficient — air temperature
correlates with sea water temperature at r = 0.857 and so re-measures it more
noisily, and scalar wind speed averages upwelling-favorable alongshore stress
against downwelling-favorable Santa Ana wind, leaving it no sign to predict.
`features.json` records the decision; `01-lag-screen.ipynb` reads it from there
rather than restating it. 330 of 1,750 cells are withheld on those grounds
before anything below is ranked.

**That fraction has changed, and the change is worth naming.** The demotion once
withheld half the grid — 330 of 660. It now withholds under a fifth, because the
pool grew with the reference series while the two met parameters stayed on one
station. The multiple-comparison arithmetic docs/04 §5 calls a side effect has
therefore quietly reversed direction, which is
[#81](https://github.com/cweber12/kelp-compare/issues/81)'s question and not
this list's to answer.

Candidates from the screen at `sha256:4cde6f9d95207dc1`. The rule, which
`01-lag-screen.ipynb` §6 applies rather than leaving to be re-derived by hand:
over the predictor cells only, drop anything the input audit flagged
`low_resolution`, keep what rests on an effective sample of at least 30 with
Pearson and Spearman agreeing to within 0.05, and take the **three strongest
by |r|**.

That last step is a cut, not a criterion. 629 cells clear the conditions before
it, so what makes these three the list is that they are the top of a ranking —
which is exactly the kind of choice this section exists to make visible.

| Polygon | Series | Feature | Lag | r | n | n_eff | Why it is interesting |
|---|---|---|---|---|---|---|---|
| `KELP:ENCINITAS` | `NDBC:LJAC1` sea water temp, 3.4 m | `days_below_14c` | 4 | +0.424 | 71 | 50.6 | The cold-water/nitrate association docs/04 §2 predicts, at a one-year lag. The one cell carried over from the previous list |
| `KELP:SOLANA-BEACH` | `SIO:LAJOLLA-PIER` sea water temp, 5 m | `days_below_14c` | 4 | +0.406 | 161 | 117.3 | The same association in a neighbouring bed, on a different instrument and a 161-quarter record rather than a 71-quarter one |
| `KELP:LA-JOLLA` | `SST:LA-JOLLA` sea water temp | `degree_days_above_18c` | 1 | −0.353 | 95 | 61.3 | Accumulated heat one quarter earlier against less canopy — a different mechanism from the two above, and the first candidate measured *over the bed itself* |

What to weigh before registering any of them:

- **This list is nearer two hypotheses than one, which the previous one was
  not.** That list was three readings of the cold-water association at lag 4,
  all on `NDBC:LJAC1`. Two of these three still are, and they remain adjacent
  beds rather than independent evidence — them agreeing is mild reassurance
  that the signal is not one bed's noise. The third is a heat-accumulation cell
  at a short lag with the opposite sign, so it can fail without the first two
  failing.
- **Three reference series is not three independent references.**
  `NDBC:LJAC1` and `SIO:LAJOLLA-PIER` are both at Scripps Pier and measure the
  same water; they are two instruments on one signal, which is reassurance about
  instrumentation rather than replication
  ([#81](https://github.com/cweber12/kelp-compare/issues/81)). `SST:LA-JOLLA`
  is the one that is genuinely elsewhere — a satellite series over the La Jolla
  bed rather than a point measurement up the coast.
- **The controls still rank alongside the predictors, and that is the warning
  they exist for.** On the cells the candidate rule keeps, the strongest control
  coefficient is |r| = 0.38 against the strongest predictor's 0.42. The medians
  now separate slightly — 0.095 for sea water temperature against 0.068 and
  0.070 for the two controls — where they were indistinguishable on the smaller
  grid. That is a thin margin to rest anything on. A screen in which a variable
  withheld on mechanistic grounds performs about as well as the one under test
  may be recovering shared seasonality rather than mechanism; that does not
  revive the controls, it discounts the predictors.
- **The satellite series enters the pool crippled on exactly the features it
  was wanted for.** All six `SST:*` beds are flagged `low_resolution` on
  `days_above_23c` and four of six on `days_below_14c`, because a daily L4
  analysis inherits QC and threshold settings sized for a 10-minute logger
  ([#113](https://github.com/cweber12/kelp-compare/issues/113)). So the SST leg
  reaches this list through the continuous features rather than the ecological
  count features, and `SST:ENCINITAS` `days_below_14c` at lag 4 — r = +0.391,
  which would otherwise rank eighth in the whole screen — is dropped by the
  audit rather than by the rule.
- **docs/04 §4.5 still cannot be attempted.** No project-sensor cell can enter
  this list: both TidbiTs hold roughly six weeks, so neither quarter clears the
  coverage floor and both anomalies are null. Whether the project's own sensors
  beat the public station remains unasked.
- **`n_eff` is a ceiling, and it is loosest exactly here.** It corrects for
  lag-1 persistence only — measured across calendar-adjacent quarters, so a
  cloud gap or a missing Q2 breaks the pair instead of being counted as one —
  and the kelp anomaly is still autocorrelated at 0.20 to 0.41 four quarters
  out across the six beds. Under a higher-order Bartlett correction, truncated
  at *K* = ⌊*n*/4⌋ with the (1 − *k*/*n*) taper, the three rows above fall to
  **41.7, 99.5 and 54.2**.
- **Read those as a range, not as three numbers.** `01-lag-screen.ipynb` §6
  computes them rather than restating them, and prints them across other
  truncations too, because they move with *K*: the LJAC1 row spans 49.5 at
  *K* = 4 down to 41.9 at *K* = 16, and the satellite row is non-monotonic
  across the ladder (54.9, 50.5, 52.5, 52.5), which is a noise-dominated tail
  rather than a precise effective sample size. The correction is a note on these
  cells and deliberately not a column across the grid — across the screen the
  expression is unbounded above `n` and its denominator can cross zero
  ([#35](https://github.com/cweber12/kelp-compare/issues/35)). On this table all
  three candidates came back below their own `n`, which §6 checks rather than
  assumes.
- **`n_eff == n` is ambiguous and the screen says which it is.** It can mean the
  correction ran and found nothing to take, or that it never ran, and nothing in
  the number separates them. The `discounted` column does, for 287 of the 1,750
  screened cells.
- **626 cells clear every condition above and were cut by the top-three rule**,
  which is the size of the choice being made here. The two strongest of them,
  named so the cut is visible rather than silent: `KELP:SOLANA-BEACH` against
  `NDBC:LJAC1` `days_below_14c` at lag 4 (r +0.347) and `KELP:SOLANA-BEACH`
  against the pier at 0.5 m, quarterly `mean` at lag 1 (r −0.312). The first is
  a fourth reading of the association this list already carries twice; the
  second is not, and is the strongest cut cell that would have added a distinct
  claim. That is a judgement, and it is the operator's to overturn.

Two exclusions predate the control demotion and stay on the record, because
both are now over-determined and would otherwise look as though the role had
done all the work:

- Anything on `wind_speed` `min`, whose quarterly minimum takes two values at
  LJAC1 (the anemometer's resolution floor). It produced the largest
  coefficients in the grid, |r| up to 0.74, and they are artefacts. The screen
  drops it in §2, one step before any role is consulted, and says why.
- `air_temperature` `p95` at Imperial Beach, lag 2: r = +0.38 but ρ = +0.04.
  A rank gap that size means a handful of points, which at n ≈ 50 is the
  common failure rather than an unusual one. It is still the strongest air
  temperature cell in the screen, and now sits among the controls.

## Known gaps that shape what can be asked

- **The pool is still one predictor family in all but name.** 620 of the 629
  cells clearing the candidate conditions are `sea_water_temperature`; the
  remaining nine are the wave family at `NDBC:46254`, whose strongest cell is
  |r| = 0.26. docs/04 §6 records that this data cannot separate thermal stress
  from nutrient limitation, and nothing in this screen can, because every
  nutrient-side feature in it is a function of temperature
  (https://github.com/cweber12/kelp-compare/issues/108).
- **Four reference series, two of which are the same water.** `NDBC:LJAC1` and
  both `SIO:LAJOLLA-PIER` depths are at Scripps Pier; the six `SST:*` beds are
  one satellite product; `NDBC:46254` reaches one bed. `NDBC:46266` is the
  nearest station to three beds and contributes nothing at all — its record
  begins 2019-12 and clears no baseline under docs/04 §3, so every one of its
  cells is null.
- **Nothing here can label an event.** docs/04 §4.2 wants El Niño quarters and
  marine-heatwave composites; ONI is not implemented
  (https://github.com/cweber12/kelp-compare/issues/109) and no Hobday
  computation exists, though the MUR and Scripps Pier series are now long enough
  to support one.
- **docs/04 §4.5, the project's key question, still cannot run** — but the
  blocker has moved. `observations/` now holds project-sensor rows for both
  TidbiTs, and the registry caught up in August 2026: positions surveyed into
  `sites.json`, all six bed outlines recorded in `polygons.geojson` and verified
  cell-for-cell against the landed Kelp Watch export (docs/02). What is missing
  is *record length*: roughly six weeks each, so neither quarter clears the
  coverage floor and both anomalies are null. Only time or a longer deployment
  closes this.
- **The input audit's "not applicable" verdict now covers two different things**
  — a feature its parameter's feature set does not define, and a series that
  produced no anomaly at all — and prints a reason true only of the first
  (https://github.com/cweber12/kelp-compare/issues/117). No coefficient is
  affected; read that count with the caveat until it is split.
- **The 2007–2019 baseline contains the 2014–2016 marine heatwave** on both
  sides of every comparison, damping the anomalies the analysis most wants to
  detect (docs/04 §3).
