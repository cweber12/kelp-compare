# SIO Shore Stations reference files

Downloaded by hand from the UCSD Library archive, so tests never reach the
network (CLAUDE.md). The source cannot be pulled at all — Google Form
registration plus Anubis proof-of-work bot protection — so there is no network
seam to stub even if tests were allowed one (doc 02).

| File | Snapshot | Preamble | Encoding | Data rows | Bytes |
|---|---|---|---|---|---|
| `lajolla_temp_excerpt.csv` | archived 2026-06-30, the pinned one | 46 lines | UTF-8 + BOM | 33 | 4,439 |
| `lajolla_temp_2020_archive_excerpt.csv` | archived 2022-07-07 | **45 lines** | **Mac Roman, no BOM** | 9 | 3,481 |
| `lajolla_temp_edge-cases.csv` | hand-built on the pinned preamble | 46 lines | UTF-8 + BOM | 7 | 3,440 |

## Why these are excerpts, and Kelp Watch's are not

The Kelp Watch fixtures next door are whole files, on the grounds that a
40-year quarterly export is a few kilobytes and trimming one only destroys
evidence. This record is 40,034 daily rows and about 1.6 MB per snapshot, and
committing fourteen of them would put 20 MB of redistributed archive in a public
repository for no test that needs it.

So these are excerpts, and every one keeps its **preamble byte for byte** —
which is where all the format risk lives: the archive date the pin is checked
against, the position, the two nominal depths, the flag legend, and the
timezone statements. The rows are real rows, unedited, chosen to carry one case
each.

## `lajolla_temp_excerpt.csv` — the pinned snapshot

33 real rows from the 2026-06-30 archive, in file order, covering:

| Rows | What they pin |
|---|---|
| 1916-08-22 … 08-31 | the first ten days: surface only, **no bottom flag at all** |
| 1926-07-19 … 07-23 | the bottom series beginning on 1926-07-21 |
| 1930-01-07, 1926-08-15 | an absent reading carrying flag `0` — surface and bottom |
| 1990-01-01 … 01-03 | the first days that carry a `TIME_PST` |
| 1990-08-06 | a post-1990 day with **no** time and `TIME_FLAG = 0` |
| 1990-09-11 | a sample after 16:00 PST, which crosses the UTC day |
| 2020-05-07 | the earliest sample in the record, 05:24 |
| 1992-11-05, 1992-04-15, 1993-08-30 | `TIME_FLAG` 1, 2, 3 |
| 2005-08-28, 1996-01-15, 1996-02-04 | `SURF_FLAG` 1, 2, 3 |
| 1997-07-19, 1996-01-15, 1996-02-04 | `BOT_FLAG` 1, 2, 3 |
| 2026-03-29 … 03-31 | the last three days of the pinned archive |

## `lajolla_temp_2020_archive_excerpt.csv` — a second snapshot, on purpose

Not redundant. It is the *format* that differs between archives, and this one
carries both of the differences that would break a parser written against the
newest file alone: a **45-line preamble**, so a fixed skip reads the column
header as data, and **Mac Roman with no byte-order mark**, so a strict UTF-8
decode raises on the degree sign in the position line (byte `0xA1` here,
`0xC2 0xB0` in the newer files).

It also declares a different archive date and a different funding award, which
is what the archive pin exists to catch: dropped against the pinned registry it
must be quarantined, not landed.

## `lajolla_temp_edge-cases.csv` — the cases the source has never emitted

Hand-built, and separate for the reason the HOBO and RTOMS pairs are separate:
a fixture edited to contain an edge case can no longer prove what the source
actually sends, so it must not be the only one.

Every row here is a case the real archive does not contain — verified across all
fourteen snapshots, where the only data flags ever written are `0, 1, 2, 3`:

| Row | Case |
|---|---|
| 2024-01-02 | a plain good day, so the others have a baseline |
| 2024-01-03 | `SURF_FLAG = 5`, "Pier Chlorophyll Program or at different location" |
| 2024-01-04 | `BOT_FLAG = 5`, the same on the other series |
| 2024-01-05 | `SURF_FLAG = 4`, "leaky bottle" — declared in the legend, salinity-only in practice |
| 2024-01-06 | a reading with **no flag beside it** |
| 2024-01-07 | `TIME_PST = 0`, midnight, the one time-of-day `HHMM` cannot spell with four digits |
| *(last)* | a trailing all-comma filler row, as five of the fourteen snapshots carry |

Its preamble is the pinned snapshot's, unmodified, so it passes the archive pin
and the tests are about the rows.

## Citation

Required wherever these data appear, in the pinned snapshot's own words:

> Carter, Melissa L.; Flick, Reinhard E.; Terrill, Eric; Beckhaus, Elena C.;
> Martin, Kayla; Fey, Connie L.; Walker, Patricia W.; Largier, John L.;
> McGowan, John A. (2022). Shore Stations Program — La Jolla, Scripps Pier
> (La Jolla Archive, 2026-06-30). In *Shore Stations Program Data Archive:
> Current and Historical Coastal Ocean Temperature and Salinity Measurements
> from California Stations*. UC San Diego Library Digital Collections.
> `https://doi.org/10.6075/J06T0K0M`
>
> Funding for the Shore Stations Program provided by the California Department
> of Parks and Recreation, Natural Resources Division, Award# C22820005.

The 2020 excerpt is from an earlier archive and names Award# `C1670003`, which
is why the citation is pinned per archive rather than per program (doc 03).
