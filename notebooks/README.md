# Notebooks — analyses of record

The science lives here. The dashboard computes no statistics of record (hard
rule 6) and the pipeline computes no results — it produces the `features/`
tables and stops. Everything downstream of those is a notebook, versioned in git
beside the package that built its inputs.

## Conventions

**Run top to bottom from tables of record in `features/`, and nothing else.** No
side reads of `observations/` or `raw/`. A notebook that reaches around the
features zone is a notebook whose result cannot be reproduced from a stated
input, and it stops being an analysis of record.

*Which tables* is the notebook's business, and more than one is fine — `02`
reads `deployment.parquet` and `validation.parquet` together, because the join
between them is its subject. What is not fine is a number that came from
anywhere else. The rule used to name `comparison.parquet` specifically, which
was true while it was the only table a notebook read and would have made `02`
non-compliant on the day it was written.

*Why the zone and not the observations behind it.* Two reasons, and the second
is the one that bites. A features table is a stated input with a digest, so a
result computed from it can be reproduced; the observations zone is a moving
partition set with no such handle. And reading it correctly is harder than it
looks — a DuckDB glob over the partition files can double-count where
`storage.read_observations` dedupes
(https://github.com/cweber12/kelp-compare/issues/8), so the obvious side read is
not merely untraceable but arithmetically wrong.

*Importing from `kelpcompare` is not a side read.* The rule is about **data** —
a second source of numbers is what makes a result untraceable. Schema facts are
the opposite case: which columns identify one series, and which anomalies belong
to the kelp half, are defined once in the package that wrote the table (docs/03),
and a notebook that restates them instead of importing them is one that will
keep screening the old schema after the table has moved on. Import the constant;
never open a second file. A notebook doing this should check on load that the
table still matches what it imported, so a package and a table that have drifted
apart say so rather than quietly analysing the overlap.

**Quote the digest.** Each notebook prints the SHA-256 of every features table
it ran against. Put those digests in any figure caption or write-up, so a number
in a document can be traced to the exact tables that produced it — and so two
write-ups can be told apart when a table is rebuilt.

*Why a digest rather than the run manifest ID docs/04 §5 asks for:* no feature
table carries the id of the run that built it. `fetch_run_id` is on every
observation row, but putting a build id on a derived table would change its bytes
on every rebuild and cost the zone its reproducibility. A digest is strictly
better anyway — it is verifiable rather than a pointer.

**To get from a digest to the run**, grep the manifests for it. Every `features`
run records the SHA-256 of each table it wrote (docs/03), and `kelpcompare
features` echoes the same 16-character prefix beside each path as it writes it:

    grep -rl dbbed1264b9ee1d8 data/raw/_manifests/

What comes back is the run that built that exact table — its code SHA, the
`--qc-max-flag` it was built at, and the warnings and gaps it recorded, none of
which the digest alone can give you. Two runs over unchanged inputs record the
same digest, so more than one manifest may name it; that is the reproducibility
property holding, not an ambiguity. Runs from before the field carry no `tables`
and a digest older than it will not be found.

**Figures go in `notebooks/figures/`, written by the notebook that owns them.**
They are regenerated on every run and committed with the executed notebook, so a
figure in the repo is always the one the current outputs describe rather than a
survivor of an older table. Each carries in its own caption the digest of the
table it was drawn from, per the rule above — a figure that leaves the repo on
its own still says what it came from.

*What is in there, and which question each one answers.* The set divides into the
record of what the screen did and the two figures for reading it:

| Figure | Written by | Answers |
|---|---|---|
| `KELP-<bed>.png` (six) | `01` §4.1 | What did every cell come out at? The completeness record — dense on purpose, and not the place to start |
| `SCREEN-RANKED.png` | `01` §9 | Which associations deserve a second look? Signals by \|r\|, sized by `n_eff`, merged cells as ticks, lag profile as a sparkline, controls interleaved, gate-withheld cells below a rule |
| `SCREEN-SIGNALS.png` | `01` §9 | What are the coefficients made of? The paired quarters behind each registered signal and the strongest control, as a time series and a scatter |

**Read `SCREEN-RANKED.png` first.** The grids are an audit artifact: 2,145 cells at
uniform visual weight answer "was everything screened" well and "what should I
look at" badly, which is the split the two `SCREEN-` figures exist to fix.

*Rendering is a package module, not notebook code.* `kelpcompare.figures` draws a
matrix it is handed and computes nothing, which keeps around figures the split
hard rule 6 draws around the dashboard: a change to a colour ramp cannot move a
number in an analysis of record. What to draw, over which series, on what feature
axis and on what scale stays in the notebook, where the science is. The axis is
itself read from `features.json` rather than restated, because the feature
columns are named after the thresholds (docs/03): a threshold retuned in the
registry renames its column, and a figure holding its own copy of the axis would
go on drawing a feature set the table no longer has.

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
of the table you meant to run against — the same string the run that wrote it
echoed beside the path. A notebook whose gate cell reports an older digest did
not pick up the rebuilt table.

## Index

| Notebook | docs/04 | What it does |
|---|---|---|
| `01-lag-screen.ipynb` | §4.1 | Kelp anomaly at *t* against every environmental feature anomaly at *t−0…4*, per polygon × series. Ranks candidates; claims nothing. Writes the six per-bed figures in `figures/` |
| `02-deployment-profile.ipynb` | §1 | What each project-sensor deployment recorded over its own in-water window — day by day, hour by hour, and against its neighbour. Reproduces every scalar feature from the daily table, and the band occupancy from both the daily and the hourly table, raising if any disagree. Writes the five `DEPLOYMENT-*` figures. Describes; tests nothing |

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

*This block and the table under it are frozen as registered, digest and
coefficients included, and are not edited when the table is rebuilt. What the
current screen says goes in the re-measurement below, so the two can be read
against each other rather than one replacing the other.*

**Only predictors are eligible.** docs/04 §5 makes `air_temperature` and
`wind_speed` **controls**: screened and reported, never pre-registered. The
reasons are mechanistic and prior to any coefficient — air temperature
re-measures sea water temperature more noisily, and scalar wind speed averages
upwelling-favorable alongshore stress against downwelling-favorable Santa Ana
wind, leaving it no sign to predict. How nearly the air re-measures the water is
printed by `01-lag-screen.ipynb` §6.2 against the table's digest, beside the two
coefficients the demotion sets it against; this file used to state the figure and
nothing computed it. `features.json` records the decision; `01-lag-screen.ipynb`
reads it from there rather than restating it. 330 of 1,750 cells are withheld on
those grounds before anything below is ranked.

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

That last step is a cut, not a criterion. 634 eligible cells collapse to 253
signals, and 250 of those are cut, so what makes these three the list is that
they are the top of a ranking — which is exactly the kind of choice this section
exists to make visible. The ranking is on |r|, and so on effect size, which
favours the shorter record at equal evidence: docs/04 §5 now says so, and
`01-lag-screen.ipynb` §7 prints what it costs.

**What the collapse is for.** Two cells agreeing on feature, lag and polygon are
one claim about one bed measured by two instruments. `NDBC:LJAC1` and
`SIO:LAJOLLA-PIER` both sit at Scripps Pier and measure the same water, so
registering them separately would count instrumentation as replication. How
alike the reference series are is measured rather than quoted:
`01-lag-screen.ipynb` §6.1 prints the whole inter-series matrix on the
`days_below_14c` anomaly against the digest of the table it read, which this
file used to state as two figures nothing in the repo computed. Beds are not
collapsed: two beds carrying one feature at one lag stay two signals, because
merging them would erase the between-bed comparison docs/04 §4.5 is built to
make — while §6.1 is also where to see how far that separation is from
independence, the six satellite pixels sitting at r ≥ 0.96 with one another.

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

### Re-measured 2026-09-02, against `sha256:dbbed1264b9ee1d8`

**The registration above is unchanged and was not re-made.** The screen was
re-run because its input was corrected, not because anything was re-chosen, and
it returned **the same three `(polygon, feature, lag)` signals**. That is the
useful part: the list survived a correction to the data underneath it, which is
a stronger statement than a fresh registration could have made — a registration
written after seeing a screen is weaker than one written before it, whatever it
says.

What moved is which cell stands for each signal, and how strongly:

| Signal | As registered | Re-measured |
|---|---|---|
| `KELP:ENCINITAS` `days_below_14c` lag 4 | `NDBC:LJAC1` 3.4 m, +0.424, n_eff 50.6, 1 cell | `SIO:LAJOLLA-PIER` 5 m, **+0.465**, n_eff 115.8, 2 cells |
| `KELP:SOLANA-BEACH` `days_below_14c` lag 4 | `SIO:LAJOLLA-PIER` 5 m, +0.406, n_eff 117.3, 2 cells | `SIO:LAJOLLA-PIER` 0.5 m, **+0.443**, n_eff 125.3, 3 cells |
| `KELP:LA-JOLLA` `degree_days_above_18c` lag 1 | `SST:LA-JOLLA`, −0.353, n_eff 61.3, 3 cells | `SST:LA-JOLLA`, −0.353, n_eff 61.3, **2 cells** |

**Where the movement came from, and why that ordering matters.** ADR-008 let a
source be declared exempt from a named QARTOD test; `spike`, tuned on a
ten-minute logger, had been removing one Scripps Pier bottom reading in five in
Q3, and stopped. So the 110-year pier record now carries the strongest cell in
two of the three groups as well as the longest one, where before it was beaten
in one group by a 71-quarter buoy series.

That correction was found and argued entirely on QC flag counts and their
seasonality
([#68](https://github.com/cweber12/kelp-compare/issues/68),
[#139](https://github.com/cweber12/kelp-compare/pull/139)), never mentioning
this screen, and its own evidence says the anomalies barely move — the
climatology absorbs almost all of the level shift. That the correction happened
to strengthen two registered signals is therefore a consequence and not a
motive, and this paragraph exists so that is checkable rather than asserted.

**Nothing here revises the registration.** Both changed references are the
ranking artefact the third bullet below already warned about: which series
stands for a signal is the strongest cell in its group, not the best instrument.
The signals are the same three; only their spokesmen changed.

**The La Jolla spread the note above flags is still worth checking**, and is now
between two cells rather than three: −0.353 through `SST:LA-JOLLA` against
−0.252 through the pier at 0.5 m, the 5 m cell having dropped out of the
eligible set. Wider than "agree" comfortably covers, as it was.

What to weigh before registering any of them:

- **Two hypotheses, not three.** Two of the three are the cold-water
  association at lag 4 on adjacent beds; the third is a heat-accumulation cell
  at a short lag with the opposite sign, so it can fail without the first two
  failing. The signal rule stops one bed being registered twice over; it does
  not make two *adjacent* beds independent evidence, and it is not meant to.
  Encinitas and Solana Beach agreeing is mild reassurance that the association
  is not one bed's noise — nothing stronger.
- **The list is ranked on |r|, and both standardised alternatives still change
  it — but no longer at the top.** |r| is an effect size and carries no sample
  size, while the signals it ranks span `n_eff` 30.2 to 161.0. Ranking instead on
  Fisher *z*, or on the 95% lower confidence bound of |ρ|, drops
  `KELP:LA-JOLLA` `degree_days_above_18c` at lag 1 and admits `KELP:LA-JOLLA`
  `days_below_14c` at lag 4 — the same substitution either way, so it is not an
  artefact of one scale.

  | Signal | r | n_eff | \|z\| | LCB | Rank by \|r\| |
  |---|---|---|---|---|---|
  | `KELP:ENCINITAS` pier 5 m `days_below_14c` lag 4 | +0.465 | 115.8 | 5.35 | 0.309 | 1 |
  | `KELP:SOLANA-BEACH` pier 0.5 m `days_below_14c` lag 4 | +0.443 | 125.3 | 5.26 | 0.290 | 2 |
  | `KELP:LA-JOLLA` pier 0.5 m `days_below_14c` lag 4 | +0.296 | 130.0 | 3.44 | 0.130 | 8 |
  | `KELP:LA-JOLLA` `SST:LA-JOLLA` `degree_days_above_18c` lag 1 | −0.353 | 61.3 | 2.82 | 0.112 | 3 |

  Read that beside the bullet above rather than as a correction to it. The
  substitution would make the list one association read three times instead of
  two hypotheses, so the tilted rule is returning the *better* list — which is
  the point, because nobody had written down why. Across the whole pool the tilt
  is mild, Spearman of |r| against `n_eff` being −0.140; it is decisive only
  here, at the cut. docs/04 §5 keeps |r| and states the reason; §7 prints all
  three rankings so the choice stays visible instead of being settled once.

  **What the re-measurement changed, and did not.** The disagreement used to
  reach the top of the list: |r| ranked the 71-quarter Encinitas cell first and
  the standardised scales ranked it third. It now does not — the first two
  signals hold rank 1 and 2 on all three scales, because the cells standing for
  them are the long pier record rather than the short buoy one. The scales
  still part company at the cut, on the third signal, exactly as before. So the
  sensitivity is unchanged in kind and narrower in reach, and the reason to
  print all three is the same.
- **The reference behind each signal is not the evidence for it.** Which series
  stands for a signal is a ranking artefact: it is the strongest cell in the
  group, not the best instrument — and the re-measurement demonstrated it, two of
  the three changing spokesman without changing signal. Encinitas now reads
  +0.465 through the pier at 5 m and +0.424 through `NDBC:LJAC1`; Solana Beach
  +0.443 through the pier at 0.5 m, +0.406 at 5 m and +0.347 through
  `NDBC:LJAC1`; La Jolla −0.353 through `SST:LA-JOLLA` and −0.252 through the
  pier at 0.5 m. Report the signal, and the spread inside it, rather than the
  series name as though it had been chosen.
- **The controls rank close behind the predictors, and that is the warning they
  exist for.** On the cells the candidate rule keeps, the strongest control
  coefficient is |r| = 0.38 against the strongest predictor's 0.47. The medians
  separate — 0.095 for sea water temperature against 0.068 and 0.070 for the two
  controls — where they were indistinguishable on the smaller grid. A screen in
  which a variable withheld on mechanistic grounds performs about as well as the
  one under test may be recovering shared seasonality rather than mechanism;
  that does not revive the controls, it discounts the predictors.

  **This margin has moved, in the predictors' favour, and it is stated here
  rather than quietly absorbed.** It read 0.38 against 0.42 until the screen was
  re-measured on 2026-09-02, at which point the strongest predictor cell gained
  0.04 and the strongest control did not move at all — the correction was to a
  sea water temperature series, and neither met parameter comes from one. So the
  caution is weaker than it was and has not gone: 0.38 against 0.47 is still one
  withheld variable reaching four fifths of the best cell the screen can offer.
  A warning that vanished the moment the data improved would have been a warning
  written to be discarded, and this one is load-bearing for how much §4.1 is
  allowed to claim.

  That comparison is between pools of unequal record length, and the direction
  is not the obvious one. Both met parameters sit on the one station, so every
  eligible control signal stops at `n_eff` ≤ 50.0 while predictors reach 161.0 —
  the 0.38-against-0.47 line is a short-record maximum set against a
  mixed-record one. Restated over the same 65 control and 253 predictor signals
  on the scales `01-lag-screen.ipynb` §7 prints:

  | Scale | Control max | Predictor max | Controls reaching the weakest registered signal |
  |---|---|---|---|
  | \|r\| | 0.380 | 0.465 | 1 of 65 |
  | \|z\| | 2.74 | 5.35 | 0 of 65 |
  | LCB | 0.114 | 0.309 | 1 of 65 |

  The caution survives standardisation rather than dissolving in it: it clears
  on Fisher *z* and returns on the confidence bound, where a control still edges
  the weakest registered signal, 0.114 against 0.112. Read it across the three.
  The |r| line alone is not the pessimistic reading of this table — it is one of
  three, and the scale that is neither a raw effect size nor a significance
  ranking agrees with it.
- **The satellite series enters the pool crippled on exactly the features it
  was wanted for.** All six `SST:*` beds are flagged `low_resolution` on
  `days_above_23c` and five of six on `days_below_14c`: over 470 bed-quarters
  those counts take only 15 to 23 distinct values, because a smoothed L4
  analysis area-averaged over a bed seldom crosses a threshold chosen against
  the in-situ record. So the SST leg reaches this list through the continuous
  features rather than the ecological count features, and `SST:ENCINITAS`
  `days_below_14c` at lag 4 — r = +0.391, which would otherwise rank seventh in
  the whole screen — is dropped by the audit rather than by the rule.

  **This is the feature thresholds, and not the QC settings, and the
  re-measurement is what separates them.** This paragraph used to lay the
  flagging at the door of
  [#113](https://github.com/cweber12/kelp-compare/issues/113) — a daily source
  inheriting QARTOD thresholds sized for a ten-minute logger. That was fixed on
  2026-09-02, `mur_sst` now running gross range alone, and **every one of these
  eleven audit rows came back byte-identical**: same 470 present, same distinct
  counts, same flags. A cause that can be removed without moving the effect was
  not the cause. What is left is `features.json`'s 23 °C and 14 °C, which are
  ecological thresholds rather than instrument ones and are not wrong — the
  satellite simply resolves them coarsely, and no QC change will alter that.
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
