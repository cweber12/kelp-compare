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
        site_id="PROJ:YELLOW-BUOY",
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
