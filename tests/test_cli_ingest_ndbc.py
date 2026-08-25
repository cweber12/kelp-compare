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
from kelpcompare.fetchers.base import SourceUnavailable, new_payload
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

    def realtime(station, *, session=None):
        asked.append((station, None))
        return new_payload(
            "ndbc",
            station.upper(),
            f"{station.upper()}.txt",
            "file://realtime",
            REALTIME.read_bytes(),
        )

    def archive(station, year, *, session=None):
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
    assert len(stored) == 300 * 5
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

    # 390 rows get all three tests; 5 sit beside a gap, where the spike test
    # has no pair of neighbours and correctly says nothing (docs/03).
    assert verdicts["gross_range:pass;spike:pass;rate_of_change:pass"] == 390
    assert verdicts["gross_range:pass;rate_of_change:pass"] == 5

    # The sentinel rows are judged as missing by every test rather than passed:
    # there is nothing in an absent value to evaluate, and missing outranks all.
    assert verdicts["gross_range:missing;spike:missing;rate_of_change:missing"] == 5
    assert water["qc_flag"].value_counts().to_dict() == {1: 395, 9: 5}


def test_a_parameter_the_station_never_measures_is_all_missing(data_root, offline):
    """LJAC1 is a shore station: its wave columns are sentinel from end to end.

    Stored anyway, as 400 missing rows rather than as nothing. "This station
    reported no wave height" and "nobody asked this station for wave height" are
    different facts, and only the first is recoverable from a row that exists.
    """
    run(data_root, "--year", "2023")
    stored = read_observations(Zones.at(data_root), "ndbc")

    waves = stored[stored["parameter"] == "wave_significant_height"]
    assert len(waves) == 400
    assert waves["value"].isna().all()
    assert set(waves["qc_flag"]) == {9}


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
    assert entry["rows_out"] == 400 * 5
    assert entry["landed"].endswith("ljac1h2023.txt.gz")


def test_the_manifest_carries_the_flag_histogram(data_root, offline):
    run(data_root, "--year", "2023")
    payload = manifest(data_root)

    # 2 = not evaluated, 9 = missing. Both are docs/03 ingest-time states.
    assert payload["qc_flags"]["2"] > 0
    assert payload["qc_flags"]["9"] > 0
    assert sum(payload["qc_flags"].values()) == 400 * 5


# --------------------------------------------------------------------------
# Failure, told apart
# --------------------------------------------------------------------------


def test_an_outage_is_a_gap_and_does_not_fail_the_run(data_root, monkeypatch):
    """docs/01 §5: a missing NDBC year must never block the rest of a run."""

    def down(station, year, *, session=None):
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

    def garbage(station, year, *, session=None):
        return new_payload("ndbc", station, "x.txt.gz", "file://x", b"not an NDBC file\n")

    monkeypatch.setattr(ndbc, "fetch_archive", garbage)

    result = run(data_root, "--year", "2023")

    assert result.exit_code == 1
    entry = manifest(data_root)["files"][0]
    assert entry["outcome"] == "failed"
    assert "two '#' header lines" in entry["reason"]


def test_a_payload_that_will_not_parse_is_still_landed(data_root, monkeypatch):
    """Landed before parsed: realtime holds ~45 days, so today's bytes are it."""

    def garbage(station, *, session=None):
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
