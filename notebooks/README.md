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

docs/04 §5 caps at three the *signals* carried from the §4.1 screen into the
§4.3 inferential models, and requires them written down **before** they are
fitted.
The screen generates several hundred coefficients; without this list, choosing
what to model after seeing the screen is choosing what to model because it
looked good, and the resulting p-values would mean nothing.

**Registered on 2026-09-01, against the screen at
`sha256:4cde6f9d95207dc1`.** These three are the relationships docs/04 §4.3 may
fit. Anything else fitted is exploratory and says so in the same breath as the
number, and all three are reported whatever they come out as — a registered
relationship that fails is a result, and dropping it is the thing registration
exists to prevent.

**Only predictors are eligible.** docs/04 §5 makes `air_temperature` and
`wind_speed` **controls**: screened and reported, never pre-registered. The
reasons are mechanistic and prior to any coefficient — air temperature
correlates with sea water temperature at r = 0.857 and so re-measures it more
noisily, and scalar wind speed averages upwelling-favorable alongshore stress
against downwelling-favorable Santa Ana wind, leaving it no sign to predict.
`features.json` records the decision; `01-lag-screen.ipynb` reads it from there
rather than restating it. 330 of 1,750 cells are withheld on those grounds
before anything below is ranked.

**That fraction has changed, and the change is why the cap counts signals.**
The demotion once withheld half the grid — 330 of 660. It now withholds under a
fifth, because the pool grew with the reference series while the two met
parameters stayed on one station, so the side effect docs/04 §5 called an
improvement has reversed direction. A cap stated in cells would have meant
something different after every registry edit; docs/04 §5 therefore states it in
signals, which is what [#81](https://github.com/cweber12/kelp-compare/issues/81)
settled.

**The rule, which `01-lag-screen.ipynb` §6 applies rather than leaving to be
re-derived by hand.** Over the predictor cells only, drop anything the input
audit flagged `low_resolution`, keep what rests on an effective sample of at
least 30 with Pearson and Spearman agreeing to within 0.05, collapse what
survives into **signals** — one per (feature, lag, polygon), the strongest cell
standing for each — and take the **three strongest by |r|**.

That last step is a cut, not a criterion. 629 eligible cells collapse to 253
signals, and 250 of those are cut, so what makes these three the list is that
they are the top of a ranking — which is exactly the kind of choice this section
exists to make visible. The ranking is on |r|, and so on effect size, which
favours the shorter record at equal evidence: docs/04 §5 now says so, and
`01-lag-screen.ipynb` §7 prints what it costs.

**What the collapse is for.** Two cells agreeing on feature, lag and polygon are
one claim about one bed measured by two instruments. `NDBC:LJAC1` and
`SIO:LAJOLLA-PIER` both sit at Scripps Pier and measure the same water — on the
`days_below_14c` anomaly the two pier depths correlate at r = 0.970 and the two
stations at 0.785–0.835 — so registering them separately would count
instrumentation as replication. Beds are not collapsed: two beds carrying one
feature at one lag stay two signals, because merging them would erase the
between-bed comparison docs/04 §4.5 is built to make.

| Polygon | Series standing for it | Feature | Lag | r | n | n_eff | Cells | Why it is interesting |
|---|---|---|---|---|---|---|---|---|
| `KELP:ENCINITAS` | `NDBC:LJAC1` sea water temp, 3.4 m | `days_below_14c` | 4 | +0.424 | 71 | 50.6 | 1 | The cold-water/nitrate association docs/04 §2 predicts, at a one-year lag. The one cell carried over from the previous list |
| `KELP:SOLANA-BEACH` | `SIO:LAJOLLA-PIER` sea water temp, 5 m | `days_below_14c` | 4 | +0.406 | 161 | 117.3 | 2 | The same association in a neighbouring bed, on a 161-quarter record rather than a 71-quarter one. Absorbs the `NDBC:LJAC1` reading of the same cell at +0.347 |
| `KELP:LA-JOLLA` | `SST:LA-JOLLA` sea water temp | `degree_days_above_18c` | 1 | −0.353 | 95 | 61.3 | 3 | Accumulated heat one quarter earlier against less canopy — a different mechanism from the two above, and the only one measured *over the bed itself*. Absorbs both pier depths, at −0.242 and −0.220 |

The `Cells` column is how many eligible cells the signal merged. Where it is
more than one, the merged coefficients are printed by `01-lag-screen.ipynb` §6
rather than summarised here, so the claim that they are one signal can be
checked against how closely they actually agree — and the La Jolla signal is the
one to check, since −0.353 against −0.242 and −0.220 is a wider spread than the
word "agree" comfortably covers.

What to weigh before registering any of them:

- **Two hypotheses, not three.** Two of the three are the cold-water
  association at lag 4 on adjacent beds; the third is a heat-accumulation cell
  at a short lag with the opposite sign, so it can fail without the first two
  failing. The signal rule stops one bed being registered twice over; it does
  not make two *adjacent* beds independent evidence, and it is not meant to.
  Encinitas and Solana Beach agreeing is mild reassurance that the association
  is not one bed's noise — nothing stronger.
- **The list is ranked on |r|, and both standardised alternatives change it.**
  |r| is an effect size and carries no sample size, while the signals it ranks
  span `n_eff` 30.2 to 161.0. Ranking instead on Fisher *z*, or on the 95% lower
  confidence bound of |ρ|, drops `KELP:LA-JOLLA` `degree_days_above_18c` at lag 1
  and admits `KELP:LA-JOLLA` `days_below_14c` at lag 4 — the same substitution
  either way, so it is not an artefact of one scale.

  | Signal | r | n_eff | \|z\| | LCB | Rank by \|r\| |
  |---|---|---|---|---|---|
  | `KELP:SOLANA-BEACH` pier 5 m `days_below_14c` lag 4 | +0.406 | 117.3 | 4.61 | 0.243 | 2 |
  | `KELP:ENCINITAS` `NDBC:LJAC1` `days_below_14c` lag 4 | +0.424 | 50.6 | 3.12 | 0.167 | 1 |
  | `KELP:LA-JOLLA` pier 5 m `days_below_14c` lag 4 | +0.287 | 123.1 | 3.24 | 0.116 | 10 |
  | `KELP:LA-JOLLA` `SST:LA-JOLLA` `degree_days_above_18c` lag 1 | −0.353 | 61.3 | 2.82 | 0.112 | 3 |

  Read that beside the bullet above rather than as a correction to it. The
  substitution would make the list one association read three times instead of
  two hypotheses, so the tilted rule is returning the *better* list — which is
  the point, because nobody had written down why. Across the whole pool the tilt
  is mild, Spearman of |r| against `n_eff` being −0.097; it is decisive only
  here, at the cut. docs/04 §5 keeps |r| and states the reason; §7 prints all
  three rankings so the choice stays visible instead of being settled once.
- **The reference behind each signal is not the evidence for it.** Which series
  stands for a signal is a ranking artefact: it is the strongest cell in the
  group, not the best instrument. Solana Beach would read almost the same
  through `NDBC:LJAC1` at +0.347, and La Jolla noticeably weaker through either
  pier depth. Report the signal, and the spread inside it, rather than the
  series name as though it had been chosen.
- **The controls still rank alongside the predictors, and that is the warning
  they exist for.** On the cells the candidate rule keeps, the strongest control
  coefficient is |r| = 0.38 against the strongest predictor's 0.42. The medians
  now separate slightly — 0.095 for sea water temperature against 0.068 and
  0.070 for the two controls — where they were indistinguishable on the smaller
  grid. That is a thin margin to rest anything on. A screen in which a variable
  withheld on mechanistic grounds performs about as well as the one under test
  may be recovering shared seasonality rather than mechanism; that does not
  revive the controls, it discounts the predictors.

  That comparison is between pools of unequal record length, and the direction
  is not the obvious one. Both met parameters sit on the one station, so every
  eligible control signal stops at `n_eff` ≤ 50.0 while predictors reach 161.0 —
  the 0.38-against-0.42 line is a short-record maximum set against a
  mixed-record one. Restated over the same 65 control and 253 predictor signals
  on the scales `01-lag-screen.ipynb` §7 prints:

  | Scale | Control max | Predictor max | Controls reaching the weakest registered signal |
  |---|---|---|---|
  | \|r\| | 0.380 | 0.424 | 1 of 65 |
  | \|z\| | 2.74 | 4.61 | 0 of 65 |
  | LCB | 0.114 | 0.243 | 1 of 65 |

  The caution survives standardisation rather than dissolving in it: it clears
  on Fisher *z* and returns on the confidence bound, where a control still edges
  the weakest registered signal, 0.114 against 0.112. Read it across the three.
  The |r| line alone is not the pessimistic reading of this table — it is one of
  three, and the scale that is neither a raw effect size nor a significance
  ranking agrees with it.
- **The satellite series enters the pool crippled on exactly the features it
  was wanted for.** All six `SST:*` beds are flagged `low_resolution` on
  `days_above_23c` and four of six on `days_below_14c`, because a daily L4
  analysis inherits QC and threshold settings sized for a 10-minute logger
  ([#113](https://github.com/cweber12/kelp-compare/issues/113)). So the SST leg
  reaches this list through the continuous features rather than the ecological
  count features, and `SST:ENCINITAS` `days_below_14c` at lag 4 — r = +0.391,
  which would otherwise rank eighth in the whole screen — is dropped by the
  audit rather than by the rule.
- **docs/04 §4.5 still cannot be attempted, and not only for want of data.**
  No project-sensor cell can enter this list: `PROJ:TIDBIT-1` holds 22 observed
  days and `PROJ:TIDBIT-2` holds 44, both in 2026 Q3, so neither clears the
  coverage floor and both anomalies are null. Coverage is the near cause; the
  far one is that a single quarter cannot clear a climatology needing ten years
  inside 2007–2019, so §4.5 is unreachable in anomaly space rather than merely
  early ([#120](https://github.com/cweber12/kelp-compare/issues/120)). Whether
  the project's own sensors beat the public station remains unasked.
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
- **250 signals clear every condition above and were cut by the top-three
  rule**, which is the size of the choice being made here. The strongest of
  them, named so the cut is visible rather than silent: `KELP:SOLANA-BEACH`
  against the pier at 0.5 m, quarterly `mean` at lag 1 (r −0.312), then
  `KELP:ENCINITAS` against `NDBC:LJAC1` `min` at lag 4 (−0.311) and
  `KELP:LA-JOLLA` against `SST:LA-JOLLA` `p95` at lag 0 (−0.302). All three are
  distinct claims rather than further readings of what is registered, which is
  the honest way to describe this cut: it is not discarding duplicates, it is
  declining hypotheses. That is a judgement, and it is the operator's to
  overturn.

  Under the old cell-based rule the strongest cut row was the `NDBC:LJAC1`
  reading of Solana Beach's registered cell — a duplicate that ranked above
  every distinct claim. That it no longer appears here is the whole point of
  counting signals.

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
