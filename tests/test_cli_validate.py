"""`kelpcompare validate`, end to end (docs/04 §1, docs/03 `validation.parquet`).

Runs the real commands against a `tmp_path` data root: ingest the reviewed
TidbiT export, land a reference series beside it, then validate. What is
asserted is what an operator would get.

The reference here is synthetic, because the point of these cases is the
plumbing — that the command reads the registry the `features` command refuses
to, writes the docs/03 table, and reports a refusal as a refusal. The
arithmetic itself is pinned in `test_features_validation.py` on pairs whose
answer can be worked out by hand.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.registry import find_deployment, load_registry
from kelpcompare.storage import Zones, read_features, write_observations

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


def land_reference(data_root: Path, *, site_id: str, depth_m: float, offset: float) -> None:
    """A reference series covering the deployment, offset by a known amount.

    Built from the deployment's own landed rows so the two overlap exactly,
    which is what lets a test assert on the bias without also asserting on how
    many bins happened to line up.
    """
    zones = Zones.at(data_root)
    own = pd.read_parquet(next(zones.observations.glob("source=project/**/*.parquet")))
    reference = own.assign(
        # Storage keeps timestamps tz-naive UTC (docs/03) and refuses to take
        # them back that way, so put the zone on before handing them over.
        timestamp=own["timestamp"].dt.tz_localize("UTC"),
        site_id=site_id,
        depth_m=depth_m,
        value=own["value"] - offset,
        source="ndbc",
        qc_flag=1,
    )
    write_observations(reference, zones, source="ndbc", run_id="test")


def ingest_deployment(data_root: Path) -> None:
    shutil.copy2(ORIGINAL, data_root / "raw" / "project_sensors" / "incoming" / ORIGINAL.name)
    run(data_root, "ingest", "--source", "project")


def test_validate_writes_the_docs03_table(data_root):
    """The committed registry names LJAC1 at 3.4 m, 4.83 m above this logger --
    inside the default 5.0 m tolerance, so all three statistics are reported."""
    ingest_deployment(data_root)
    land_reference(data_root, site_id="NDBC:LJAC1", depth_m=3.4, offset=1.0)

    run(data_root, "validate")

    table = read_features(Zones.at(data_root), "validation")
    (row,) = table[table["site_id"] == PROJECT_SITE].to_dict("records")
    assert row["reference_site_id"] == "NDBC:LJAC1"
    assert row["depth_gap_m"] == pytest.approx(abs(DEPLOYMENT.depth_m - 3.4))
    assert row["depth_comparable"]
    assert row["bias"] == pytest.approx(1.0)
    assert row["serial"] == KNOWN_SERIAL


def test_the_run_manifest_records_the_table_it_wrote(data_root):
    """`validation` is a features-zone table too, and was as untraceable as the
    rest of the zone (docs/03 run manifests)."""
    ingest_deployment(data_root)
    land_reference(data_root, site_id="NDBC:LJAC1", depth_m=3.4, offset=1.0)

    run(data_root, "validate")

    manifests = sorted((data_root / "raw" / "_manifests").glob("*-validate.json"))
    (entry,) = json.loads(manifests[-1].read_text(encoding="utf-8"))["tables"]
    path = Zones.at(data_root).feature_table("validation")
    assert entry["table"] == "validation"
    assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert entry["rows"] == len(read_features(Zones.at(data_root), "validation"))


def test_a_strictness_that_excludes_every_row_says_so(data_root):
    """Ingest lands in-window rows at flag 2, "not evaluated", so the pass-only
    rerun docs/04 s1 asks for excludes a deployment that has not been through
    `kelpcompare qc` yet. That has to be a warning naming the site rather than
    an empty table, which would read as a sensor nobody could find a reference
    for."""
    ingest_deployment(data_root)
    land_reference(data_root, site_id="NDBC:LJAC1", depth_m=3.4, offset=1.0)

    result = run(data_root, "validate", "--qc-max-flag", "1")

    assert "cannot be validated" in result.output
    assert str(PROJECT_SITE) in result.output
    assert not Zones.at(data_root).feature_table("validation").exists()


def test_running_qc_first_brings_those_rows_back(data_root):
    """The counterpart: the same strictness works once the rows carry a verdict."""
    ingest_deployment(data_root)
    land_reference(data_root, site_id="NDBC:LJAC1", depth_m=3.4, offset=1.0)
    run(data_root, "qc", "--source", "project")

    run(data_root, "validate", "--qc-max-flag", "1")

    table = read_features(Zones.at(data_root), "validation")
    (row,) = table[table["site_id"] == PROJECT_SITE].to_dict("records")
    assert row["qc_max_flag"] == 1
    assert row["bias"] == pytest.approx(1.0)


def test_the_tolerance_comes_from_features_json(data_root):
    """ADR-006: retuning the threshold is a registry edit, not a code change."""
    ingest_deployment(data_root)
    land_reference(data_root, site_id="NDBC:LJAC1", depth_m=3.4, offset=1.0)
    config = data_root / "registry" / "features.json"
    payload = config.read_text(encoding="utf-8").replace(
        '"coverage_floor": 0.6,', '"coverage_floor": 0.6, "neighbor_depth_tolerance_m": 1.0,'
    )
    config.write_text(payload, encoding="utf-8")

    run(data_root, "validate")

    table = read_features(Zones.at(data_root), "validation")
    (row,) = table[table["site_id"] == PROJECT_SITE].to_dict("records")
    assert not row["depth_comparable"]
    assert pd.isna(row["bias"])
    assert not pd.isna(row["correlation"])
    assert "refused" in run(data_root, "validate", "--dry-run").output


def test_a_dry_run_writes_nothing(data_root):
    ingest_deployment(data_root)
    land_reference(data_root, site_id="NDBC:LJAC1", depth_m=3.4, offset=1.0)

    result = run(data_root, "validate", "--dry-run")

    assert not Zones.at(data_root).feature_table("validation").exists()
    assert "nothing written" in result.output


def test_validate_is_regenerated_wholesale(data_root):
    """A pair the registry no longer declares must lose its row, which is why
    this is `replace_features` rather than a source-scoped write."""
    ingest_deployment(data_root)
    land_reference(data_root, site_id="NDBC:LJAC1", depth_m=3.4, offset=1.0)
    run(data_root, "validate")
    before = len(read_features(Zones.at(data_root), "validation"))

    run(data_root, "validate")

    assert len(read_features(Zones.at(data_root), "validation")) == before


def test_an_empty_zone_says_so_rather_than_writing_a_table(data_root):
    result = run(data_root, "validate")

    assert "nothing to validate" in result.output
    assert not Zones.at(data_root).feature_table("validation").exists()


def test_a_reference_with_no_rows_is_warned_about(data_root):
    """The registry names LJAC1 and 9410230 and neither has been fetched here.
    A deployment silently absent from the table is indistinguishable from one
    that agreed with everything."""
    ingest_deployment(data_root)

    result = run(data_root, "validate")

    assert "warning" in result.output
    assert "NDBC:LJAC1" in result.output
