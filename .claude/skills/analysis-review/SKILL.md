---
name: analysis-review
description: Statistical guardrails for kelp-vs-environment analyses. Use when writing or reviewing notebooks, fitting or interpreting correlations, GLMs/GAMs, event studies, or changepoint analyses, when reporting significance or effect sizes, or when preparing figures/results for the research team.
---

# Analysis Review

Methods of record: docs/04-analysis-methods.md §4–6. The binding
constraint: ~100–150 usable quarterly kelp observations per polygon.

## Checklist before trusting any result

1. **Anomalies, not raw values.** If a correlation was computed on raw
   series, it's probably the seasonal cycle. Recompute on anomalies.
2. **Autocorrelation-honest inference.** Quarterly series are
   autocorrelated: use Newey–West/HAC errors or explicit AR terms. Naive
   OLS p-values on these series are overstated — flag them in review.
3. **Blocked cross-validation only.** Time-contiguous blocks; never
   shuffled K-fold on a time series.
4. **Collinearity trap.** Temperature and nitrate proxies are strongly
   anti-correlated by regional oceanography. Do not interpret a
   temperature coefficient as isolating thermal stress from nutrient
   limitation.
5. **Multiple comparisons.** The lag × feature × polygon screen (§4.1) is
   exploratory. Carry only pre-registered relationships (analysis README)
   into inferential models; report effect sizes with uncertainty, not
   p-value collections.
6. **Missing-data discipline.** Confirm null kelp quarters were excluded, not
   imputed or zeroed. Low-coverage environmental quarters are *flagged*
   `usable = false` in `quarterly_env`, never dropped from the table — so
   confirm the notebook actually filters on `usable`, and check
   `baseline_years` before trusting an `_anom` column.
7. **Lag sanity.** Kelp responds with a lag; a strong lag-0-only effect
   with nothing at lag 1–2 deserves suspicion (except wave removal, which
   is fast).
8. **Small-N modeling.** Keep predictor counts low (rule of thumb:
   ≥15–20 obs per predictor). Prefer event composites (§4.2) over complex
   models. ML models are deferred by decision — don't introduce them.

## Reproducibility requirements

Notebooks run top-to-bottom from tables of record in `features/` — more
than one is fine, and which ones is the notebook's business — with no side
reads of observations or raw. Figures for sharing carry the run manifest ID
in the caption. Seed anything stochastic.

## Interpretation limits (state them in write-ups)

Landsat canopy is surface expression only: urchin grazing, predator
dynamics, and subsurface condition are invisible to every dataset here, so
unexplained variance is expected and ecological. Cloud-gap missingness is
seasonally biased toward winter. Project-sensor records are short relative
to the kelp record: the sensor-vs-public-station comparison (§4.5) is
confined to the overlap window.
