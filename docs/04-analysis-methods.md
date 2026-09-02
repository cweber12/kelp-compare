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

**The wave parameters are flagged asymmetrically, and the asymmetry is the
finding.** `wave_significant_height` carries spike thresholds and deliberately no
rate of change; `wave_peak_period` carries neither. Both Waveriders report every 30
minutes, and the ~292,000 readings behind this are a much larger record than the
temperature thresholds above were sized on.

Two windows of the same series decide it. A real storm build is a monotonic ramp —
2.03, 2.39, 2.75, 2.99, 3.01, 3.89, 4.00, 4.16, 4.52, 4.86 m through 22 February
2023 — whose steepest spike statistic is only 0.43 m, because the midpoint of a
ramp's neighbours tracks the ramp. A fault is a single sample that departs and
returns: 1.36, 1.26, 1.38, 1.33, **2.98**, 1.23, 1.27 m on 11 January 2018, a spike
statistic of 1.70 m. Spike tells those apart by a factor of four. Rate of change
cannot — that same real ramp steps at 1.76 m/h against the fault's 2.04–2.11 m/h —
so a rate threshold placed between them would flag genuine storm growth, which is
the event wave data exists to capture. That is this section's own worry about the
temperature thresholds, arriving before the mistake rather than after it.

`wave_peak_period` gets neither test for a different reason. The peak period is
where the spectral maximum sits, so it hops discontinuously whenever two swell
trains of similar energy compete: its spike statistic reaches 8.71 s at p99 and its
rate of change 20.6 s/h. A neighbour-difference test has no power to separate that
from a fault, and https://github.com/cweber12/kelp-compare/issues/68 is the record of what
happens when one is applied anyway.

Across both stations the spike block flags **1 suspect and 0 fail** in 291,883
evaluated readings — the 2.98 m sample above, inside a 2018 Q1 the coverage floor
already marks unusable. So it changes no feature value today; it is there for the
next fault. A suspect of 0.75 would have caught all five of the January 2018
excursions rather than one, and was rejected: it sits 1.19× above the worst real
spike in eleven years (0.63 m) against 1.59× at 1.0, and a suspect flag *removes*
data under the default filter, so the thin-headroom mistake this section already
records against the temperature thresholds is not one to repeat knowingly.

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

**A reference is only comparable at a comparable depth, and the constraint is
per-statistic.** `sensor_depths_m` records where a public station's instrument
actually sits, and the gap between that and a deployment's `depth_m` is not an
offset that can be corrected out afterwards — below the thermocline it is most
of the signal. So, against a reference at another depth:

- **Bias is not reportable.** It measures stratification and prints it as
  instrument error, which inverts what this table is evidence for.
- **RMSE is not reportable.** It inherits the same offset.
- **Correlation is**, because both series still track the same synoptic
  forcing — but it degrades as the gap grows and must be reported labelled
  with the gap rather than presented as agreement.

`NDBC:LJAC1`'s only sea-water-temperature sensor is 3.4 m below MLLW (doc 02).
That is comparable to `PROJ:TIDBIT-1` at 8.23 m and **not** to `PROJ:TIDBIT-2`
at 16.76 m, which sits below the summer thermocline — its site note records the
gap and the evidence for it. Both sites name the station in `neighbor_refs` all
the same, because it is genuinely the nearest one; being nearest and being
depth-comparable are different questions and the registry answers only the
first. It cannot answer the second as a field, because the gap is a property of
the pair rather than of either station, so this table's code has to read
`sensor_depths_m` against each deployment's `depth_m` and decide per statistic.

**Comparable is a configured tolerance, and it is provisional.**
`policy.neighbor_depth_tolerance_m` in `registry/features.json` sets the gap
within which bias and RMSE are reported; it defaults to **5.0 m** and is absent
from the file until someone disagrees with that. The default is not arbitrary
but it is thin: it is set from the only two pairs this project has measured —
`NDBC:LJAC1` at 3.4 m sits 4.83 m above `PROJ:TIDBIT-1` and runs about 1 °C
warmer, and 13.36 m above `PROJ:TIDBIT-2` and runs about 5 °C warmer. So 5.0 m
admits the first and refuses the second, which is the verdict this section
already reached by hand. It is a stratification threshold standing on two
observations of one summer, and it should be retuned against a record that spans
a winter, when the water column is mixed and a 13 m gap may be no gap at all.

