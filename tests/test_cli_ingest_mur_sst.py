"""`kelpcompare ingest --source mur_sst`, end to end (docs/02, docs/03).

The real command against a `tmp_path` data root, with the network replaced at
the one seam that touches it -- `fetch_realtime` / `fetch_archive`. Everything
below that, the landing, the reduction, the write and the manifest, is the code
an operator runs. Nothing here reaches NOAA, per CLAUDE.md.

What this source adds over the other pulled ones is that a window is addressed
by a *bed* rather than a station code: six sites share one dataset id, and what
varies between them is the box their outline occupies. So the cases below are
mostly about the seam that resolves that -- and about what it refuses, since a
derived site whose polygon has no outline must cost the run one bed rather than
the run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.fetchers import mur_sst
from kelpcompare.fetchers.base import SourceUnavailable, new_payload
from kelpcompare.storage import Zones, read_observations

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures" / "mur_sst"
DEL_MAR = FIX / "del_mar_2020-07-01_03_excerpt.csv"
LA_JOLLA = FIX / "la_jolla_2020-07-01_03_excerpt.csv"
REGISTRY_SOURCE = REPO_ROOT / "data" / "registry"

#: Which recorded payload stands in for which bed. Every other bed is served
#: Del Mar's grid, which would not reduce -- so the cases below scope to these
#: two, and the default fan out has one case of its own that expects the rest to
#: fail rather than to quietly produce a number from another bed's water.
RECORDED = {"KELP_DEL-MAR": DEL_MAR, "KELP_LA-JOLLA": LA_JOLLA}


@pytest.fixture
def data_root(tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "registry").mkdir(parents=True)
    for name in ("sites.json", "parameters.json", "polygons.geojson"):
        shutil.copy2(REGISTRY_SOURCE / name, root / "registry" / name)
    return root


@pytest.fixture
def offline(monkeypatch):
    """Serve the recorded payloads in place of the network, and record the asks."""
    asked: list[tuple[str, tuple, int | None]] = []

    def serve(bounds, year, station):
        asked.append((station, tuple(round(b, 4) for b in bounds), year))
        path = RECORDED.get(station)
        if path is None:
            raise SourceUnavailable(f"no recorded payload for {station}")
        label = f"{station}_realtime.csv" if year is None else f"{station}_{year}.csv"
        return new_payload(mur_sst.SOURCE, station, label, f"file://{label}", path.read_bytes())

    def realtime(bounds, *, station, session=None, validators=None):
        return serve(bounds, None, station)

    def archive(bounds, year, *, station, session=None, validators=None):
        return serve(bounds, year, station)

    monkeypatch.setattr(mur_sst, "fetch_realtime", realtime)
    monkeypatch.setattr(mur_sst, "fetch_archive", archive)
    return asked


def run(data_root: Path, *extra: str, stations: tuple[str, ...] = ("SST:DEL-MAR",)):
    """The real command, scoped to the beds a recorded payload exists for."""
    scope = [arg for station in stations for arg in ("--station", station)]
    result = CliRunner().invoke(
        main, ["ingest", "--source", "mur_sst", "--data-root", str(data_root), *scope, *extra]
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def manifest(data_root: Path) -> dict:
    files = sorted((data_root / "raw" / "_manifests").glob("*.json"))
    assert len(files) == 1, f"expected one manifest, found {len(files)}"
    return json.loads(files[0].read_text())


def edit_site(data_root: Path, site_id: str, **fields) -> None:
    """Edit one site record in the copied registry, the way an operator would."""
    path = data_root / "registry" / "sites.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    for site in doc["sites"]:
        if site["site_id"] == site_id:
            site.update({k: v for k, v in fields.items() if v is not None})
            for key, value in fields.items():
                if value is None:
                    site.pop(key, None)
    path.write_text(json.dumps(doc), encoding="utf-8")


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_bed_lands_its_payload_and_writes_one_row_per_day(data_root, offline):
    assert run(data_root).exit_code == 0

    stored = read_observations(Zones.at(data_root), source=mur_sst.SOURCE)
    assert len(stored) == 3
    assert set(stored["site_id"]) == {"SST:DEL-MAR"}
    assert set(stored["parameter"]) == {"sea_water_temperature"}
    assert stored["depth_m"].isna().all()
    assert list(stored["value"]) == pytest.approx([20.0006, 20.7961, 20.8526], abs=5e-4)


def test_the_landing_directory_is_the_bed_not_the_dataset(data_root, offline):
    """Six derived sites share one `station_code`, so landing by it would put
    six beds' bytes in one directory under names that differ only by digest."""
    run(data_root)

    landed = sorted((data_root / "raw" / "mur_sst").rglob("*.csv"))
    assert [p.parent.name for p in landed] == ["KELP_DEL-MAR"]
    assert landed[0].name.endswith("__KELP_DEL-MAR_realtime.csv")


def test_the_request_box_comes_from_the_beds_own_outline(data_root, offline):
    """The whole point of the derived-site seam: what varies between these six
    windows is the geometry the registry pairs each with, not a station code."""
    run(data_root, stations=("SST:DEL-MAR", "SST:LA-JOLLA"))

    boxes = {station: bounds for station, bounds, _ in offline}
    assert boxes["KELP_DEL-MAR"] == (-117.2903, 32.9334, -117.2587, 32.9713)
    assert boxes["KELP_LA-JOLLA"] == (-117.3175, 32.7891, -117.2574, 32.8671)


