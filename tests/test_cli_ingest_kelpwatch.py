"""`kelpcompare ingest --source kelpwatch`, end to end (docs/02, docs/03).

The real command against a `tmp_path` data root seeded with the committed
registry, driven with the Click runner. Nothing is stubbed: this source is
downloaded by hand, so there is no network seam to replace and the whole path
from a dropped file to a landing and a manifest is the code an operator runs.

The two things worth watching are what this ingest does *not* do -- it writes no
observations, because a canopy value belongs to a polygon and that zone is keyed
on `site_id` -- and how it decides which polygon a file belongs to, which is the
registry and nothing in the file.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.fetchers import kelpwatch

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_SOURCE = REPO_ROOT / "data" / "registry"
FIX = Path(__file__).parent / "fixtures" / "kelpwatch"
LAJOLLA = FIX / "kelp_lajolla.csv"
DELMAR = FIX / "kelp_delmar.csv"


@pytest.fixture
def data_root(tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "registry").mkdir(parents=True)
    for name in ("sites.json", "parameters.json", "features.json", "polygons.geojson"):
        shutil.copy2(REGISTRY_SOURCE / name, root / "registry" / name)
    return root


def drop(data_root: Path, *sources: Path, rename: str | None = None) -> Path:
    """Put exports where the operator puts them."""
    incoming = data_root / "raw" / "kelpwatch" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    for source in sources:
        shutil.copy2(source, incoming / (rename or source.name))
    return incoming


def run(data_root: Path, *extra: str):
    result = CliRunner().invoke(
        main, ["ingest", "--source", "kelpwatch", "--data-root", str(data_root), *extra]
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def manifest(data_root: Path) -> dict:
    files = sorted((data_root / "raw" / "_manifests").glob("*.json"))
    assert len(files) == 1, f"expected one manifest, found {len(files)}"
    return json.loads(files[0].read_text())


def landings(data_root: Path) -> list[Path]:
    root = data_root / "raw" / "kelpwatch"
    return sorted(p for p in root.rglob("*.csv") if "incoming" not in p.parts)


def unpin_revision(data_root: Path) -> None:
    path = data_root / "registry" / "polygons.geojson"
    payload = json.loads(path.read_text())
    del payload["kelp_watch"]
    path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_an_export_lands_untouched_under_its_revision_and_polygon(data_root):
    drop(data_root, LAJOLLA)
    result = run(data_root)

    assert result.exit_code == 0
    (landed,) = landings(data_root)
    assert landed.read_bytes() == LAJOLLA.read_bytes()  # untouched, hard rule 1
    assert landed.parent.name == "KELP_LA-JOLLA"
    assert landed.parent.parent.name == "ver23"


def test_the_ingest_writes_no_observations(data_root):
    """The one source that lands raw and stops. A canopy value belongs to a
    polygon; `observations/` is keyed on `site_id` (docs/03)."""
    drop(data_root, LAJOLLA, DELMAR)
    run(data_root)

    assert not (data_root / "observations").exists()
    assert not (data_root / "features").exists()


def test_every_dropped_export_is_ingested(data_root):
    drop(data_root, LAJOLLA, DELMAR)
    result = run(data_root)

    assert result.exit_code == 0
    assert len(landings(data_root)) == 2
    assert manifest(data_root)["counts"] == {"ingested": 2}


def test_re_dropping_the_same_bytes_lands_once(data_root):
    """Content-addressed, like every other landing: re-running is a no-op."""
    drop(data_root, LAJOLLA)
    run(data_root)
    drop(data_root, LAJOLLA)
    run(data_root)

    assert len(landings(data_root)) == 1


def test_two_revisions_never_share_a_directory(data_root):
    """A newer revision may revise history as well as extend it, so the
    directory is what makes mixing them impossible rather than discouraged."""
    drop(data_root, LAJOLLA)
    run(data_root)

    path = data_root / "registry" / "polygons.geojson"
    payload = json.loads(path.read_text())
    payload["kelp_watch"]["revision"] = 24
    path.write_text(json.dumps(payload), encoding="utf-8")
    run(data_root)

    assert {p.parent.parent.name for p in landings(data_root)} == {"ver23", "ver24"}


# --------------------------------------------------------------------------
# The registry decides which polygon a file is
# --------------------------------------------------------------------------


def test_an_export_the_registry_does_not_claim_is_quarantined(data_root):
    """Hard rule 5's posture for this source: the file says nothing about which
    geometry it describes, so an unclaimed one is never attributed by guesswork."""
    drop(data_root, LAJOLLA, rename="kelp_somewhere_else.csv")
    result = run(data_root)

    entry = manifest(data_root)["files"][0]
    assert entry["outcome"] == "quarantined"
    assert "source_file" in entry["reason"]
    assert (data_root / "quarantine" / "kelp_somewhere_else.csv").exists()
    assert landings(data_root) == []
    assert result.exit_code == 0  # the gate working is not a failure


def test_the_polygon_comes_from_the_filename_not_the_contents(data_root):
    """The same bytes under two names are two different polygons -- which is why
    getting `source_file` wrong is a silent error, and why the registry is
    the only thing allowed to decide."""
    drop(data_root, LAJOLLA, rename="kelp_delmar.csv")
    run(data_root)

    entry = manifest(data_root)["files"][0]
    assert entry["polygon_id"] == "KELP:DEL-MAR"


def test_a_file_that_is_not_an_export_is_skipped_not_quarantined(data_root):
    """Nothing claimed it and nothing recognised it; there is no judgement to
    record about a file that simply is not this source's."""
    incoming = drop(data_root, LAJOLLA)
    (incoming / "notes.txt").write_text("not an export\n", encoding="utf-8")
    result = run(data_root)

    outcomes = {Path(e["path"]).name: e["outcome"] for e in manifest(data_root)["files"]}
    assert outcomes["notes.txt"] == "skipped"
    assert outcomes["kelp_lajolla.csv"] == "ingested"
    assert result.exit_code == 0


