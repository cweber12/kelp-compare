# 04 — Analysis Methods

**Status:** Draft for review

This document defines the QA/QC plan, the quarterly feature set, and the
statistical methods in the order they will be applied. The guiding
constraint is sample size: roughly forty years × four quarters, with cloud
gaps, is on the order of 100–150 usable kelp observations per polygon.
Methods are chosen to be interpretable and defensible at that N; anything
data-hungry is explicitly deferred.

## 1. QA/QC (applies to all high-frequency inputs; strictest for project sensors)

QARTOD-style tests, implemented with the open `ioos_qc` package, attached
as flags and never as deletions: gross range (against parameter registry
bounds), climatology range, spike, rate of change, flat line, and gap
detection. Roll-up flag per row: pass / not evaluated / suspect / fail.
Default analysis filter is pass + not-evaluated; sensitivity checks rerun
key results at pass-only.

### What `kelpcompare qc` runs today

Three of those tests are implemented: **gross range**, **spike**, and **rate
of change**. Thresholds live in `parameters.json`, not in code (ADR-004); doc
03 documents the block. A parameter with no thresholds for a test does not get
that test, and the omission is visible in `qc_tests` rather than hidden behind
a default. Every row of a series is tested, including rows ingest already
failed for falling outside a deployment window — that redundancy is the point
(doc 06 §5 check 6), and a verdict already recorded is never relaxed.

On the reviewed TidbiT deployment these settings flag nothing in-water at all,
while the install transient — a reading taken in air — fails the spike test and
comes out suspect on rate of change, having passed gross range. That is the
predicted result, and it is what the thresholds were sized against.

Four properties to keep in mind when reading flags:

- Both neighbour-reading tests withdraw rather than guess where a neighbour is
  missing. Spike says nothing at the ends of a series or either side of a gap;
  rate of change says nothing at the first row, or where the preceding value is
  absent. `ioos_qc` returns GOOD in each of those rate cases — index 0 because
  its rate array is initialised to zeros and filled only from index 1, a
  post-gap row because the masked difference makes `roc > threshold` False
  rather than unknown — so this stage overrides it. The divergence is
  deliberate: a gap is exactly where a rate test would matter most, and a pass
  it never earned survives the default `qc_flag <= 2` filter while an omission
  is visible in `qc_tests`.
- The spike test judges a sample against the midpoint of its two neighbours, so
  a spike large enough to fail necessarily pushes both neighbours past the
  suspect threshold. One bad reading costs three rows from a `qc_flag <= 2`
  query, not one.
- The same arithmetic fires on a *step*, where the midpoint is not a valid
  reference at all. An internal-bore front — a discontinuity, not a spike —
  draws suspect flags on the two rows straddling it, with nothing anomalous in
  either. The test cannot tell the two shapes apart.
- A suspect flag *removes* data under the default filter, and the thresholds
  have less headroom than that warrants. They are provisional, tuned against a
  single three-week deployment: on that record the largest 10-minute step is
  2.11 °C (12.7 °C/h) against an 18 °C/h suspect threshold — a factor of 1.4 —
  and the largest spike statistic is 0.91 °C against 1.5 °C. Those three weeks
  span 17.8–24.1 °C with no upwelling excursion in them, so the events the
  thresholds most need to survive are absent from the data they were sized
  against: a 4 °C drop in one 10-minute sample is 24 °C/h and comes out
  suspect. Since §2 makes the quarterly minimum and `days_below_14c` the
  nitrate proxy, the rows at risk are the coldest rows of the sharpest
  intrusions — the signal, not the noise. Retuning is deferred on the same
  grounds as climatology below: it needs a record that contains the events,
  and this one does not.

**Deferred, with reasons.** *Climatology* needs a multi-year per-quarter
baseline that no series in this project yet has; running it against three weeks
of data would test the data against itself. *Flat line* fires on genuinely
quiescent water at naive settings — the reviewed deployment holds within
instrument resolution for over four hours at one point — so its tolerance has
to be tied to instrument resolution and its duration tuned against a longer
record before it can be trusted to flag a stuck sensor rather than a calm
night. *Gap detection* is reported at ingest as a cadence audit (doc 06 §5
check 3) rather than as a per-row flag.

