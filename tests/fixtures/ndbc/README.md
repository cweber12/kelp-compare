# NDBC reference payloads

Recorded once, by hand, so tests never reach the network (CLAUDE.md). Both are
**verbatim excerpts** of real payloads from station LJAC1: the two header lines
followed by one contiguous window of data rows. Nothing was reformatted,
re-spaced, or synthesised — a fixture that has been tidied stops being evidence
of what the station emits.

| File | Source URL | Retrieved | Kept |
|---|---|---|---|
| `ljac1h2023_excerpt.txt` | `https://www.ndbc.noaa.gov/data/historical/stdmet/ljac1h2023.txt.gz` | 2026-08-25 | rows 40736–41135 of 87 302 (2023-06-20 17:06 – 2023-06-22 11:24 UTC) |
| `LJAC1_realtime_excerpt.txt` | `https://www.ndbc.noaa.gov/data/realtime2/LJAC1.txt` | 2026-08-25 | rows 651–950 of 10 746 (2026-08-23 05:48 – 2026-08-21 23:54 UTC, newest first) |

## Why these windows

The archive window brackets the **largest water-temperature step in the 2023
record** (2.9 °C) and contains five `999.0` water-temperature sentinels and
eighteen air-temperature ones. It is the case issue #4 is about and the case a
naive parse gets wrong, in one file.

The realtime window brackets a run of `MM` water temperatures, so the other
sentinel convention is covered too.

## Station metadata

From `https://www.ndbc.noaa.gov/station_page.php?station=LJAC1`, retrieved
2026-08-25 — the source for the depth recorded in `sites.json`:

    Station LJAC1 - 9410230 - La Jolla, CA
    Owned and maintained by NOAA's National Ocean Service
    Water Level Observation Network
    32.867 N 117.257 W
    Site elevation: 9.3 m above mean sea level
    Air temp height: 7.2 m above site elevation
    Anemometer height: 8.2 m above site elevation
    Barometer elevation: 11.3 m above mean sea level
    Sea temp depth: 3.4 m below MLLW

Note the station title: LJAC1 **is** CO-OPS 9410230. NDBC redistributes the NOS
platform's observations; the two are not independent stations.

## Refreshing

Realtime holds only ~45 days, so `LJAC1_realtime_excerpt.txt` names timestamps
that are no longer retrievable. That is intended: a fixture is a record of a
payload, not a window into the live feed. Re-record only if the *layout* changes,
and update the assertions in `tests/test_fixtures_ndbc.py` in the same commit.
