"""`kelpcompare ingest --source ndbc`, end to end (docs/02, docs/03).

The real command against a `tmp_path` data root, with the network replaced at
the one seam that touches it -- `fetch_realtime` / `fetch_archive`. Everything
below that, the landing, the parse, the write and the manifest, is the code an
operator runs. Nothing here reaches NOAA, per CLAUDE.md.
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.fetchers import ndbc
from kelpcompare.fetchers.base import NotModified, SourceUnavailable, new_payload
from kelpcompare.storage import Zones, read_observations

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures" / "ndbc"
ARCHIVE = FIX / "ljac1h2023_excerpt.txt"
REALTIME = FIX / "LJAC1_realtime_excerpt.txt"
REGISTRY_SOURCE = REPO_ROOT / "data" / "registry"


@pytest.fixture
def data_root(tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "registry").mkdir(parents=True)
    for name in ("sites.json", "parameters.json"):
        shutil.copy2(REGISTRY_SOURCE / name, root / "registry" / name)
    return root


@pytest.fixture
def offline(monkeypatch):
    """Serve the recorded payloads in place of the network, and record the asks."""
    asked: list[tuple[str, int | None]] = []

    def realtime(station, *, session=None, validators=None):
        asked.append((station, None))
        return new_payload(
            "ndbc",
            station.upper(),
            f"{station.upper()}.txt",
            "file://realtime",
            REALTIME.read_bytes(),
        )

    def archive(station, year, *, session=None, validators=None):
        asked.append((station, year))
        return new_payload(
            "ndbc",
            station.upper(),
            f"{station.lower()}h{year}.txt.gz",
            f"file://{year}",
            # mtime=0 so two calls return identical bytes. A completed year is a
            # static file on NDBC's server; a stub whose bytes changed per call
            # would be modelling something the source does not do, and would
            # quietly defeat the content-addressed landing below.
            gzip.compress(ARCHIVE.read_bytes(), mtime=0),
        )

    monkeypatch.setattr(ndbc, "fetch_realtime", realtime)
    monkeypatch.setattr(ndbc, "fetch_archive", archive)
    return asked


def run(data_root: Path, *extra: str):
    result = CliRunner().invoke(
        main, ["ingest", "--source", "ndbc", "--data-root", str(data_root), *extra]
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def manifest(data_root: Path) -> dict:
    files = sorted((data_root / "raw" / "_manifests").glob("*.json"))
    assert len(files) == 1, f"expected one manifest, found {len(files)}"
    return json.loads(files[0].read_text())


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_realtime_ingest_lands_raw_and_writes_observations(data_root, offline):
    result = run(data_root)

    assert result.exit_code == 0
    assert offline == [("LJAC1", None)]  # every NDBC station the registry declares

    landed = list((data_root / "raw" / "ndbc" / "LJAC1").iterdir())
    assert len(landed) == 1
    assert landed[0].read_bytes() == REALTIME.read_bytes()  # untouched, hard rule 1

    stored = read_observations(Zones.at(data_root), "ndbc")
    assert len(stored) == 300 * 3  # the three parameters LJAC1 declares, not five columns
    assert set(stored["site_id"]) == {"NDBC:LJAC1"}


def test_the_registry_depth_reaches_the_stored_rows(data_root, offline):
    """docs/02: the 3.4 m intake is recorded so a depth mismatch cannot hide."""
    run(data_root)
    stored = read_observations(Zones.at(data_root), "ndbc")

    water = stored[stored["parameter"] == "sea_water_temperature"]
    assert set(water["depth_m"]) == {3.4}
    assert stored[stored["parameter"] == "wind_speed"]["depth_m"].isna().all()


def test_an_archive_year_partitions_by_that_year(data_root, offline):
    result = run(data_root, "--year", "2023")

    assert result.exit_code == 0
    assert offline == [("LJAC1", 2023)]
    assert (data_root / "observations" / "source=ndbc" / "year=2023").is_dir()

    stored = read_observations(Zones.at(data_root), "ndbc")
    assert stored["timestamp"].dt.year.unique().tolist() == [2023]


def test_several_years_are_separate_windows(data_root, offline):
    run(data_root, "--year", "2023", "--year", "2024")

    assert offline == [("LJAC1", 2023), ("LJAC1", 2024)]
    assert len(manifest(data_root)["files"]) == 2


def test_re_ingesting_the_same_payload_does_not_double_the_rows(data_root, offline):
    """docs/03: the partition dedupes on (site, parameter, timestamp, depth)."""
    run(data_root, "--year", "2023")
    before = len(read_observations(Zones.at(data_root), "ndbc"))

    run(data_root, "--year", "2023")
    after = read_observations(Zones.at(data_root), "ndbc")

    assert len(after) == before
    landed = list((data_root / "raw" / "ndbc" / "LJAC1").iterdir())
    assert len(landed) == 1  # content-addressed: the same bytes land once


def test_qc_runs_over_the_ingested_station(data_root, offline):
    """The point of a second source: it goes through the stages already built.

    The QARTOD stage was written against one HOBO deployment. Here it evaluates
    a public station it has never seen, off the same two columns, with no code
    of its own for the new source -- which is the claim the docs/03 schema makes
    and the reason a second source is worth landing before any features are.
    """
    run(data_root, "--year", "2023")

    result = CliRunner().invoke(main, ["qc", "--source", "ndbc", "--data-root", str(data_root)])
    assert result.exit_code == 0  # no warnings: nothing went unevaluated

    water = read_observations(Zones.at(data_root), "ndbc").pipe(
        lambda df: df[df["parameter"] == "sea_water_temperature"]
    )
    verdicts = water["qc_tests"].value_counts().to_dict()

    # 390 rows get all three tests. The remaining 5 in-water rows sit beside a
    # gap, and which tests reach a verdict there depends on which side they are:
    # the spike test needs a pair of neighbours, the rate test needs a
    # predecessor that is a measurement (docs/03).
    assert verdicts["gross_range:pass;spike:pass;rate_of_change:pass"] == 390
    assert verdicts["gross_range:pass"] == 3  # resuming after a gap, and row 0
    assert verdicts["gross_range:pass;rate_of_change:pass"] == 2  # running into one

    # The sentinel rows are judged as missing by every test rather than passed:
    # there is nothing in an absent value to evaluate, and missing outranks all.
    assert verdicts["gross_range:missing;spike:missing;rate_of_change:missing"] == 5
    assert water["qc_flag"].value_counts().to_dict() == {1: 395, 9: 5}


def test_a_parameter_the_station_has_no_sensor_for_lands_no_rows(data_root, offline):
    """LJAC1 is a shore station with no wave sensor, and `sites.json` says so.

    The stdmet format has fixed columns, so WVHT and DPD arrive in every file
    holding the sentinel. Storing them was landing millions of rows that say
    nothing (issue #21). "Nobody asked this station for wave height" is now
    recorded in the registry, which is a better home for it than a row.
    """
    run(data_root, "--year", "2023")
    stored = read_observations(Zones.at(data_root), "ndbc")

    assert set(stored["parameter"]) == {
        "sea_water_temperature",
        "air_temperature",
        "wind_speed",
    }
    assert stored[stored["parameter"].isin(["wave_significant_height", "wave_peak_period"])].empty


def test_a_declared_sensor_reporting_nothing_still_lands_its_missing_rows(data_root, offline):
    """The distinction the registry exists to hold: an outage stays in the record.

    Air temperature is declared and the 2023 file holds sentinel for some of it,
    so those rows land flagged missing rather than vanishing the way the wave
    columns now do.
    """
    run(data_root, "--year", "2023")
    stored = read_observations(Zones.at(data_root), "ndbc")

    air = stored[stored["parameter"] == "air_temperature"]
    assert len(air) == 400
    assert air["value"].isna().any()
    assert set(air.loc[air["value"].isna(), "qc_flag"]) == {9}


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------


def test_the_manifest_records_the_fetch_not_an_adapter(data_root, offline):
    run(data_root, "--year", "2023")
    entry = manifest(data_root)["files"][0]

    assert entry["outcome"] == "ingested"
    assert entry["fetcher"] == "ndbc"
    assert entry["adapter"] is None  # exactly one of the two is ever set
    assert entry["site_id"] == "NDBC:LJAC1"
    assert entry["rows_in"] == 400
    assert entry["rows_out"] == 400 * 3
    assert entry["landed"].endswith("ljac1h2023.txt.gz")


def test_the_manifest_carries_the_flag_histogram(data_root, offline):
    run(data_root, "--year", "2023")
    payload = manifest(data_root)

    # 2 = not evaluated, 9 = missing. Both are docs/03 ingest-time states.
    assert payload["qc_flags"]["2"] > 0
    assert payload["qc_flags"]["9"] > 0
    assert sum(payload["qc_flags"].values()) == 400 * 3


# --------------------------------------------------------------------------
# Failure, told apart
# --------------------------------------------------------------------------


def test_an_outage_is_a_gap_and_does_not_fail_the_run(data_root, monkeypatch):
    """docs/01 §5: a missing NDBC year must never block the rest of a run."""

    def down(station, year, *, session=None, validators=None):
        raise SourceUnavailable(f"https://example.invalid/{year}: HTTP 503")

    monkeypatch.setattr(ndbc, "fetch_archive", down)

    result = run(data_root, "--year", "2019")

    assert result.exit_code == 0
    payload = manifest(data_root)
    assert payload["files"][0]["outcome"] == "skipped"
    assert any("HTTP 503" in gap for gap in payload["gaps"])
    assert not (data_root / "observations").exists()


def test_a_payload_that_arrives_and_will_not_parse_fails_the_run(data_root, monkeypatch):
    """A format change is not an outage; it needs a human, so it sets the code."""

    def garbage(station, year, *, session=None, validators=None):
        return new_payload("ndbc", station, "x.txt.gz", "file://x", b"not an NDBC file\n")

    monkeypatch.setattr(ndbc, "fetch_archive", garbage)

    result = run(data_root, "--year", "2023")

    assert result.exit_code == 1
    entry = manifest(data_root)["files"][0]
    assert entry["outcome"] == "failed"
    assert "two '#' header lines" in entry["reason"]


def test_a_payload_that_will_not_parse_is_still_landed(data_root, monkeypatch):
    """Landed before parsed: realtime holds ~45 days, so today's bytes are it."""

    def garbage(station, *, session=None, validators=None):
        return new_payload("ndbc", station, "LJAC1.txt", "file://x", b"not an NDBC file\n")

    monkeypatch.setattr(ndbc, "fetch_realtime", garbage)

    run(data_root)

    landed = list((data_root / "raw" / "ndbc" / "LJAC1").iterdir())
    assert [p.read_bytes() for p in landed] == [b"not an NDBC file\n"]


# --------------------------------------------------------------------------
# Selecting what to fetch
# --------------------------------------------------------------------------


def test_a_station_can_be_named_by_code_or_by_site_id(data_root, offline):
    run(data_root, "--station", "ljac1")
    run(data_root, "--station", "NDBC:LJAC1")

    assert offline == [("LJAC1", None), ("LJAC1", None)]


def test_a_station_the_registry_does_not_declare_is_refused(data_root, offline):
    result = run(data_root, "--station", "NOPE1")

    assert result.exit_code != 0
    assert "registry declares" in str(result.exception)
    assert offline == []  # nothing was asked of NOAA


def test_dry_run_writes_nothing_at_all(data_root, offline):
    result = run(data_root, "--year", "2023", "--dry-run")

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert not (data_root / "observations").exists()
    assert not (data_root / "raw" / "ndbc").exists()
    assert not (data_root / "raw" / "_manifests").exists()


def test_path_is_refused_for_a_pulled_source(data_root, offline, tmp_path):
    result = run(data_root, "--path", str(tmp_path))

    assert result.exit_code != 0
    assert "does not apply" in str(result.exception)


def test_year_is_refused_for_a_file_drop_source(data_root):
    result = CliRunner().invoke(
        main,
        ["ingest", "--source", "project", "--data-root", str(data_root), "--year", "2023"],
    )

    assert result.exit_code != 0
    assert "do not apply" in str(result.exception)


# --------------------------------------------------------------------------
# A re-run asks instead of downloading
# --------------------------------------------------------------------------


def conditional(monkeypatch, *, etag: str = '"v1"', unchanged_for: str | None = None):
    """Serve the recorded archive, honouring a conditional request.

    `unchanged_for` is the validator the fake server considers current; a
    request carrying it gets `NotModified`, anything else gets the file. That is
    the whole of what NDBC does, and it is the only way to exercise it offline.
    """
    asked: list[dict | None] = []

    def archive(station, year, *, session=None, validators=None):
        asked.append(dict(validators) if validators is not None else None)
        if unchanged_for is not None and (validators or {}).get("etag") == unchanged_for:
            raise NotModified(f"file://{year}: HTTP 304")
        return new_payload(
            "ndbc",
            station.upper(),
            f"{station.lower()}h{year}.txt.gz",
            f"https://example.invalid/{station.lower()}h{year}.txt.gz",
            gzip.compress(ARCHIVE.read_bytes(), mtime=0),
            etag=etag,
            last_modified="Tue, 16 Feb 2016 16:31:39 GMT",
        )

    monkeypatch.setattr(ndbc, "fetch_archive", archive)
    return asked


def latest_manifest(data_root: Path) -> dict:
    """The most recent run's manifest, for cases that run the command twice.

    `manifest` insists on exactly one, which is the right assertion for a
    single-run test and the wrong one here -- run ids sort chronologically by
    construction, so the last is the newest.
    """
    files = sorted((data_root / "raw" / "_manifests").glob("*.json"))
    return json.loads(files[-1].read_text())


def validators(data_root: Path) -> dict:
    path = data_root / "cache" / "http-validators.json"
    return json.loads(path.read_text())["urls"] if path.exists() else {}


def test_a_successful_ingest_records_what_the_server_called_this_version(data_root, monkeypatch):
    conditional(monkeypatch)
    run(data_root, "--year", "2023")

    (entry,) = validators(data_root).values()
    assert entry["etag"] == '"v1"'
    assert entry["last_modified"] == "Tue, 16 Feb 2016 16:31:39 GMT"


def test_the_second_run_asks_conditionally_and_is_told_nothing_changed(data_root, monkeypatch):
    asked = conditional(monkeypatch, unchanged_for='"v1"')

    run(data_root, "--year", "2023")
    result = run(data_root, "--year", "2023")

    assert asked == [{}, {"etag": '"v1"', "last_modified": "Tue, 16 Feb 2016 16:31:39 GMT"}]
    assert result.exit_code == 0
    assert latest_manifest(data_root)["files"][0]["outcome"] == "unchanged"
    assert "unchanged" in result.output


def test_an_unchanged_window_writes_no_landing_and_no_rows(data_root, monkeypatch):
    """The bytes are already in raw/ and the rows are already in the zone, so
    there is nothing left to do. Re-parsing landed bytes is `rebuild`'s job."""
    conditional(monkeypatch, unchanged_for='"v1"')
    run(data_root, "--year", "2023")
    before = len(read_observations(Zones.at(data_root), "ndbc"))
    landed = sorted(p.name for p in (data_root / "raw" / "ndbc" / "LJAC1").iterdir())

    run(data_root, "--year", "2023")

    assert len(read_observations(Zones.at(data_root), "ndbc")) == before
    assert sorted(p.name for p in (data_root / "raw" / "ndbc" / "LJAC1").iterdir()) == landed


def test_an_unchanged_window_is_not_a_gap(data_root, monkeypatch):
    """`skipped` means a hole in the record and notes one; this means the record
    is complete. A phantom gap on every re-run would make the field useless."""
    conditional(monkeypatch, unchanged_for='"v1"')
    run(data_root, "--year", "2023")
    run(data_root, "--year", "2023")

    payload = latest_manifest(data_root)
    assert payload["gaps"] == []
    assert payload["counts"] == {"unchanged": 1}


def test_a_revised_file_upstream_is_still_picked_up(data_root, monkeypatch):
    """The property that makes this safe. NDBC does re-issue an archive year
    after QC, and a mechanism that could mask that would be worse than the
    download it saves."""
    conditional(monkeypatch, unchanged_for='"v1"')
    run(data_root, "--year", "2023")

    # The server now calls it something else -- our held validator is stale.
    conditional(monkeypatch, etag='"v2"', unchanged_for='"v2"')
    result = run(data_root, "--year", "2023")

    assert latest_manifest(data_root)["files"][0]["outcome"] == "ingested"
    assert result.exit_code == 0
    assert next(iter(validators(data_root).values()))["etag"] == '"v2"'


def test_a_window_whose_rows_never_landed_is_fetched_again(data_root, monkeypatch):
    """The rule the whole mechanism rests on: a validator means "fully ingested
    at this version". Recorded any earlier -- at the landing, say -- the next run
    would step straight past a window whose rows never made it out of the parser.
    """
    conditional(monkeypatch)

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("kelpcompare.cli.write_observations", explode)
    failed = run(data_root, "--year", "2023")

    assert failed.exit_code == 1
    assert validators(data_root) == {}  # nothing was remembered
    assert (data_root / "raw" / "ndbc" / "LJAC1").exists()  # ...though it did land

    monkeypatch.undo()
    asked = conditional(monkeypatch, unchanged_for='"v1"')
    recovered = run(data_root, "--year", "2023")

    assert asked == [{}]  # asked unconditionally, because nothing was known
    assert recovered.exit_code == 0
    assert len(read_observations(Zones.at(data_root), "ndbc")) == 400 * 3


def test_a_dry_run_remembers_nothing(data_root, monkeypatch):
    """It wrote no rows, so it has no right to claim the window is ingested."""
    conditional(monkeypatch)
    run(data_root, "--year", "2023", "--dry-run")

    assert validators(data_root) == {}


def test_a_source_that_offers_no_validator_is_simply_fetched_every_time(data_root, offline):
    """The recorded-payload fixture sends neither header, which is how a source
    without them behaves: no entry, no condition, no harm."""
    run(data_root, "--year", "2023")
    run(data_root, "--year", "2023")

    assert validators(data_root) == {}
    assert offline == [("LJAC1", 2023), ("LJAC1", 2023)]


def test_the_whole_run_shares_one_session(data_root, monkeypatch):
    """A nineteen-year backfill should open one connection, not nineteen."""
    seen: list[object] = []

    def archive(station, year, *, session=None, validators=None):
        seen.append(session)
        return new_payload(
            "ndbc",
            station.upper(),
            f"{station.lower()}h{year}.txt.gz",
            f"https://example.invalid/{year}",
            gzip.compress(ARCHIVE.read_bytes(), mtime=0),
        )

    monkeypatch.setattr(ndbc, "fetch_archive", archive)
    run(data_root, "--year", "2023", "--year", "2024", "--year", "2025")

    assert len(seen) == 3
    assert all(s is seen[0] for s in seen)
    assert seen[0] is not None