**Both sides are reduced to a common cadence before comparing** — the coarser of
the two median native intervals, each side contributing its mean within the bin.
Doc 03 records that rule, the two alternatives rejected, and the caveat that a
grab sample against a bin mean is not like for like.

**Built** — `src/kelpcompare/features/validation.py`, written by `kelpcompare
validate` into `features/validation.parquet` (doc 03). It is its own command
rather than part of `kelpcompare features`, because it needs the site registry
and `features` deliberately does not take one: `neighbor_refs`,
`same_platform_as` and `sensor_depths_m` are all registry facts, and the
quarterly builder is written so it can never come to depend on them.

What remains missing is the *other* neighbour this section asks for, and the
reason is now narrower than it was. There is a satellite SST fetcher — JPL MUR
L4, doc 02 — but it produces a series **per kelp bed**, which is what §4.5
needs and is not what this section asks for. This section asks for satellite SST
*at the sensor location*, and at MUR's 1 km those are different numbers: a bed
mean is an average over 5–34 cells, and `PROJ:TIDBIT-1` sits in one of them.

So a project sensor today still has exactly one independent reference:
`NDBC:LJAC1`, which `COOPS:9410230` folds into. A validation table with one
reference can say a sensor tracks its neighbour; it cannot yet say the sensor
captures local signal the network misses, which is the more interesting claim
and the one §4.5 is for.

Building the sensor-location series is a small change to the fetcher and a
larger question to this table, which is why it was scoped out rather than
folded in: it has to be decided whether a satellite series enters
`neighbor_refs` at all, and **what depth it is comparable at**. MUR publishes
`sea_surface_foundation_temperature` and no depth, so the bed rows land with a
null `depth_m` (doc 02). Against `PROJ:TIDBIT-1` at 8.23 m the
`neighbor_depth_tolerance_m` rule above has nothing to compare, and a null is
not a gap this table may fill with a guess — the whole point of the rule is that
an unstated depth must not become an assumed one.

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

### The wave family stays deferred, and now for a measured reason

The refusal above stands on its own — doc 02 attributes this family to CDIP, and
CDIP has no fetcher. But the NDBC Waverider records added since are the first wave
data this project holds (`NDBC:46254` from 2015-02, `NDBC:46266` from 2019-12, both
reporting every 30 minutes), and measuring them says the family would not earn its
place at these stations even once a fetcher exists.

**The one feature named above that survives is already built.** *Quarterly max
height* is the universal `statistics` set's `max`, live in every quarter: at
`NDBC:46254` the Q3 maximum ranges 1.14–1.60 m across twelve years (sd 0.167 m).

**The other two are degenerate exactly where they could be used.** Counting
observed days whose maximum significant height clears a threshold, over 46254's
whole record:

| threshold | year-quarters with no event day at all |
|---|---|
| 2.0 m | Q1 1/11, Q2 1/11, **Q3 12/12**, Q4 3/11 |
| 1.5 m | Q1 0/11, Q2 0/11, **Q3 6/12**, Q4 0/11 |
| 1.0 m | none, in any quarter |

At a storm-scale threshold Q3 is a constant zero — a column with no variance for an
anomaly to be taken against. At the only threshold that varies in every quarter, 1.0 m,
the "event" fires on 472 of 877 Q1 days and 163 of 1,052 Q3 days: that is ordinary
winter weather being counted, not a storm. No value is both, and the threshold is
per-parameter rather than per-quarter, so one value has to serve all four.

**And the quarter that holds the events cannot carry an anomaly.** Q1 is where the
waves are — 178 observed days above 1.5 m against Q3's six — and it is the quarter §3
leaves null at this station. Coverage is a property of the buoy rather than of the
parameter: `WVHT`, `DPD` and `WTMP` arrive in the same rows, so all three hold nine
usable Q1 years against a ten-year minimum, and every Q1 wave anomaly in
`comparison.parquet` is null. Wave data does not fill that hole; §3 records what would.

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

**A series that cannot cover the window may be given its own, declared in
`features.json`.** The window above is an artifact of one station's record —
it is 2007–2019 *because* LJAC1 begins in 2007 — so every newer nearshore
station is otherwise structurally ineligible for an anomaly. The nearest
public stations to two of the beds begin in 2015-02 and 2019-12; neither can
supply this baseline, and both would otherwise be inert in §4.1 and §4.5.

