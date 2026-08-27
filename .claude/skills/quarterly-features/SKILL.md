---
name: quarterly-features
description: Definitions and rules for building quarterly environmental features and anomalies aligned to the Kelp Watch calendar. Use when writing or modifying src/kelpcompare/features/, when computing climatologies or anomalies, when handling missing kelp quarters or low-coverage sensor quarters, or when adding a new feature or parameter family.
---

# Quarterly Features

Authoritative math: docs/04-analysis-methods.md §2–3. Schema for outputs:
docs/03-data-model.md (`quarterly_env`, `climatology_env`). Where the
configuration lives and why: ADR-006.

Built: `kelpcompare features` produces all five docs/03 tables --
`quarterly_env`, `climatology_env`, `quarterly_kelp`, `climatology_kelp` and
`comparison`.

The climatology is generic over its series key: the caller passes which columns
identify one series and which are measured features, so kelp (`polygon_id`) and
environment (`source, site_id, parameter, depth_m`) share one implementation and
cannot drift apart. Passing the wrong key for a table raises rather than coming
back empty.

`quarterly_kelp` is built from the Kelp Watch landings plus `polygons.geojson`,
never through `observations` -- a canopy value belongs to a polygon and that
zone is keyed on `site_id`. `comparison` is regenerated wholesale from the two
quarterly tables as they stand on disk.

Deferred: the published SBC LTER data package as a second route (giant vs bull
kelp, biomass, per-pixel), blocked on an EDI account -- issue #25.

## Calendar and alignment

Kelp Watch quarters: Q1 Jan–Mar, Q2 Apr–Jun, Q3 Jul–Sep, Q4 Oct–Dec.

Row key: **`source × site_id × parameter × depth_m × year × quarter`** — the
QC series key plus time, so every feature row traces to one QC series. Not
`parameter-family`: no family vocabulary exists in any registry file. Not
without `depth_m`: that would average a shallow and a deep logger across a
thermocline, corrupting exactly the quarterly minimum and cold-day counts.

Quarters are assigned in **UTC**. A 5pm 31 December reading on the US west
coast falls in the following Q1. DST is irrelevant rather than handled.

## Non-negotiable rules

1. Missing ≠ zero, both directions: a cloud-gapped kelp quarter is null; a
   sensor quarter below the configured coverage floor (default 0.60) is
   flagged `usable = false`, not imputed and not dropped. Never `fillna(0)`.
   **The Kelp Watch export writes `0` for an unobserved quarter**, so only
   `count_cells_no_clouds == 0` tells a cloud gap from an empty bed (docs/02);
   the parser applies that, and by the time a quarter reaches the table it
   carries null.
2. Compute features only from QC-filtered rows (`qc_flag <= 2` default,
   overridable per run and recorded in `qc_max_flag`). Coverage counts the
   same filtered rows, so a quarter that failed QC on every row scores
   **zero** coverage, not full.
3. Climatology baseline is FIXED — **2007–2019, minimum 10 usable years** —
   and written to its own table with its window, year count, mean and
   standard deviation. Anomalies must not shift when new data arrives.
   Only usable *and complete* quarters contribute.
4. Every measured feature gets an `_anom` twin. Bookkeeping columns
   (`n_obs`, `cadence_s`, `pct_coverage`, …) and the spell gap markers do
   not. Raw-value correlations against kelp are almost always seasonal-cycle
   artifacts.
5. Thresholds, the floor and the baseline are registry values in
   `data/registry/features.json`, never literals in code (ADR-006). Column
   names are derived from the configured thresholds, so a retune renames the
   column rather than redefining it.

## Temperature features (per quarter)

`mean`, `min`, `max`, `p05`, `p95`, `variance` (universal `statistics` set),
plus `days_above_20c`, `days_above_23c`, `max_spell_above_20c_days`,
`max_spell_above_20c_gap_interrupted`, `degree_days_above_18c`,
`days_below_14c` (upwelling/nitrate proxy — in Southern California nitrate is
high only in cold water, so quarterly min and cold-day counts carry nutrient
information).

Exact definitions, all day-based, every day with ≥1 observation counting:

- "Days above X" = distinct UTC days whose daily **max** exceeds X.
- "Days below X" = distinct UTC days whose daily **min** falls below X.
- Degree-days above X = Σ over observed days of `max(0, daily_mean − X)`,
  in °C·day. Never a time integral: that would weight by sample spacing.
