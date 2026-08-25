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

Two properties to keep in mind when reading flags:

- The spike test judges a sample against the midpoint of its two neighbours, so
  a spike large enough to fail necessarily pushes both neighbours past the
  suspect threshold. One bad reading costs three rows from a `qc_flag <= 2`
  query, not one.
- A suspect flag *removes* data under the default filter. Thresholds are
  therefore set generously and are provisional, tuned against a single
  three-week deployment. Setting them tighter would preferentially remove the
  brief cold excursions §2 relies on as a nitrate proxy — the signal, not the
  noise.

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

Not built: it is blocked on the public-source fetchers (doc 02), since there is
no neighbor series to compare against until one exists. `sites.json` already
carries the `neighbor_refs` this will read.

## 2. Quarterly feature definitions

Computed per site × quarter from QC-filtered observations, then converted
to anomalies. Temperature family: mean, min (proxy for nitrate-bearing
upwelled water — in Southern California nitrate is high only in cold
water, so quarterly minimum temperature carries nutrient information),
max, 5th/95th percentiles, variance, days above 20 °C and 23 °C (giant
kelp stress thresholds; exact values to be finalized against literature
and treated as tunable), longest consecutive spell above 20 °C,
degree-days above 18 °C, days below 14 °C. Wave family (CDIP): count of
events with significant height above thresholds, longest event duration,
quarterly max height. Water level (CO-OPS): mean sea-level anomaly vs.
predictions. Covariates: marine heatwave days in quarter (Hobday et al.
definition computed on the long SST series with a fixed 1983–2012
baseline), ENSO state (ONI), BEUTI/CUTI quarterly means. Coverage rule:
quarters with under 60% valid data are excluded from feature use, mirroring
the missing-not-zero handling of kelp.

## 3. Climatology and anomalies

For every series (kelp and environmental), compute the quarterly
climatology — the long-run mean for each of Q1–Q4 — and work in anomalies
(or standardized anomalies) thereafter. This removes the seasonal cycle
that would otherwise dominate every correlation. The climatology baseline
period is fixed and recorded (proposed: 1984–2013 where available) so
anomalies don't shift as new data arrives. STL decomposition is available
as a cross-check on the simpler climatology subtraction.

## 4. Analysis ladder

Applied in order; each rung informs whether the next is warranted.

**4.1 Lagged cross-correlation screen.** Kelp anomaly at quarter t vs.
each environmental feature anomaly at t−0…4, per polygon × site pair.
Output is a lag–feature correlation matrix (the dashboard's "lag
explorer"). Purpose: recover the known physics (heat stress at short
lags, cold/nitrate association, wave removal in winter) and rank which
project-sensor features carry signal. Screening only — no significance
claims from this step.

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