An override is **declared, never derived**. A window computed from whatever
years have landed would grow with every backfill and move every anomaly ever
taken against it, which is the one thing the fixed window exists to prevent.
The alternative was measured before being rejected: Scripps Pier's record
begins in 1916, and its own full record as a baseline shifts the climatology
by 0.77–1.02 °C in every quarter — larger than most anomalies being studied.
ADR-007 records the decision.

`min_years` is **not** overridable and is taken from the canonical window. How
thin is too thin for a climatology belongs to the method rather than to a
station, and per-station minimums would make the weakest baselines the ones
nearest the beds. So a station with six usable years stays null whatever
window is declared for it.

**A declared window buys whole quarters, not a whole series, and at `NDBC:46254`
the quarter it misses is winter.** Its 2015–2025 window holds eleven usable years
in Q2, Q3 and Q4 and nine in Q1, because 2015 Q1 is 0.522 covered (the record opens
on 12 February) and 2018 Q1 is 0.195 (February is missing outright). Nine against a
minimum of ten leaves Q1 null, so the nearest public station to `KELP:LA-JOLLA`
contributes nothing to §4.5 in the season storm-driven canopy removal happens.

Three things could close it, none free. NDBC's yearly archive for 2026 is not
published yet and the realtime feed reaches back only about 45 days, so 2026 Q1 is
absent rather than unusable; when that file lands, Q1 reaches ten years — but only
if the window's end year is moved, which shifts every anomaly already taken against
2015–2025 and is therefore a deliberate act under ADR-007, not a refresh. CDIP is
the other route: 46254 is a Scripps-operated Waverider and doc 02 already says wave
data for this location comes from CDIP, whose own archive for the same buoy may
predate the NDBC record and reach the canonical window. Otherwise the hole stands.

Lowering `coverage_floor` to 0.5 would also reach ten years by admitting 2015 Q1 at
0.521, and is rejected: it is a global knob tuned to rescue one quarter of one
station, and this section already forbids a half-observed quarter dragging a
baseline it is later compared against.

**The cost, documented rather than mitigated: two windows can coexist in one
screen.** Anomalies taken against different baselines are not strictly
comparable, which matters most in §4.5, where the whole point is to compare
what a project sensor and a public station each explain. The window is stamped
on every climatology row, so a reader can see which baseline produced a number
and exclude it — the same "make it visible rather than filter it" posture §1
takes with QC flags. Any §4.5 result that mixes windows must say so.

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

**All three legs now exist.** The satellite leg is JPL MUR L4 SST reduced to one
series per bed (doc 02), landing as six derived sites — `SST:LA-JOLLA` … — each
paired with its own bed by leg (d) of the doc 03 pairing rule. It is the only
leg that reaches all six beds with a series measured *at* the bed rather than
borrowed from up the coast, which matters most for the two below.

*Which* public station plays the neighbor is not a free choice, and it is
recorded rather than made per analysis. `polygons.geojson` pairs each polygon
with the project sensors, with every public station within 8 km that has an
ingested record, and with the references able to supply a climatology; doc 03
states the rule and the registry's own `_provisional` block carries it. Two
consequences bear on how a §4.5 result is read.

**Two beds have no station in range.** `KELP:SAN-DIEGO` and
`KELP:IMPERIAL-BEACH` keep La Jolla references 13.5 km and 32.3 km away,
because the only nearer sites are outfall moorings without a usable record. The
neighbor leg of the comparison is correspondingly weak for those two, and a
result that reads as "the project sensor beats the public station" there is
partly a statement about how far away the public station is. The satellite leg
does not inherit that weakness — `SST:SAN-DIEGO` and `SST:IMPERIAL-BEACH` are
over those beds — which cuts the other way too: on those two beds the satellite
is the *better-placed* predictor, so it beating a 32 km station is not evidence
about what a satellite can see. The stations
nearest them are held out on a written gate — a record overlapping the kelp
series, and evidence of tracking regional forcing — not on distance.

**A near station need not carry anomalies.** `NDBC:46266` sits inside
`KELP:DEL-MAR` and is the nearest station to three beds, but its record begins
2019-12 and clears no baseline under §3, so it enters the §4.1 screen with a
null environmental side. It is paired for the raw series and the depth
comparison it does support; the anomaly comparison for those beds still rests
on the long-record references.