### Neighbor validation

Project sensors additionally get neighbor validation per deployment: bias,
RMSE, and correlation against the nearest public station and against
satellite SST at the sensor location, reported in a standing validation
table. This is the evidence base for the claim that non-NOAA/SCCOOS
sensors are trustworthy — and later, for the more interesting claim that
they capture *local* signal the public network misses.

Not built, and the blocker is no longer the one this section first recorded. The
public-station half of the comparison exists: `NDBC:LJAC1` now carries 2007–2026
in `observations/`, and `sites.json` names it in both project sites'
`neighbor_refs` beside `COOPS:9410230`. What is still missing is the other
neighbour — no satellite SST fetcher exists, and none for CO-OPS either (doc 02)
— and, more immediately, the sensor side of it: **no project-sensor deployment
has been ingested**, so there is nothing in `observations/` to validate. The
three-week TidbiT record the QC thresholds above were sized against lives in
`tests/fixtures/`, which is not a zone.

## 2. Quarterly feature definitions

Computed per QC series × quarter from QC-filtered observations, then
converted to anomalies. Temperature family: mean, min (proxy for
nitrate-bearing upwelled water — in Southern California nitrate is high only
in cold water, so quarterly minimum temperature carries nutrient
information), max, 5th/95th percentiles, variance, days above 20 °C and
23 °C (giant kelp stress thresholds; exact values to be finalized against
literature and treated as tunable), longest consecutive spell above 20 °C,
degree-days above 18 °C, days below 14 °C. Wave family (CDIP): count of
events with significant height above thresholds, longest event duration,
quarterly max height. Water level (CO-OPS): mean sea-level anomaly vs.
predictions. Covariates: marine heatwave days in quarter (Hobday et al.
definition computed on the long SST series with a fixed 1983–2012
baseline), ENSO state (ONI), BEUTI/CUTI quarterly means.

Thresholds, the coverage floor and the baseline window live in
`registry/features.json`, not in code (ADR-006); doc 03 documents the file
and the output schema. The universal statistics set and the temperature set
are built; the wave and water-level sets are refused until their fetchers
exist. The kelp family — canopy area and canopy extent — is built from the
Kelp Watch export directly and takes no feature set, because the export
arrives already reduced to one value per quarter.

### What each feature counts, exactly

Stated precisely enough that a reviewer can reproduce a number by hand from
the raw series.

**Quarters are UTC.** The consequence, stated rather than discovered: a
reading taken at 5pm on 31 December on the US west coast is 01:00 on 1
January UTC and falls in the *following* Q1. Site-local quarters would fix
that at the cost of reintroducing local time past the normalizer, and would
make the calendar depend on which site a row came from. Daylight saving
becomes irrelevant rather than handled.

**Percentiles interpolate linearly** between the two nearest order
statistics; **variance is the sample convention** (`ddof = 1`), so a
single-observation quarter yields a null variance rather than a zero, which
would claim the water did not vary.

**The day is the unit for every threshold feature**, and every day with at
least one observation counts. `n_days_observed` records how many days that
was, so a count reads as a floor rather than as a census. Requiring a minimum
per-day coverage was rejected: it invents a second coverage threshold, and it
would discard the hottest day of a quarter if that day happened to be
short-sampled.

- **`days_above_{t}`** — the number of observed days whose daily *maximum*
  exceeds `t`.
- **`days_below_{t}`** — the number of observed days whose daily *minimum*
  falls below `t`.
- **`degree_days_above_{t}`** — the sum, over observed days, of the positive
  excess of that day's *mean* over `t`, in °C·day. The daily mean is the
  unweighted mean of the day's samples. A trapezoidal time integral and a
  per-sample rectangle sum were both rejected: they weight by sample spacing,
  so an hourly station and a 10-minute logger would report different
  accumulations for identical water, and a burst of clustered samples would
  overweight itself.
