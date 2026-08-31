# NDBC reference payloads

Recorded once, by hand, so tests never reach the network (CLAUDE.md). All six
are **verbatim excerpts** of real payloads — the two header lines followed by
one contiguous window of data rows. Nothing was reformatted, re-spaced, or
synthesised: a fixture that has been tidied stops being evidence of what the
station emits.

Three stations, in two shapes. `LJAC1` is a shore station reporting the met
columns and no waves; `46254` and `46266` are nearshore Waveriders reporting
waves and water temperature and nothing else. Each has one archive excerpt and
one realtime excerpt, because those two layouts differ from each other in three
ways at once.

| File | Source URL | Retrieved | Kept |
|---|---|---|---|
| `ljac1h2023_excerpt.txt` | `https://www.ndbc.noaa.gov/data/historical/stdmet/ljac1h2023.txt.gz` | 2026-08-25 | rows 40736–41135 of 87 302 (2023-06-20 17:06 – 2023-06-22 11:24 UTC) |
| `LJAC1_realtime_excerpt.txt` | `https://www.ndbc.noaa.gov/data/realtime2/LJAC1.txt` | 2026-08-25 | rows 651–950 of 10 746 (2026-08-23 05:48 – 2026-08-21 23:54 UTC, newest first) |
| `46254h2015_excerpt.txt` | `https://www.ndbc.noaa.gov/data/historical/stdmet/46254h2015.txt.gz` | 2026-08-30 | rows 1–200 of 14 955 (2015-02-12 16:48 – 2015-02-16 20:18 UTC) |
| `46254_realtime_excerpt.txt` | `https://www.ndbc.noaa.gov/data/realtime2/46254.txt` | 2026-08-30 | rows 601–800 of 2163 (2026-08-18 13:56 – 2026-08-14 10:26 UTC, newest first) |
| `46266h2019_excerpt.txt` | `https://www.ndbc.noaa.gov/data/historical/stdmet/46266h2019.txt.gz` | 2026-08-30 | rows 1–200 of 1205 (2019-12-06 17:30 – 2019-12-10 21:30 UTC) |
| `46266_realtime_excerpt.txt` | `https://www.ndbc.noaa.gov/data/realtime2/46266.txt` | 2026-08-30 | rows 601–800 of 2170 (2026-08-18 17:26 – 2026-08-14 13:56 UTC, newest first) |

## Why these windows

The archive window brackets the **largest water-temperature step in the 2023
record** (2.9 °C) and contains five `999.0` water-temperature sentinels and
eighteen air-temperature ones. It is the case issue #4 is about and the case a
naive parse gets wrong, in one file.

The realtime window brackets a run of `MM` water temperatures, so the other
sentinel convention is covered too.

## The two nearshore Waveriders

`46254` (Scripps Nearshore) and `46266` (Del Mar Nearshore) are a different
*kind* of NDBC station from LJAC1, which is why they get fixtures of their own
rather than being assumed to look the same. They are wave buoys: of the
fourteen stdmet data columns they carry data in five — `WVHT DPD APD MWD WTMP`
— and every other column is sentinel in **every row of both layouts**. LJAC1 is
the opposite shape, reporting the met columns and no waves at all.

Their archive excerpts are taken from **row 1 of each station's first year**,
not from the middle, because the first row *is* the fact worth pinning: 46254's
record begins `2015 02 12 16 48` and 46266's begins `2019 12 06 17 30`. Those
two timestamps are why neither station can supply the 2007–2019 climatology
baseline, which is the whole of docs/04 §3's per-series window question and
ADR-007. A window chosen from the middle of the record would not show it.

Note that 46266's 2019 file holds 1205 rows covering 6–31 December — a
part-month, not a year. Read as "the record starts in 2019" it suggests one
baseline year exists; it does not.

## Station metadata

From the NDBC station pages, retrieved on the dates above — the source for the
depths recorded in `sites.json`. For LJAC1:

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

Both Waveriders report the same sensor depth as each other, and it is not
LJAC1's:

    Station 46254 - SCRIPPS Nearshore, CA (201)
    Sea temp depth: 0.46 m below water line     Water depth: 46 m

    Station 46266 - Del Mar Nearshore, CA (153)
    Sea temp depth: 0.46 m below water line     Water depth: 17 m

0.46 m is a surface measurement; LJAC1's intake is at 3.4 m and the project
loggers sit at 8.23 m and 16.76 m. That gap is why `sensor_depths_m` is per
parameter rather than per site.

## Refreshing

Realtime holds only ~45 days, so `LJAC1_realtime_excerpt.txt` names timestamps
that are no longer retrievable. That is intended: a fixture is a record of a
payload, not a window into the live feed. Re-record only if the *layout* changes,
and update the assertions in `tests/test_fixtures_ndbc.py` in the same commit.
