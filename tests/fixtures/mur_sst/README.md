# JPL MUR L4 SST reference payloads

Recorded once, by hand, so tests never reach the network (CLAUDE.md). The two
recorded files are **verbatim** NOAA CoastWatch `griddap` responses — the two
header lines followed by every cell-day the request returned. Nothing was
reformatted, re-spaced, or synthesised: a fixture that has been tidied stops
being evidence of what the server emits.

| File | Source URL | Retrieved | Kept |
|---|---|---|---|
| `del_mar_2020-07-01_03_excerpt.csv` | `…/griddap/jplMURSST41.csv?analysed_sst[(2020-07-01T09:00:00Z):1:(2020-07-03T09:00:00Z)][(32.9334):1:(32.9713)][(-117.2903):1:(-117.2587)]` | 2026-08-31 | all 60 rows (20 cells × 3 days) |
| `la_jolla_2020-07-01_03_excerpt.csv` | `…/griddap/jplMURSST41.csv?analysed_sst[(2020-07-01T09:00:00Z):1:(2020-07-03T09:00:00Z)][(32.7891):1:(32.8671)][(-117.3175):1:(-117.2574)]` | 2026-08-31 | all 189 rows (63 cells × 3 days) |
| `del_mar_edge-cases.csv` | **hand-built** | — | 60 rows on Del Mar's real grid |

Host: `https://coastwatch.pfeg.noaa.gov/erddap`. Both boxes are exactly what
`mur_sst.request_bounds` produces for the committed outline of that bed, so the
fixtures test the request this code actually makes rather than one shaped to
suit them.

## Why these two beds

They are the two failure modes, one each.

**`KELP:DEL-MAR` is the bed the obvious reduction produces nothing for.** Its
outline is narrower than the 1 km grid along its whole length, so **no MUR cell
centre falls inside it at all** — a centres-inside rule would have left one of
the six beds with no satellite leg. Five cells overlap it, and the area-weighted
mean over them is what this fixture pins.

**`KELP:LA-JOLLA`'s request box contains shoreline.** Twelve of its 63 cells are
land and arrive as `NaN`, which is what a mean over the *box* would have averaged
in. Its outline still reaches 96.3 % coverage, so the fixture also pins that the
land mask reduces coverage rather than the temperature.

Both were recorded for the same 2020-07-01…03 window so the two reductions are
directly comparable: 20.00 / 20.80 / 20.85 °C at Del Mar against 19.97 / 20.33 /
20.29 °C at La Jolla, 14 km apart. That difference is the whole reason MUR was
chosen over OISST, whose 0.25° grid hands both beds the same cell
(<https://github.com/cweber12/kelp-compare/issues/106>).

## Why the third file is hand-built

Three summer days do not contain the two cases the parser has to get right, and
waiting for a real payload that does would mean shipping the code untested
against them:

- **the declared `_FillValue` (`-7.768`) arriving as a number.** ERDDAP writes
  `NaN` in CSV, so this should never happen — but a `-7.768 °C` that reached the
  weighted mean would drag a bed's daily value by the fill's whole distance from
  the water, and QC runs *after* the reduction rather than before it.
- **a day the product covers nowhere over the bed.** The row stays in the record
  flagged missing rather than being dropped, because a dropped day is a hole the
  doc 04 §3 coverage arithmetic cannot see.

It uses **one value per water cell per day** on purpose. The expected mean is
then that value whatever the weights are, so the edited fixture cannot become
the thing that proves the weighting — the recorded ones do that. It sits on Del
Mar's real grid (5 latitudes × 4 longitudes, real land mask), so it is still a
payload this bed could receive.