- **`max_spell_above_{t}_days`** — the longest run of *consecutive calendar
  days* each of which qualifies under the `days_above` rule. **A spell is
  broken by an unobserved day, never bridged across one**: joining two
  qualifying days either side of a day nobody measured would assert something
  about a day with no data.
- **`max_spell_above_{t}_gap_interrupted`** — whether the longest run ended at
  an unobserved day inside the quarter, meaning the reported length is a floor
  and the true spell may have been longer. A run ended by an observed day that
  simply did not qualify is a measurement, not a floor, and is not marked;
  neither is one ended by the quarter boundary, which is a limitation of
  quarterly features rather than a hole in the record. Where several runs tie
  for longest, the marker is set if any of them touched a gap.

### Coverage, and the biases to disclose

`pct_coverage` is `n_obs / expected_obs`, where `expected_obs` is the
quarter's duration divided by the series' own **median observed inter-sample
interval**. The median is robust to the thing being measured: gaps are the
tail of the interval distribution, not its middle, so an hourly series
missing half a quarter still has a median interval of an hour and correctly
scores one half. A registry-declared cadence was rejected — nothing records
one, and upstream changes sampling rates across decades without telling
anyone — as was distinct-days-with-data, under which a once-daily series
would score full coverage while being useless for a daily maximum. Doc 03
documents the clamp, the under-two-observations case, and the storage
consequences.

Coverage rule: quarters below the floor (default 60%) are flagged `usable =
false` rather than dropped, mirroring the missing-not-zero handling of kelp.
Analysis filters on `usable`; the floor stays a sensitivity knob.

Two biases follow from the day-based definitions and are disclosed here
rather than left to be discovered:

- **Day counts run low under partial coverage.** A day observed only
  overnight cannot show its daytime maximum, so `days_above_{t}` understates
  in an under-covered quarter. The coverage floor and `n_days_observed` exist
  to expose this.
- **Spell lengths are floors, not measurements**, wherever
  `..._gap_interrupted` is true.

### The kelp half: coverage means something different, and biases harder

`quarterly_kelp` runs the same machinery on the same calendar, but its coverage
is spatial rather than temporal: `pct_cells_observed` is `n_cells_observed /
n_cells`, the cloud-free 30 m cells over the bed's historic footprint. The same
floor applies, shared rather than duplicated — it answers the same question, and
a second knob with no separate evidence behind it would be a knob nobody could
tune.

**Three properties to state rather than let a reader discover.**

- **A partially observed quarter is biased low, not merely noisier.**
  `kelp_area_m2` is a *sum over the cells that were seen*, so a quarter with two
  thirds of its bed under cloud reports roughly two thirds of the canopy that
  was there. This is worse than the environmental analogue, where partial
  coverage adds noise and misses extremes but does not systematically shrink the
  mean. **Nothing corrects for it.** Scaling by the observed fraction was
  rejected: it is imputation wearing a feature's clothes, and it assumes the
  unseen part of a bed looks like the seen part, which is exactly what a patchy
  bed does not do. The quarter is flagged `usable = false` below the floor and
  keeps its value.
- **Missing is written as zero upstream**, and only the cloud-free cell count
  tells a cloud gap from a genuinely empty bed (doc 02). The parser applies that
  rule; by the time a quarter reaches this table an unobserved one carries null.
  The distinction matters most in a marginal bed, where zero is the normal
  reading.
- **Cloud gaps are seasonally biased, so the missingness is not random.** Across
  the six exported San Diego county beds, 9.1% of Q4 and 5.8% of Q1 have no
  cloud-free observation, against 0.8% of Q3. Any analysis that drops null
  quarters is therefore dropping winter preferentially, which §6 already names
  as an interpretive limit.

Two measured quantities carry anomalies: `kelp_area_m2`, how much canopy there
was, and `n_cells_kelp`, how far it spread. A bed can thin without shrinking and
shrink without thinning; the notebook chooses which answers its question.