# --------------------------------------------------------------------------
# The pinned revision
# --------------------------------------------------------------------------


def test_a_registry_that_pins_no_revision_refuses_the_whole_run(data_root):
    """Not fail-soft, deliberately: the export carries no version of its own, so
    a landing made without one could never be traced to a citable dataset."""
    drop(data_root, LAJOLLA)
    unpin_revision(data_root)
    result = run(data_root)

    assert result.exit_code != 0
    assert "kelp_watch.revision" in str(result.exception)
    assert landings(data_root) == []
    assert not (data_root / "raw" / "_manifests").exists()


def test_the_manifest_records_the_revision_on_every_landing(data_root):
    """docs/02: the provenance chain from a figure to a DOI closes here, because
    it closes nowhere in the file."""
    drop(data_root, LAJOLLA, DELMAR)
    run(data_root)

    assert {e["dataset_revision"] for e in manifest(data_root)["files"]} == {23}


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------


def test_the_manifest_records_a_polygon_rather_than_a_site(data_root):
    drop(data_root, LAJOLLA)
    run(data_root)
    entry = manifest(data_root)["files"][0]

    assert entry["outcome"] == "ingested"
    assert entry["fetcher"] == "kelpwatch"
    assert entry["adapter"] is None
    assert entry["polygon_id"] == "KELP:LA-JOLLA"
    assert entry["site_id"] is None  # a polygon is not a site
    assert entry["rows_in"] == 212  # the export, max rows included
    assert entry["rows_out"] == 170  # the quarters


def test_the_dropped_max_rows_are_recorded_against_the_file(data_root):
    drop(data_root, LAJOLLA)
    run(data_root)
    entry = manifest(data_root)["files"][0]

    assert any("42 derived `max` row" in warning for warning in entry["warnings"])


def test_a_cloud_gap_is_recorded_as_a_gap_and_reported(data_root):
    """It is an upstream hole, the same kind of fact an NDBC outage is -- and
    for this source the hole is the result, so the operator sees it without
    opening the manifest."""
    drop(data_root, LAJOLLA)
    result = run(data_root)

    gaps = manifest(data_root)["gaps"]
    assert any("KELP:LA-JOLLA: 7 quarter(s)" in gap for gap in gaps)
    assert "no cloud-free observation" in result.output
    assert result.exit_code == 0  # a cloud gap is the record, not a failure


def test_a_clean_run_raises_no_run_level_warning(data_root):
    """Both parser notes fire on every well-formed export, so promoting them
    would make a warning that always fires -- which stops being read."""
    drop(data_root, LAJOLLA, DELMAR)
    run(data_root)

    assert manifest(data_root)["warnings"] == []


# --------------------------------------------------------------------------
# Failure, told apart
# --------------------------------------------------------------------------


def test_an_export_that_will_not_parse_fails_the_run(data_root):
    """A format change is not a cloud gap; it needs a human, so it sets the code."""
    incoming = drop(data_root, DELMAR)
    header = ",".join(kelpwatch.EXPORT_COLUMNS)
    (incoming / "kelp_lajolla.csv").write_text(
        header + "\n1984,annual,0,0,10,10\n", encoding="utf-8"
    )
    result = run(data_root)

    assert result.exit_code == 1
    outcomes = {Path(e["path"]).name: e for e in manifest(data_root)["files"]}
    assert outcomes["kelp_lajolla.csv"]["outcome"] == "failed"
    assert "annual" in outcomes["kelp_lajolla.csv"]["reason"]
    # ...and the export that was fine still landed.
    assert outcomes["kelp_delmar.csv"]["outcome"] == "ingested"


def test_a_bad_export_is_not_landed(data_root):
    """Unlike a pulled payload, this file is not going anywhere: the operator
    still has it, and `raw/` is the record of what the project chose to trust."""
    incoming = drop(data_root)
    (incoming / "kelp_lajolla.csv").write_text("nonsense\n", encoding="utf-8")
    run(data_root)

    assert landings(data_root) == []


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing_at_all(data_root):
    drop(data_root, LAJOLLA)
    result = run(data_root, "--dry-run")

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert landings(data_root) == []
    assert not (data_root / "raw" / "_manifests").exists()


def test_a_single_file_can_be_named(data_root):
    incoming = drop(data_root, LAJOLLA, DELMAR)
    run(data_root, "--path", str(incoming / "kelp_delmar.csv"))

    assert [p.parent.name for p in landings(data_root)] == ["KELP_DEL-MAR"]


def test_nothing_dropped_is_not_an_error(data_root):
    result = run(data_root)

    assert result.exit_code == 0
    assert "nothing to ingest" in result.output


def test_station_and_year_are_refused_for_a_dropped_source(data_root):
    result = run(data_root, "--year", "2023")

    assert result.exit_code != 0
    assert "do not apply" in str(result.exception)
