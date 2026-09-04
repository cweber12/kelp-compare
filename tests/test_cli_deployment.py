"""`kelpcompare deployments`, end to end (docs/04 §1, docs/03 `deployment.parquet`).

Runs the real commands against a `tmp_path` data root: ingest the reviewed
TidbiT export, then reduce it over its own window. What is asserted is what an
operator would get.

The arithmetic is pinned in `test_features_deployment.py` on frames whose answer
can be worked out by hand. What these cases hold is the plumbing — that the
command reads the registry the `features` command refuses to, that it writes the
docs/03 table and records it in a manifest, and above all that the same landed
rows come out of this command usable and out of `features` unusable, which is
the whole reason the table exists.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.registry import find_deployment, load_registry
from kelpcompare.storage import Zones, read_features

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
ORIGINAL = FIX / "Tidbit_1__22506632__2026-08-01_07_44_27_PDT__Data_PDT_.xlsx"
REGISTRY_SOURCE = REPO_ROOT / "data" / "registry"

KNOWN_SERIAL = "22506632"
DEPLOYMENT = find_deployment(load_registry(REGISTRY_SOURCE / "sites.json"), KNOWN_SERIAL)
PROJECT_SITE = DEPLOYMENT.site_id


@pytest.fixture
def data_root(tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "registry").mkdir(parents=True)
    for name in ("sites.json", "parameters.json", "features.json"):
        shutil.copy2(REGISTRY_SOURCE / name, root / "registry" / name)
    (root / "raw" / "project_sensors" / "incoming").mkdir(parents=True)
    return root


def run(data_root: Path, command: str, *extra: str, expect: int = 0):
    result = CliRunner().invoke(main, [command, "--data-root", str(data_root), *extra])
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    assert result.exit_code == expect, f"{command} exited {result.exit_code}: {result.output}"
    return result


def ingest_deployment(data_root: Path) -> None:
    shutil.copy2(ORIGINAL, data_root / "raw" / "project_sensors" / "incoming" / ORIGINAL.name)
    run(data_root, "ingest", "--source", "project")


def test_deployments_writes_the_docs03_table(data_root):
    ingest_deployment(data_root)

    run(data_root, "deployments")

    table = read_features(Zones.at(data_root), "deployment")
    (row,) = table[table["site_id"] == PROJECT_SITE].to_dict("records")
    assert row["serial"] == KNOWN_SERIAL
    assert row["deployment_number"] == DEPLOYMENT.deployment_number
    assert row["depth_m"] == pytest.approx(DEPLOYMENT.depth_m)
    assert row["parameter"] == "sea_water_temperature"
    assert row["instrument"] == DEPLOYMENT.instrument


def test_the_record_the_quarterly_table_calls_unusable_is_usable_here(data_root):
    """The defect this table exists to correct, end to end on the real export.

    The same landed rows, reduced two ways. Against Q3 the logger recorded under
    a quarter of a quarter and `usable` is false; against its own window it
    recorded essentially all of it. Only one of those is a statement about the
    instrument.
    """
    ingest_deployment(data_root)

    run(data_root, "features", "--source", "project")
    run(data_root, "deployments")

    zones = Zones.at(data_root)
    quarterly = read_features(zones, "quarterly_env")
    (quarter,) = quarterly[quarterly["site_id"] == PROJECT_SITE].to_dict("records")
    deployment = read_features(zones, "deployment")
    (window,) = deployment[deployment["site_id"] == PROJECT_SITE].to_dict("records")

    assert not quarter["usable"]
    assert quarter["pct_coverage"] < 0.25
    assert window["usable"]
    assert window["pct_coverage"] > 0.99

    # The same rows, so the water is described identically. Only the window moved.
    assert window["n_obs"] == quarter["n_obs"]
    assert window["mean"] == pytest.approx(quarter["mean"])
    assert window["max"] == pytest.approx(quarter["max"])
    assert window["days_above_20c"] == quarter["days_above_20c"]


def test_a_spell_reaching_the_start_of_the_deployment_is_no_longer_a_floor(data_root):
    """Q3 opens on 1 July and this logger went in on the 11th, so the quarterly
    row marks its longest warm spell as a floor. Against the deployment window
    there are no unobserved days to break it, and it is a measurement."""
    ingest_deployment(data_root)

    run(data_root, "features", "--source", "project")
    run(data_root, "deployments")

    zones = Zones.at(data_root)
    quarterly = read_features(zones, "quarterly_env")
    (quarter,) = quarterly[quarterly["site_id"] == PROJECT_SITE].to_dict("records")
    deployment = read_features(zones, "deployment")
    (window,) = deployment[deployment["site_id"] == PROJECT_SITE].to_dict("records")

    assert quarter["max_spell_above_20c_gap_interrupted"]
    assert not window["max_spell_above_20c_gap_interrupted"]
    assert window["max_spell_above_20c_days"] == quarter["max_spell_above_20c_days"]


def test_the_table_offers_no_anomaly_column_to_be_misread(data_root):
    """docs/04 s3 and ADR-007: a project sensor cannot have a climatology for a
    decade, so this table declines to carry a column that could only be null."""
    ingest_deployment(data_root)

    run(data_root, "deployments")

    table = read_features(Zones.at(data_root), "deployment")
    assert not [name for name in table.columns if name.endswith("_anom")]


def test_it_joins_to_the_validation_table_on_the_deployment(data_root):
    """The key is `validation.parquet`'s without its reference columns, so what
    the instrument recorded sits beside how it compared to its neighbours."""
    ingest_deployment(data_root)

    run(data_root, "deployments")

    zones = Zones.at(data_root)
    deployment = read_features(zones, "deployment")
    shared = ["site_id", "serial", "deployment_number", "parameter", "depth_m"]
    assert not set(shared) - set(deployment.columns)


def test_the_run_manifest_records_every_table_it_wrote(data_root):
    """A features-zone table that could not be traced to the run that wrote it
    would be the gap docs/03 run manifests exist to close (hard rule 7). This
    command writes three, and one traceable table beside two untraceable ones is
    the same gap. Asserted as a set equality rather than a subset, so a fourth
    table added later fails here rather than going unmanifested."""
    ingest_deployment(data_root)

    run(data_root, "deployments")

    manifests = sorted((data_root / "raw" / "_manifests").glob("*-deployments.json"))
    entries = json.loads(manifests[-1].read_text(encoding="utf-8"))["tables"]
    zones = Zones.at(data_root)
    assert {entry["table"] for entry in entries} == {
        "deployment",
        "deployment_daily",
        "deployment_hourly",
    }
    for entry in entries:
        path = zones.feature_table(entry["table"])
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["rows"] == len(read_features(zones, entry["table"]))


def test_a_strictness_that_excludes_every_row_says_so(data_root):
    """Ingest lands in-window rows at flag 2, so a pass-only run before
    `kelpcompare qc` excludes the deployment. That has to be a warning naming the
    site rather than an empty table, which would read as a logger nobody
    registered."""
    ingest_deployment(data_root)

    result = run(data_root, "deployments", "--qc-max-flag", "1")

    assert "produces no deployment row" in result.output
    assert str(PROJECT_SITE) in result.output
    assert not Zones.at(data_root).feature_table("deployment").exists()


def test_running_qc_first_brings_those_rows_back(data_root):
    """The counterpart: the same strictness works once the rows carry a verdict."""
    ingest_deployment(data_root)
    run(data_root, "qc", "--source", "project")

    run(data_root, "deployments", "--qc-max-flag", "1")

    table = read_features(Zones.at(data_root), "deployment")
    assert len(table[table["site_id"] == PROJECT_SITE]) == 1


def test_a_dry_run_writes_nothing_at_all(data_root):
    ingest_deployment(data_root)

    result = run(data_root, "deployments", "--dry-run")

    assert "dry run" in result.output
    assert not Zones.at(data_root).feature_table("deployment").exists()
    assert not list((data_root / "raw" / "_manifests").glob("*-deployments.json"))