## 3. Climatology and anomalies

For every series (kelp and environmental), compute the quarterly
climatology — the long-run mean for each of Q1–Q4 — and work in anomalies
(or standardized anomalies) thereafter. This removes the seasonal cycle
that would otherwise dominate every correlation. The climatology baseline
period is fixed and recorded so anomalies don't shift as new data arrives.
STL decomposition is available as a cross-check on the simpler climatology
subtraction.

### The baseline: 2007–2019, minimum 10 usable years

This supersedes the 1984–2013 window earlier drafts of this document
proposed. **The NDBC LJAC1 archive begins in 2007** — every year from 1984 to
2004 returns 404, and 2005–2006 fail to parse under an older header layout —
so the proposed window holds seven usable years against a ten-year minimum
and would leave every anomaly null on the project's only long record.
2007–2019 is thirteen complete years, every quarter of them clearing the
coverage floor, and it ends well before the tail of the record so a later
backfill cannot shift an anomaly already computed.

**The cost, documented rather than mitigated: 2007–2019 contains the
2014–2016 marine heatwave.** The warm-season baseline is therefore raised by
the very event the analysis most wants to detect, and subsequent warm
anomalies are damped accordingly — a systematic, one-directional bias that
must be stated wherever a warm anomaly from this baseline is reported. The
clean pre-heatwave alternative, 2007–2013, is seven years, which is too thin
to support the word climatology and would force the minimum down to meet it.

**A baseline too thin to be one produces no anomaly.** Below the minimum
years the anomaly is null rather than computed, on the same reasoning §1
gives for deferring the climatology *QC* test: running a baseline against a
record too short to contain one tests the data against itself. **Only usable,
complete quarters contribute** — a half-observed quarter cannot drag the
baseline it is later compared against, and an in-progress quarter cannot bias
it toward whatever part of the year the run happened in.

Every measured feature gets an `_anom` twin; bookkeeping columns do not, so
the table never offers the anomaly of a row count. Anomalies *are* computed
for unusable quarters, because `usable` is already the single gate on the
table. The climatology is written to its own table with its mean, standard
deviation, contributing-year count and window (doc 03), so the promise that
anomalies do not shift is checkable by diffing two runs.

**The environmental anomaly columns are populated for the public station and
empty for the project sensor.** `NDBC:LJAC1` spans the baseline window on every
parameter — thirteen usable years in 2007–2019 for `sea_water_temperature`,
eleven for `air_temperature` and `wind_speed` — so 178 of its 231 quarterly rows
carry at least one anomaly, against baselines of ten to thirteen years. The
exception is Q2 for the two met parameters, whose Q2 baseline holds nine years
against the ten-year minimum and therefore produces nothing. The project sensor
is the half still empty, and doubly so: its only deployment is three weeks long,
which is roughly a quarter of one quarter and unusable by the coverage rule, and
it has not been ingested at all.

The columns shipped before any of this was true, rather than being deferred, and
that decision paid: the schema did not change under downstream readers when the
LJAC1 history landed.

**The kelp anomalies are real, and denser than the environmental ones.** The
Kelp Watch record runs 1984–2026, so every polygon clears the ten-year minimum
with all thirteen baseline years contributing, and 976 of 1,020 quarterly rows
carry an anomaly. The shared climatology therefore now runs against a long
enough series on both sides of the comparison rather than one — and because a
single implementation produces both (doc 03), the two halves cannot drift apart
in how they treat a baseline.

One consequence follows immediately and is worth stating before anyone reads a
number: the baseline contains the 2014–2016 marine heatwave on the kelp side
too, so a warm-period kelp anomaly is damped by the event it is measuring, in
the same direction and for the same reason as the environmental one.

## 4. Analysis ladder

Applied in order; each rung informs whether the next is warranted.

