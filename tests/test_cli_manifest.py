"""How a run's manifest says the run ended (docs/03 "Run manifests", hard rule 7).

A run that stopped partway used to leave rows in `observations/` stamped with a
`fetch_run_id` that no manifest described -- the bytes traceable and the run not
(issue #115). These drive the real commands against a `tmp_path` data root, with
the network replaced at the same seam the other CLI tests use, and stop them
partway to see what they leave behind. Nothing here reaches NOAA.

`mur_sst` is the source under test for no reason but that its beds give a run
several windows to be interrupted between; the behaviour belongs to the CLI
boundary, not to any one source, so `qc` is driven here too.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.fetchers import mur_sst
from kelpcompare.fetchers.base import new_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures" / "mur_sst"
REGISTRY_SOURCE = REPO_ROOT / "data" / "registry"
RECORDED = {
    "KELP_DEL-MAR": FIX / "del_mar_2020-07-01_03_excerpt.csv",
    "KELP_LA-JOLLA": FIX / "la_jolla_2020-07-01_03_excerpt.csv",
}
BOTH = ("SST:DEL-MAR", "SST:LA-JOLLA")


@pytest.fixture
def data_root(tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "registry").mkdir(parents=True)
    for name in ("sites.json", "parameters.json", "polygons.geojson"):
        shutil.copy2(REGISTRY_SOURCE / name, root / "registry" / name)
    return root


def serve(monkeypatch, *, interrupt_after: int | None = None, watcher=None):
    """Recorded payloads in place of the network, optionally stopping partway.

    `interrupt_after` raises `KeyboardInterrupt` from inside the fetch once that
    many beds have been served, which is where a Ctrl+C on a long backfill
    actually lands: between windows, after some rows are already on disk.
    """
    served = []

    def fetch(bounds, year=None, *, station, session=None, validators=None):
        if watcher is not None:
            watcher(len(served))
        if interrupt_after is not None and len(served) >= interrupt_after:
            raise KeyboardInterrupt
        served.append(station)
        path = RECORDED[station]
        return new_payload(
            mur_sst.SOURCE, station, path.name, f"file://{path.name}", path.read_bytes()
        )

    monkeypatch.setattr(mur_sst, "fetch_realtime", lambda b, **kw: fetch(b, None, **kw))
    monkeypatch.setattr(mur_sst, "fetch_archive", fetch)
    return served


def ingest(data_root: Path, *extra: str, stations: tuple[str, ...] = BOTH):
    scope = [arg for station in stations for arg in ("--station", station)]
    return CliRunner().invoke(
        main, ["ingest", "--source", "mur_sst", "--data-root", str(data_root), *scope, *extra]
    )


def manifests(data_root: Path, command: str = "") -> list[dict]:
    found = sorted((data_root / "raw" / "_manifests").glob(f"*{command}*.json"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in found]


def only(data_root: Path, command: str = "") -> dict:
    found = manifests(data_root, command)
    assert len(found) == 1, f"expected one manifest, found {len(found)}"
    return found[0]


# --------------------------------------------------------------------------
# The record a run leaves behind
# --------------------------------------------------------------------------


def test_a_run_that_finishes_is_recorded_as_completed(data_root, monkeypatch):
    serve(monkeypatch)
    assert ingest(data_root).exit_code == 0

    record = only(data_root)
    assert record["status"] == "completed"
    assert record["finished_at"]


def test_an_interrupted_run_still_leaves_a_manifest(data_root, monkeypatch):
    """The whole of issue #115: rows on disk, and a run that can be traced."""
    serve(monkeypatch, interrupt_after=1)
    result = ingest(data_root)

    # Click turns a KeyboardInterrupt into its own abort, which is what an
    # operator sees at the terminal after Ctrl+C.
    assert result.exit_code == 1
    assert "Aborted!" in result.output

    record = only(data_root)
    assert record["status"] == "interrupted"
    assert record["finished_at"]


def test_the_interrupted_manifest_describes_the_work_that_completed(data_root, monkeypatch):
    """An honest partial record, not an empty one: those rows exist."""
    serve(monkeypatch, interrupt_after=1)
    ingest(data_root)

    record = only(data_root)
    ingested = [f for f in record["files"] if f["outcome"] == "ingested"]
    assert len(ingested) == 1
    assert ingested[0]["rows_out"] > 0
    assert ingested[0]["partitions"]


def test_the_running_record_is_on_disk_before_the_run_ends(data_root, monkeypatch):
    """What a process killed without unwinding leaves -- a closed console window
    on Windows never runs `finally`, so this record is the only trace."""
    seen = []

    def watch(_served):
        seen.extend(m["status"] for m in manifests(data_root))

    serve(monkeypatch, watcher=watch)
    assert ingest(data_root).exit_code == 0

    assert seen and set(seen) == {"running"}


def test_an_interrupted_dry_run_writes_no_manifest(data_root, monkeypatch):
    """`--dry-run` promises nothing written, and an interruption does not
    turn that into a file the completed run would have refused to write."""
    serve(monkeypatch, interrupt_after=1)
    ingest(data_root, "--dry-run")

    assert manifests(data_root) == []


def test_a_run_whose_input_failed_is_completed_not_interrupted(data_root, monkeypatch):
    """`_report` raises SystemExit *after* writing, and that unwind must not be
    read as an interruption: the run reached its end and said so."""
    serve(monkeypatch)

    def explode(*args, **kwargs):
        raise ValueError("unparseable grid")

    monkeypatch.setattr(mur_sst, "parse", explode)
    result = ingest(data_root)

    assert result.exit_code == 1  # a failed input still sets the code
    record = only(data_root)
    assert record["status"] == "completed"
    assert [f["outcome"] for f in record["files"]] == ["failed", "failed"]


def test_an_interrupted_qc_run_also_leaves_its_manifest(data_root, monkeypatch):
    """The boundary is the CLI's, not one command's."""
    serve(monkeypatch)
    assert ingest(data_root).exit_code == 0

    def explode(*args, **kwargs):
        raise KeyboardInterrupt

    # Patched where `cli` bound it, not where `qc` defines it: the command
    # imported the name, so rebinding the module attribute would miss.
    monkeypatch.setattr("kelpcompare.cli.evaluate", explode)
    result = CliRunner().invoke(main, ["qc", "--data-root", str(data_root)])

    assert result.exit_code == 1
    assert only(data_root, "-qc")["status"] == "interrupted"
