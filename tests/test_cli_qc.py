"""`kelpcompare qc`, end to end (docs/04 s1, docs/03).

Every test runs the real commands against a `tmp_path` data root, so what is
asserted is what an operator would get: ingest first, then qc over what ingest
left. Nothing here touches the repo's own `data/` beyond copying the committed
registry -- raw is append-only forever (hard rule 1).

The numbers come from the reviewed deployment, and docs/06 s5 check 6 predicted
them before this stage existed: gross range flags nothing in-water, and the
install transient is condemned twice over.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from kelpcompare import storage
from kelpcompare.cli import main
from kelpcompare.qc.flags import parse_tests
from kelpcompare.storage import FLAG_FAIL, FLAG_NOT_EVALUATED, FLAG_PASS, Zones

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
ORIGINAL = FIX / "Tidbit_1__22506632__2026-08-01_07_44_27_PDT__Data_PDT_.xlsx"
REGISTRY_SOURCE = REPO_ROOT / "data" / "registry"


@pytest.fixture
def data_root(tmp_path) -> Path:
    """A docs/03 data root with the committed registry and an empty incoming/."""
    root = tmp_path / "data"
    (root / "registry").mkdir(parents=True)
    for name in ("sites.json", "parameters.json"):
        shutil.copy2(REGISTRY_SOURCE / name, root / "registry" / name)
    (root / "raw" / "project_sensors" / "incoming").mkdir(parents=True)
    return root


def run(data_root: Path, command: str, *extra: str, expect: int = 0):
    """Invoke the real command, insisting it exited as expected.

    The insistence matters: without it a test asserting on rows that qc should
    have changed would pass just as well against a qc that does not exist.
    """
    result = CliRunner().invoke(main, [command, "--data-root", str(data_root), *extra])
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    assert result.exit_code == expect, f"{command} exited {result.exit_code}: {result.output}"
    return result


def ingest(data_root: Path, *extra: str):
    shutil.copy2(ORIGINAL, data_root / "raw" / "project_sensors" / "incoming" / ORIGINAL.name)
    return run(data_root, "ingest", "--source", "project", *extra)


def stored(data_root: Path):
    return storage.read_observations(Zones.at(data_root), source="project")


def manifests(data_root: Path) -> list[dict]:
    files = sorted((data_root / "raw" / "_manifests").glob("*.json"))
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def qc_manifest(data_root: Path) -> dict:
    runs = [m for m in manifests(data_root) if m["command"] == "qc"]
    assert len(runs) == 1, f"expected one qc manifest, found {len(runs)}"
    return runs[0]


def partitions(data_root: Path) -> list[Path]:
    return sorted((data_root / "observations" / "source=project" / "year=2026").glob("part-*"))


# --------------------------------------------------------------------------
# The reference deployment -- docs/06 s5 check 6
# --------------------------------------------------------------------------


def test_qc_passes_every_in_water_reading(data_root):
    ingest(data_root)
    assert run(data_root, "qc").exit_code == 0

    rows = stored(data_root)
    usable = rows.loc[rows["qc_flag"] <= 2]
    assert len(usable) == 3022
    assert set(usable["qc_flag"]) == {FLAG_PASS}
    assert round(usable["value"].min(), 2) == 17.76


def test_qc_never_removes_a_row(data_root):
    """Hard rule 4: flags, never deletions."""
    ingest(data_root)
    run(data_root, "qc")
    assert len(stored(data_root)) == 3029


def test_the_out_of_window_readings_are_not_relaxed(data_root):
    """A plausible temperature measured in air is still not a measurement."""
    ingest(data_root)
    run(data_root, "qc")

    rows = stored(data_root)
    failed = rows.loc[rows["qc_flag"] == FLAG_FAIL]
    assert len(failed) == 7
    assert all(parse_tests(t)["gross_range"] == "pass" for t in failed["qc_tests"])


def test_the_install_transient_is_caught_by_two_tests_independently(data_root):
    ingest(data_root)
    run(data_root, "qc")

    rows = stored(data_root)
    transient = rows.loc[rows["value"].idxmin()]
    assert round(transient["value"], 2) == 14.78
    assert parse_tests(transient["qc_tests"]) == {
        "deployment_window": "fail",
        "gross_range": "pass",
        "spike": "fail",
        "rate_of_change": "suspect",
    }


# --------------------------------------------------------------------------
# What qc does to the zone
# --------------------------------------------------------------------------


def test_qc_leaves_one_partition_named_for_the_run_that_flagged_it(data_root):
    ingest(data_root)
    run(data_root, "qc")

    (part,) = partitions(data_root)
    assert part.name.endswith("-qc.parquet")


def test_qc_keeps_the_ingest_run_that_fetched_each_row(data_root):
    """The partition name records who flagged; fetch_run_id records who fetched."""
    ingest(data_root)
    run(data_root, "qc")
    assert set(stored(data_root)["fetch_run_id"]) == {_ingest_run_id(data_root)}


def test_running_qc_twice_changes_nothing(data_root):
    ingest(data_root)
    run(data_root, "qc")
    first = stored(data_root).sort_values("timestamp").reset_index(drop=True)

    run(data_root, "qc")
    second = stored(data_root).sort_values("timestamp").reset_index(drop=True)
    assert first["qc_flag"].equals(second["qc_flag"])
    assert first["qc_tests"].equals(second["qc_tests"])


def test_reingesting_resets_the_rows_it_overwrites_and_qc_restores_them(data_root):
    """docs/03: a re-ingested row has not been evaluated since it was fetched."""
    ingest(data_root)
    run(data_root, "qc")
    assert set(stored(data_root)["qc_flag"]) == {FLAG_PASS, FLAG_FAIL}

    ingest(data_root)
    reset = stored(data_root)
    assert set(reset["qc_flag"]) == {FLAG_NOT_EVALUATED, FLAG_FAIL}

    run(data_root, "qc")
    assert set(stored(data_root)["qc_flag"]) == {FLAG_PASS, FLAG_FAIL}


# --------------------------------------------------------------------------
# The manifest -- hard rule 7, docs/03
# --------------------------------------------------------------------------


def test_the_run_manifest_records_the_flag_histogram(data_root):
    ingest(data_root)
    run(data_root, "qc")

    payload = qc_manifest(data_root)
    assert payload["command"] == "qc"
    assert payload["sources"] == ["project"]
    assert payload["qc_flags"] == {str(FLAG_PASS): 3022, str(FLAG_FAIL): 7}


def test_the_run_manifest_records_each_evaluated_series(data_root):
    ingest(data_root)
    run(data_root, "qc")

    (series,) = qc_manifest(data_root)["series"]
    assert series["source"] == "project"
    assert series["site_id"] == "PROJ:YELLOW-BUOY"
    assert series["parameter"] == "sea_water_temperature"
    assert series["rows"] == 3029
    assert set(series["tests"]) == {"gross_range", "spike", "rate_of_change"}
    assert series["qc_flags"] == {str(FLAG_PASS): 3022, str(FLAG_FAIL): 7}


# --------------------------------------------------------------------------
# Options and edges
# --------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_at_all(data_root):
    ingest(data_root)
    before = {p.name for p in partitions(data_root)}

    result = run(data_root, "qc", "--dry-run")
    assert result.exit_code == 0
    assert "dry run" in result.output
    assert {p.name for p in partitions(data_root)} == before
    assert set(stored(data_root)["qc_flag"]) == {FLAG_NOT_EVALUATED, FLAG_FAIL}
    assert not [m for m in manifests(data_root) if m["command"] == "qc"]


def test_a_dry_run_still_reports_what_it_would_flag(data_root):
    ingest(data_root)
    assert "3022" in run(data_root, "qc", "--dry-run").output


def test_a_zone_with_nothing_in_it_is_not_an_error(data_root):
    result = run(data_root, "qc")
    assert result.exit_code == 0
    assert "nothing to evaluate" in result.output
    assert not (data_root / "raw" / "_manifests").exists()


def test_one_source_can_be_named(data_root):
    ingest(data_root)
    assert run(data_root, "qc", "--source", "project").exit_code == 0
    assert set(stored(data_root)["qc_flag"]) == {FLAG_PASS, FLAG_FAIL}


def test_naming_a_source_with_no_stored_rows_is_not_an_error(data_root):
    ingest(data_root)
    result = run(data_root, "qc", "--source", "ndbc")
    assert result.exit_code == 0
    assert set(stored(data_root)["qc_flag"]) == {FLAG_NOT_EVALUATED, FLAG_FAIL}


def _ingest_run_id(data_root: Path) -> str:
    (payload,) = [m for m in manifests(data_root) if m["command"] == "ingest"]
    return payload["run_id"]


def test_a_source_whose_stored_verdicts_are_corrupt_fails_loudly(data_root):
    """`parse_tests` refuses a verdict it cannot read rather than dropping it.

    Corruption cannot come from the pipeline, so it is written here directly.
    What matters is the response: the run records it, exits non-zero, and leaves
    the rows exactly as it found them rather than re-flagging from a half-read
    record.
    """
    ingest(data_root)
    (part,) = partitions(data_root)
    rows = storage.read_observations(Zones.at(data_root), source="project")
    rows.loc[0, "qc_tests"] = "deployment_window"  # no status
    rows.to_parquet(part, index=False)

    result = run(data_root, "qc", expect=1)
    assert "deployment_window" in result.output
    assert set(stored(data_root)["qc_flag"]) == {FLAG_NOT_EVALUATED, FLAG_FAIL}


def test_a_source_that_cannot_be_read_does_not_cost_the_run_the_ones_that_can(data_root):
    """docs/02 fail-soft, and hard rule 7: recorded and stepped over, with a
    manifest either way.

    `ndbc` sorts before `project`, so a read failure that escaped would abort
    the run before the source that was fine had been evaluated at all -- and
    take the manifest with it, leaving no record that the run happened.
    """
    ingest(data_root)
    unreadable = data_root / "observations" / "source=ndbc" / "year=2026"
    unreadable.mkdir(parents=True)
    (unreadable / "part-20260101T000000000Z-ingest.parquet").write_text("not a parquet file")

    result = run(data_root, "qc", expect=1)
    assert "ndbc" in result.output
    assert set(stored(data_root)["qc_flag"]) == {FLAG_PASS, FLAG_FAIL}
    assert any("ndbc" in warning for warning in qc_manifest(data_root)["warnings"])


def test_a_partition_left_holding_two_files_does_not_change_what_qc_stores(data_root):
    """Issue #3, end to end.

    Before the read deduped, this run reported 6058 rows for a 3029-row series
    and stored three verdicts that a clean run does not produce -- the install
    transient came back `spike:suspect;rate_of_change:pass` instead of
    `spike:fail;rate_of_change:suspect`, silently retiring the doc 06 s5 check 6
    redundancy. Exit code was 0 throughout.
    """
    ingest(data_root)
    run(data_root, "qc")
    clean = stored(data_root).sort_values("timestamp").reset_index(drop=True)

    (part,) = partitions(data_root)
    (part.parent / "part-20260101T000000000Z-ingest.parquet").write_bytes(part.read_bytes())
    assert len(partitions(data_root)) == 2

    result = run(data_root, "qc")
    assert "3029 rows" in result.output

    again = stored(data_root).sort_values("timestamp").reset_index(drop=True)
    assert len(again) == 3029
    assert again["qc_flag"].equals(clean["qc_flag"])
    assert again["qc_tests"].equals(clean["qc_tests"])
    assert len(partitions(data_root)) == 1