- Spell = consecutive **calendar** days meeting the condition. **Broken by an
  unobserved day, never bridged**, and marked `_gap_interrupted` when a gap
  ended it, so a floor is not reported as a measurement.
- Percentiles interpolate linearly; variance is `ddof=1`, so one observation
  gives a null variance, not zero.

Bookkeeping beside them: `n_obs`, `n_days_observed`, `cadence_s`,
`expected_obs`, `pct_coverage`, `usable`, `quarter_complete`, `qc_max_flag`,
`baseline_years`, `feature_set`.

`pct_coverage = n_obs / expected_obs`, where `expected_obs` is the quarter's
duration over the series' **median observed inter-sample interval** — so an
hourly station and a 10-minute logger are judged on the same scale. Clamped
at 1 with a manifest warning when a cadence changed mid-quarter.
`quarter_complete` separates an unfinished quarter from a station outage.

## Other families

Not implemented; the configuration parser refuses to declare them until the
fetchers exist.

- Waves (CDIP): `n_events_hs_above_3m` (event = contiguous exceedance),
  `max_event_hours`, quarterly max Hs.
- Water level (CO-OPS): mean observed-minus-predicted anomaly.
- Covariates: marine heatwave days (Hobday definition, fixed 1983–2012 SST
  baseline), ONI/ENSO state, BEUTI/CUTI quarterly means.

A parameter that has a fetcher but no ecological feature set gets
`statistics` — that is what `air_temperature`, `wind_speed`, `water_level`
and the wave parameters have today.

## Known biases to state, not discover

- Day counts run **low** under partial coverage: a day observed only
  overnight cannot show its daytime maximum.
- Spell lengths are **floors** wherever `_gap_interrupted` is true.
- The 2007–2019 baseline **contains the 2014–2016 marine heatwave**, so the
  warm-season baseline is raised and later warm anomalies are damped.

## Testing expectations

Feature functions are pure (DataFrame in, DataFrame out) and tested against
hand-computed frames whose values a reviewer can check by arithmetic; the CLI
suite drives the same code end to end from the recorded fixtures. Cases the
suite must keep: a quarter whose gap straddles a threshold spell (broken and
marked); a below-coverage quarter (computed and flagged, not dropped); a
DST-spanning quarter (UTC makes it irrelevant — the test proves that); a
quarter where every row failed QC (zero coverage); a one-observation quarter
(null cadence, zero coverage, null variance); observations either side of a
UTC quarter boundary; two runs producing identical bytes; a year appended
outside the baseline moving no existing anomaly.

## The kelp half

Row key: **`polygon_id × year × quarter`**. Two measured quantities, both with
`_anom` twins: `kelp_area_m2` (how much canopy) and `n_cells_kelp` (how far it
spread). No species split and no biomass — the UI export carries neither.

Coverage is spatial: `pct_cells_observed = n_cells_observed / n_cells`, the
cloud-free 30 m cells over the bed's historic footprint. Same floor as the
environmental half, shared rather than duplicated.

**Disclose, do not correct: a partially observed kelp quarter is biased low.**
`kelp_area_m2` is a sum over the cells that were seen, so two thirds of a bed
under cloud reports about two thirds of the canopy. Scaling by the observed
fraction is imputation and is rejected. Flag it unusable and keep the value.

Cloud gaps are seasonally biased — 9.1% of Q4 and 5.8% of Q1 unobserved against
0.8% of Q3, across the six exported beds — so dropping nulls drops winter.

## The comparison table

One row per `polygon_id × env_source × site_id × parameter × depth_m × year ×
quarter × lag`, lags 0–4. Earlier drafts keyed it without parameter and depth;
that cannot represent a site with several parameters.

- **The environment leads, kelp responds.** Lag 2 on a 2015Q3 row is kelp in
  2015Q3 against water in 2015Q1. `env_year`/`env_quarter` are on the row.
- **Lag 0 is included**; omitting it would make its absence look like a result.
- **Nothing is filtered.** Rows survive where either side is unusable or where
  the lag reaches before the environmental record. `usable` is the single gate.
- **Pairs come from `polygons.geojson`**, never from name matching.
