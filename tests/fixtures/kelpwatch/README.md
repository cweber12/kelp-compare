# Kelp Watch reference exports

Downloaded by hand from kelpwatch.org, so tests never reach the network
(CLAUDE.md). Both are **whole files, byte for byte** — not excerpts. The export
is a few kilobytes for a forty-year record, so there is nothing to gain by
trimming one and everything to lose: a fixture that has been tidied stops being
evidence of what the site emits.

| File | Selected geometry | Retrieved | Bytes | Rows |
|---|---|---|---|---|
| `kelp_lajolla.csv` | La Jolla kelp bed | 2026-08-26 | 5,889 | 212 |
| `kelp_delmar.csv` | Del Mar kelp bed | 2026-08-26 | 4,435 | 212 |

Both cover 1984Q1 – 2026Q2 and carry a per-year `max` row. The export writes
LF line endings, a single header line, no preamble, and **no identifier for the
geometry it describes** — which is why `polygons.geojson` has to say which file
belongs to which polygon.

Source dataset, from the site's own recommended-citation download on the same
day: Bell, T., K. Cavanaugh, D. Siegel. 2024. *SBC LTER: Time series of
quarterly NetCDF files of kelp biomass in the canopy from Landsat 5, 7 and 8,
since 1984 (ongoing)* **ver 23**. Environmental Data Initiative.
`https://doi.org/10.6073/pasta/2c1218b7ebe6967da52000adf02f6a8b`.

## Why these two

They are the same forty years over two beds of very different size, which is
what makes the "no kelp" versus "no observation" distinction visible in one
pair rather than assertable:

| | La Jolla | Del Mar |
|---|---|---|
| historic footprint | 8,309 cells | 130 cells |
| quarters with **no** cloud-free observation | 7 | 8 |
| quarters observed, showing **zero** kelp | 13 | 112 |
| partially observed quarters | 12 | 2 |

**Del Mar is the case that matters.** It is a marginal bed that is genuinely
empty for 112 of its 170 quarters, and unobservable for 8 more. The export
writes `0` for all 120 of them. Only `count_cells_no_clouds` tells the two
apart, and getting it wrong puts 8 fabricated zeros into the response variable
— in a bed where zero is the normal reading, so nothing downstream would look
wrong.

La Jolla supplies the partial-coverage cases the coverage floor is about,
including 1990Q4 at 979 of 8,309 cells (11.8%) and 1984Q4 at 5,426 (65.3%),
either side of the 0.6 floor. Del Mar adds 1996Q1 at 3 of 130 cells (2.3%) —
observed, so not missing, but not a quarter to believe.

## Refreshing

Unlike the NDBC realtime feed, these are re-retrievable: the site serves the
whole record on every export. Re-record when the **layout** changes, or when
moving to a newer dataset revision, and update the assertions in
`tests/test_fixtures_kelpwatch.py` and the pinned revision in
`data/registry/polygons.geojson` in the same commit.

Note that a newer revision may revise history as well as extend it — the
upstream product recalibrates sensors and refills scan-line gaps — so a
re-record is a change to numbers already published, not only an append.