**Deferred:** machine-learning models (insufficient N for them to beat
classical methods honestly), HF-radar transport analysis, and multi-species
work — revisit if the sensor network or study area grows.

## 5. Multiple comparisons and reporting discipline

The lag × feature × polygon screen generates many correlations; treat 4.1
as exploratory, pre-register (informally, in the analysis README) the
relationships carried into 4.3, and report effect sizes with
uncertainty rather than p-value collections. All notebooks run
start-to-finish from the comparison table; figures for team sharing export
with the run manifest ID in the caption.

### What may be registered, and how many

"A handful" was a placeholder while nothing had been registered and the grid
was 330 cells. It is now the gate on the whole 4.3 rung, and the pool grows
whenever a reference series is added — one station took it from 330 to 990, and
the satellite leg took it to 1,420 — so the rule is stated as a count of
*signals* rather than of cells.

**A signal is one (feature, lag, polygon).** Two screened cells that agree on
all three are the same claim about the same bed measured by different
instruments, and they register once. `NDBC:LJAC1` and `SIO:LAJOLLA-PIER` both
sit at Scripps Pier and measure the same water; the pier's two depths are more
nearly one series still. Counting those as separate registrations would be
counting instrumentation as replication.

**How alike they are is measured rather than quoted.** `01-lag-screen.ipynb`
§6.1 prints the inter-series correlation matrix over the `days_below_14c`
anomaly, for every reference series the comparison table carries and against
that table's digest; read the coefficients there. This section stated six of
them in prose from the day the rule was written and nothing in the repo computed
them, so they aged while the pool grew from 330 predictor cells to 1,420 — and
by the time one was checked, its overlap count no longer reproduced. That count
was the lag-0 reading of the pier pair; §6.1 takes the wider one, over every
environmental quarter the table reaches at any lag, and prints both.

**Beds stay separate, deliberately.** A stricter class collapsing (feature, lag)
across polygons would erase the between-bed comparison 4.5 exists to make, so
two beds carrying the same feature at the same lag are two signals — while
noting that adjacent beds are not independent evidence either, which is a matter
for how the result is read rather than for what may be registered.

**Registration is capped at *k* = 3 signals**, chosen by |r| over the eligible
predictor cells with the strongest cell in each signal standing for it. Three
because 4.3 has ~100–150 observations and cannot support more predictors than
that honestly, which is the same constraint 4.3 already states; the cap is set
here rather than read off a ranking. That the cut is a cut and not a criterion
stays true — several hundred cells clear the eligibility conditions before it —
which is why the selected list is written down before anything is fitted.

**That ranking is on effect size, and the choice has a direction.** |r| says how
large an association is and nothing about how much record it rests on, while the
pool it ranks spans `n_eff` 30.2 to 161.0 — a fivefold spread. At equal evidence
the rule therefore prefers the shorter series, and the `n_eff` ≥ 30 floor is the
only guard against it. Ranking on an effect size is what this section's own
"effect sizes rather than p-value collections" asks for, and it is also why the
obvious alternative is not simply better: Fisher *z* is monotone in the p-value,
so ranking on *z* is the significance tally forbidden below, under another name.

**What the tilt costs is measured rather than assumed.** `01-lag-screen.ipynb`
§7 ranks the same signals three ways — |r|, Fisher *z*, and the 95% lower
confidence bound on |ρ|, the last being the one scale that discounts imprecision
without becoming a p-value ranking. On the screen at `sha256:dbbed1264b9ee1d8`
the tilt is mild across the pool (Spearman of |r| against `n_eff` = −0.140) and
decisive at the cut: *both* standardised scales drop the same registered signal
and admit the same replacement. It reaches only the cut: on this screen the top
two signals hold rank 1 and 2 on all three scales, where on the screen the list
was registered against, `sha256:4cde6f9d95207dc1`, |r| and the standardised
scales disagreed about which came first. What they buy is a third reading of the
cold-water association in place of the list's only independent mechanism, so the
fairer ranking here returns the worse list. §7 also restates the control check
on all three scales, where it clears on *z* and survives on the confidence
bound — the caution `notebooks/README.md` carries is not an artefact of |r|.

