"""Run manifests: the audit trail ADR-002 offers in place of a scheduler UI.

What matters here is that a manifest records what a run *didn't* do as loudly as
what it did -- a quarantined file, a skipped source, a cadence gap. A run that
silently dropped an input would be the one failure the manifest exists to make
impossible.
"""

from __future__ import annotations

import json

from kelpcompare.adapters.base import Check
from kelpcompare.manifest import RunManifest, code_version, new_run_id
from kelpcompare.storage import Zones


def test_run_ids_sort_chronologically():
    """storage._dedupe breaks ties on this ordering, so it is load-bearing."""
    early = "20260824T120000000Z-ingest"
    late = "20260824T120000001Z-ingest"
    assert sorted([late, early]) == [early, late]
    assert new_run_id("ingest") > "20260101T000000000Z-ingest"


def test_a_run_id_names_its_command():
    assert new_run_id("features").endswith("Z-features")


def test_code_version_reports_the_sha_and_whether_the_tree_was_dirty():
    """A SHA alone overstates reproducibility if the tree had uncommitted work."""
    sha, dirty = code_version()
    assert sha is None or len(sha) == 40
    assert isinstance(dirty, bool)


def test_a_manifest_writes_where_docs_03_says(tmp_path):
    zones = Zones.at(tmp_path)
    manifest = RunManifest.start("ingest", run_id="20260824T120000000Z-ingest")
    path = manifest.write(zones)

    assert path == zones.manifests / "20260824T120000000Z-ingest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["command"] == "ingest"
    assert payload["started_at"] and payload["finished_at"]


def test_an_ingested_file_records_its_landing_and_row_counts(tmp_path):
    manifest = RunManifest.start("ingest", sources=["project"])
    manifest.add_file(
        "incoming/Tidbit_1.xlsx",
        "ingested",
        adapter="hobo_xlsx",
        provenance="original",
        serial="22506632",
        site_id="PROJ:TIDBIT-1",
        landed="raw/project_sensors/22506632/abc__Tidbit_1.xlsx",
        rows_in=3029,
        rows_out=3029,
        qc_flags={"2": 3022, "4": 7},
    )
    payload = json.loads(manifest.write(Zones.at(tmp_path)).read_text(encoding="utf-8"))

    entry = payload["files"][0]
    assert entry["outcome"] == "ingested"
    assert entry["rows_out"] == 3029
    assert entry["qc_flags"] == {"2": 3022, "4": 7}
    assert payload["counts"] == {"ingested": 1}
    assert payload["sources"] == ["project"]


def test_checks_are_recorded_verbatim_and_surface_as_warnings(tmp_path):
    """A skipped check is never silent (docs/06 s3)."""
    manifest = RunManifest.start("ingest")
    entry = manifest.add_file("edited.xlsx", "ingested", provenance="edited")
    entry.record_checks(
        (
            Check("details_statistics", "skipped", "hand-edited file"),
            Check("cadence_audit", "pass", "no gaps"),
            Check("registry_gate", "pass", "serial matched"),
        )
    )
    payload = json.loads(manifest.write(Zones.at(tmp_path)).read_text(encoding="utf-8"))

    recorded = payload["files"][0]
    assert [c["name"] for c in recorded["checks"]] == [
        "details_statistics",
        "cadence_audit",
        "registry_gate",
    ]
    assert recorded["warnings"] == ["details_statistics: hand-edited file"]


def test_a_quarantined_file_is_recorded_not_omitted(tmp_path):
    """Fail soft: the run continues, but the rejection is on the record."""
    manifest = RunManifest.start("ingest")
    manifest.add_file(
        "stranger.xlsx",
        "quarantined",
        quarantined_to="quarantine/stranger.xlsx",
        reason="no deployment record for serial 99999999",
    )
    manifest.add_file("good.xlsx", "ingested", rows_out=10)
    payload = json.loads(manifest.write(Zones.at(tmp_path)).read_text(encoding="utf-8"))

    assert payload["counts"] == {"quarantined": 1, "ingested": 1}
    rejected = payload["files"][0]
    assert rejected["quarantined_to"].endswith("stranger.xlsx")
    assert "99999999" in rejected["reason"]
    assert rejected["landed"] is None  # a quarantined file never lands in raw/


def test_gaps_and_warnings_are_separate_records(tmp_path):
    manifest = RunManifest.start("ingest")
    manifest.note_gap("NDBC LJAC1 returned no rows for 2026-06")
    manifest.note_warning("series 'Light' has no series_map entry; skipped")
    payload = json.loads(manifest.write(Zones.at(tmp_path)).read_text(encoding="utf-8"))

    assert payload["gaps"] == ["NDBC LJAC1 returned no rows for 2026-06"]
    assert payload["warnings"] == ["series 'Light' has no series_map entry; skipped"]


def test_writing_twice_keeps_the_first_finish_time(tmp_path):
    manifest = RunManifest.start("ingest").finish()
    finished = manifest.finished_at
    manifest.write(Zones.at(tmp_path))
    assert manifest.finished_at == finished


def test_a_started_run_is_running_and_claims_no_finish_time():
    """The state every run is in while it is still doing work."""
    manifest = RunManifest.start("ingest")
    assert manifest.status == "running"
    assert manifest.finished_at is None


def test_the_start_record_is_written_before_any_work(tmp_path):
    """What survives a process killed without unwinding (issue #115).

    A closed console window on Windows terminates without running `finally`, so
    this file is the only evidence such a run existed at all.
    """
    zones = Zones.at(tmp_path)
    manifest = RunManifest.start("ingest", run_id="20260824T120000000Z-ingest")
    path = manifest.write_start(zones)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "running"
    assert payload["started_at"] and payload["finished_at"] is None


def test_writing_the_terminal_record_completes_a_running_run(tmp_path):
    """The report functions rely on `write` finishing the run for them."""
    zones = Zones.at(tmp_path)
    manifest = RunManifest.start("ingest", run_id="20260824T120000000Z-ingest")
    manifest.write_start(zones)
    path = manifest.write(zones)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["finished_at"]
    # Overwritten in place: one run, one manifest, whatever happened to it.
    assert list(zones.manifests.glob("*.json")) == [path]


def test_an_interrupted_run_is_never_promoted_to_completed(tmp_path):
    """`write` is also the unwind path's own call, and must not undo it.

    Promoting it would put back exactly the lie the status field exists to
    prevent: rows on disk under a run the manifest claims finished cleanly.
    """
    zones = Zones.at(tmp_path)
    manifest = RunManifest.start("ingest")
    manifest.add_file("2019.nc", "ingested", rows_out=4380)
    manifest.interrupt()
    payload = json.loads(manifest.write(zones).read_text(encoding="utf-8"))

    assert payload["status"] == "interrupted"
    assert payload["finished_at"]
    # The work that did complete is still described -- that is the point of
    # writing the record at all.
    assert payload["counts"] == {"ingested": 1}