def test_two_beds_produce_two_series_from_the_same_dataset(data_root, offline):
    assert run(data_root, stations=("SST:DEL-MAR", "SST:LA-JOLLA")).exit_code == 0

    stored = read_observations(Zones.at(data_root), source=mur_sst.SOURCE)
    assert set(stored["site_id"]) == {"SST:DEL-MAR", "SST:LA-JOLLA"}
    assert len(stored) == 6


def test_an_archive_year_is_asked_for_per_bed(data_root, offline):
    assert run(data_root, "--year", "2020").exit_code == 0

    assert [(station, year) for station, _, year in offline] == [("KELP_DEL-MAR", 2020)]


def test_the_manifest_names_the_bed_rather_than_the_shared_dataset_id(data_root, offline):
    """`jplMURSST41 realtime` six times over is a manifest nobody can read."""
    run(data_root, stations=("SST:DEL-MAR", "SST:LA-JOLLA"))

    entries = {e["site_id"]: e for e in manifest(data_root)["files"]}
    assert set(entries) == {"SST:DEL-MAR", "SST:LA-JOLLA"}
    assert entries["SST:DEL-MAR"]["outcome"] == "ingested"
    assert entries["SST:DEL-MAR"]["rows_in"] == 60
    assert entries["SST:DEL-MAR"]["rows_out"] == 3


def test_the_coverage_the_reduction_achieved_reaches_the_manifest(data_root, offline):
    """A bed's mean is over the part of it the grid backs, and that fraction is
    not derivable from the stored rows -- so it has to be recorded here."""
    run(data_root)

    (entry,) = manifest(data_root)["files"]
    assert any("94.9%" in warning for warning in entry["warnings"])


def test_a_re_run_lands_nothing_new_and_supersedes_its_own_rows(data_root, offline):
    """Content-addressed landing plus the docs/03 partition rewrite: the same
    window twice is one file in `raw/` and one row per day in the partition."""
    run(data_root)
    run(data_root)

    assert len(list((data_root / "raw" / "mur_sst").rglob("*.csv"))) == 1
    assert len(read_observations(Zones.at(data_root), source=mur_sst.SOURCE)) == 3


def test_no_validator_is_recorded_for_this_host(data_root, offline):
    """Measured: this host serves no ETag and a `Last-Modified` that is the
    response's own generation time. A cache entry here would be a timestamp
    presented as a version."""
    run(data_root)

    cached = data_root / "cache" / "http-validators.json"
    urls = json.loads(cached.read_text())["urls"] if cached.exists() else {}
    assert urls == {}


# --------------------------------------------------------------------------
# What one bad registry record costs
# --------------------------------------------------------------------------


def test_the_default_scope_is_every_derived_site_the_registry_declares(data_root, offline):
    """Asserted against the registry rather than a written list, so adding a bed
    does not edit a test -- but a bed silently dropping out still fails here."""
    CliRunner().invoke(main, ["ingest", "--source", "mur_sst", "--data-root", str(data_root)])
    asked = {station for station, _, _ in offline}

    declared = {
        site["derived_from"]["polygon_id"].replace(":", "_")
        for site in json.loads((data_root / "registry" / "sites.json").read_text(encoding="utf-8"))[
            "sites"
        ]
        if site.get("operator") == "mur_sst"
    }
    assert declared <= asked


def test_a_bed_with_no_recorded_outline_costs_that_bed_and_not_the_run(data_root, offline):
    """docs/03 makes geometry optional, and this is the stage that cannot
    proceed without one. The other bed in the same run still lands."""
    path = data_root / "registry" / "polygons.geojson"
    doc = json.loads(path.read_text(encoding="utf-8"))
    for feature in doc["features"]:
        if feature["properties"]["polygon_id"] == "KELP:DEL-MAR":
            feature["geometry"] = None
    path.write_text(json.dumps(doc), encoding="utf-8")

    run(data_root, stations=("SST:DEL-MAR", "SST:LA-JOLLA"))

    entries = {e["site_id"]: e for e in manifest(data_root)["files"]}
    assert entries["SST:DEL-MAR"]["outcome"] == "failed"
    assert "outline has not been recorded" in entries["SST:DEL-MAR"]["reason"]
    assert entries["SST:LA-JOLLA"]["outcome"] == "ingested"


def test_a_derived_site_naming_a_polygon_that_does_not_exist_is_refused(data_root, offline):
    edit_site(data_root, "SST:DEL-MAR", derived_from={"polygon_id": "KELP:ATLANTIS"})

    run(data_root)

    (entry,) = manifest(data_root)["files"]
    assert entry["outcome"] == "failed"
    assert "polygons.geojson does not" in entry["reason"]
    assert not list((data_root / "raw" / "mur_sst").rglob("*.csv"))


def test_a_site_under_this_source_with_no_derivation_at_all_is_refused(data_root, offline):
    """The registry gate for a derived source. Without the block there is
    nothing to say which bed the rows would be about, and a fetch that went
    ahead would land bytes under a bed nothing reduces."""
    edit_site(data_root, "SST:DEL-MAR", derived_from=None)

    run(data_root)

    (entry,) = manifest(data_root)["files"]
    assert entry["outcome"] == "failed"
    assert "declares no `derived_from`" in entry["reason"]


def test_an_outage_on_one_bed_is_a_gap_rather_than_a_failure(data_root, offline):
    """docs/01 s5: a source that did not answer costs the run that window and
    does not set the exit code."""
    result = run(data_root, stations=("SST:DEL-MAR", "SST:ENCINITAS"))

    entries = {e["site_id"]: e for e in manifest(data_root)["files"]}
    assert entries["SST:ENCINITAS"]["outcome"] == "skipped"
    assert entries["SST:DEL-MAR"]["outcome"] == "ingested"
    assert result.exit_code == 0