**4.1 Lagged cross-correlation screen.** Kelp anomaly at quarter t vs.
each environmental feature anomaly at t−0…4, per polygon × environmental
series. Output is a lag–feature correlation matrix (the dashboard's "lag
explorer"). Purpose: recover the known physics (heat stress at short
lags, cold/nitrate association, wave removal in winter) and rank which
project-sensor features carry signal. Screening only — no significance
claims from this step.

`comparison.parquet` (doc 03) makes this a query rather than a script. Three
conventions it fixes, so that a result cannot depend on which of them a notebook
assumed:

- **The environment leads and kelp responds.** Lag 2 on a 2015Q3 row is kelp in
  2015Q3 against the water in 2015Q1. `env_year` and `env_quarter` are recorded
  on every row, so the direction is checkable by reading one rather than by
  re-deriving it — which matters because getting it backwards produces a
  correlation matrix that reads as kelp predicting temperature, and that looks
  like a finding rather than a bug.
- **Lag 0 is included.** A same-quarter association is a hypothesis like any
  other, and omitting it would make its absence look like a result.
- **Nothing is filtered.** Rows survive where either side is unusable, or where
  the lag reaches before the environmental record and the environmental side is
  null. `usable` is the single gate and the analysis applies it once, so what
  filtering cost is visible rather than already spent.

Which polygon pairs with which site is read from `polygons.geojson`, never
matched by name — doc 03's integrity rule, at the one place that would be
tempted to break it.

**4.2 Event studies.** Define discrete events: marine heatwaves, top-decile
wave quarters, El Niño quarters. Superposed-epoch analysis of kelp anomaly
from four quarters before to eight after event onset, compared against
matched non-event windows. With small N, event composites are more
interpretable and more robust than regression, and they directly answer
"what happened to kelp around the 2014–2016 heatwave near our sites?"

**4.3 Regression with honest errors.** GLM/GAM (statsmodels, pyGAM) of
kelp anomaly on a small set of lagged features chosen from 4.1 — small
because ~100–150 observations cannot support many predictors.
Requirements: autocorrelation-robust inference (Newey–West/HAC errors or
explicit AR terms), collinearity checks (temperature and nitrate proxies
are strongly anti-correlated by construction, so coefficients must not be
read as isolating "thermal" vs. "nutrient" effects without care), and
out-of-sample checks via blocked (not shuffled) cross-validation in time.

**4.4 Changepoint detection.** `ruptures` on the kelp anomaly series to
locate regime shifts, compared against shift dates in the environmental
series and known events. Descriptive framing.

**4.5 Spatial signal test — the project's key question.** For each project
sensor, fit the same simple model against kelp in concentric/directional
polygons at increasing distance. If explanatory power decays with distance
from the sensor — and exceeds what the nearest public station achieves for
the near polygons — that is direct evidence the independent sensors add
local information beyond NOAA/SCCOOS. This comparison (project sensor vs.
public neighbor vs. satellite SST as competing predictors for the same
kelp series) is the analysis the whole system is built to support.

**Deferred:** machine-learning models (insufficient N for them to beat
classical methods honestly), HF-radar transport analysis, and multi-species
work — revisit if the sensor network or study area grows.

## 5. Multiple comparisons and reporting discipline

The lag × feature × polygon screen generates many correlations; treat 4.1
as exploratory, pre-register (informally, in the analysis README) the
handful of relationships carried into 4.3, and report effect sizes with
uncertainty rather than p-value collections. All notebooks run
start-to-finish from the comparison table; figures for team sharing export
with the run manifest ID in the caption.

## 6. Known interpretive limits

Landsat canopy is a surface expression — subsurface kelp condition, urchin
grazing, and predator dynamics are invisible to every dataset in this
system, so environmental models will leave variance unexplained for
reasons that are ecological, not statistical. Cloud-gap missingness is
seasonally biased (worse in winter), which slightly biases which quarters
are comparable. Sensor deployments are short relative to the kelp record;
early analyses will lean on public stations for the historical period and
use project sensors for the recent overlap, with the spatial signal test
(4.5) confined to the overlap window.
