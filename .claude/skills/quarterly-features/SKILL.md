---
name: quarterly-features
description: Definitions and rules for building quarterly environmental features and anomalies aligned to the Kelp Watch calendar. Use when writing or modifying src/kelpcompare/features/, when computing climatologies or anomalies, when handling missing kelp quarters or low-coverage sensor quarters, or when adding a new feature or parameter family.
---

# Quarterly Features

Authoritative math: docs/04-analysis-methods.md §2–3. Schema for outputs:
docs/03-data-model.md (`quarterly_env`, `quarterly_kelp`, `comparison`).

## Calendar and alignment

Kelp Watch quarters: Q1 Jan–Mar, Q2 Apr–Jun, Q3 Jul–Sep, Q4 Oct–Dec.
All features are computed per `site_id × parameter-family × year × quarter`
in UTC and joined to kelp per polygon at lags 0–4 quarters.

## Non-negotiable rules

1. Missing ≠ zero, both directions: a cloud-gapped kelp quarter is null;
   a sensor quarter with `pct_coverage < 0.60` (config: tunable) is flagged
   unusable, not imputed. Never `fillna(0)`.
2. Compute features only from QC-filtered rows (`qc_flag <= 2` default).
3. Climatology baseline is FIXED (1984–2013 where available; else the
   documented per-site baseline) and recorded in output metadata. Anomalies
   must not shift when new data arrives.
4. Every feature gets an `_anom` twin. Raw-value correlations against kelp
   are almost always seasonal-cycle artifacts — analysis uses anomalies.

## Temperature features (per quarter)

mean, min, max, p05, p95, variance, `days_above_20c`, `days_above_23c`,
`max_spell_above_20c_days` (longest consecutive run), 
`degree_days_above_18c`, `days_below_14c` (upwelling/nitrate proxy — in
Southern California nitrate is high only in cold water, so quarterly min
and cold-day counts carry nutrient information), `n_obs`, `pct_coverage`.
Thresholds (20/23/18/14 °C) are config values with doc-04 defaults, not
hardcoded literals.

"Days above X" = count of distinct UTC days whose daily max exceeds X.
Spell length = consecutive days meeting the condition.

## Other families

- Waves (CDIP): `n_events_hs_above_3m` (event = contiguous exceedance),
  `max_event_hours`, quarterly max Hs.
- Water level (CO-OPS): mean observed-minus-predicted anomaly.
- Covariates: marine heatwave days (Hobday definition, fixed 1983–2012
  SST baseline), ONI/ENSO state, BEUTI/CUTI quarterly means.

## Testing expectations

Feature functions are pure (DataFrame in, row out) and tested against
hand-computed fixtures, including: a quarter with a gap straddling a
threshold spell, a below-coverage quarter, and a DST-spanning quarter
(UTC math should make DST irrelevant — the test proves it).