**The rule is not restated on that evidence, and could not honestly be.** The
list each scale returns is now visible, so picking the scale from it would be
picking a rule by the answer it gives — the selection-on-outcome pre-registration
exists to prevent, which it does not stop being because a human makes the call.
|r| stands, with §7 printing the alternatives beside it as a standing
sensitivity. A change to the ranking rule is stated before a screen is run and
takes effect at the next registration, against the next digest, with both lists
reported.

### No family-wise correction is applied at 4.3, and why

Pre-registration is what controls multiplicity here. The screen is exploratory
and claims nothing; 4.3 fits the *k* signals named in advance, so there is no
post-hoc selection at that rung for a correction to undo. Applying Bonferroni or
an FDR procedure over the registered signals on top of that would be answering a
question nobody asked, and it would do so by assuming an independence
`01-lag-screen.ipynb` §6.1 measures the absence of — the signals share beds,
share stations, and sit on autocorrelated series.

What replaces a correction is three obligations, none optional:

- **Autocorrelation-robust uncertainty on every coefficient** (HAC errors or
  explicit AR terms), which 4.3 already requires and which is where the
  dependence between quarters is actually paid for.
- **All *k* registered signals reported**, whatever they come out as. A
  registered relationship that fails is a result; dropping it is what
  registration exists to prevent.
- **No significance count.** Effect sizes with intervals, not a tally of how
  many of three cleared a threshold — that tally is the p-value collection the
  paragraph above forbids, reassembled.

Anything fitted that was *not* registered is exploratory and is reported as
such, in the same breath as the number.

### Predictors and controls

Not every parameter in the screen is eligible for that pre-registration.
`features.json` gives each one a `role`: a **predictor** may be registered and
carried into 4.3; a **control** is screened and reported and never registered,
because its coefficient is evidence about the screen rather than about kelp.
The role governs the analysis and nothing else — a control is fetched,
normalized, flagged, aggregated and stored exactly as a predictor is, so a
demotion withholds a parameter from a claim without withholding a row. That is
§1's flags-not-deletions posture applied one layer up, and it is why the
demotion below is a registry edit rather than a filter in a fetcher.

**`air_temperature` and `wind_speed` are controls.** Two separate reasons, and
neither is a coefficient that came out weak:

- **Air temperature is largely re-measuring the water.** Coastal air temperature
  is substantially a consequence of the SST beneath it, so as a predictor it is
  a noisier proxy for a quantity this project measures directly, in the medium
  kelp actually lives in. How nearly it re-measures the water is printed by
  `01-lag-screen.ipynb` §6.2, against the digest of the table it read: the
  quarterly mean anomalies of all three `NDBC:LJAC1` parameters, over the
  quarters where every one of them is usable — the gating this section used to
  describe and nothing applied. §6.2 also prints what reading each pair on its
  own quarters instead would give, because the comparison this demotion rests on
  needs common quarters and that requirement costs something.
- **Scalar wind speed mixes two opposite mechanisms.** Upwelling is driven by
  equatorward alongshore wind stress: a northwesterly is upwelling-favorable,
  bringing the cold nutrient-rich water §2 makes `days_below_14c` a proxy for. A
  Santa Ana is offshore and downwelling-favorable — warm and nutrient-poor. Both
  register as speed, so the variable averages a signal against its own negation
  and its coefficient has no sign to predict. The variables that separate them
  are the ones doc 02 already names: BEUTI/CUTI for upwelling, CDIP for
  storm-driven canopy removal. A direction-resolved wind stress would reopen the
  question; scalar speed does not.

Neither can serve 4.5 in any case. The project sensors measure water
temperature, so a met parameter is neither the quantity under test nor a
validation reference for it.

**What controls are for.** Keeping them is not politeness. If air temperature
correlates with kelp about as strongly as sea water temperature does, that is
evidence the screen is recovering shared seasonality rather than mechanism — and
a screen carrying one predictor family cannot establish that about itself. The
notebook therefore reports the control cells beside the predictor cells rather
than hiding them, and only the ranking that feeds registration is restricted.

**The multiple-comparison arithmetic moves as a side effect, and only as one.**
Restricting the pool once halved the cells eligible for registration — 330 of
660. It now withholds under a fifth: 330 of 1,750, because the pool grew with
the reference series while the two met parameters stayed on one station. The
side effect has therefore reversed direction, which is a reason not to have
rested anything on it. It changes the denominator; it does not change the
top-*k*-by-|r| cut that selects candidates, and it does not make two adjacent
beds against one station into independent evidence.

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
