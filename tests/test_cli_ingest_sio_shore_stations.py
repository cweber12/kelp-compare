"""`kelpcompare ingest --source sio_shore_stations`, end to end (docs/02, docs/03).

The real command against a `tmp_path` data root seeded with the committed
registry, driven with the Click runner. Nothing is stubbed: this source cannot
be pulled at all, so there is no network seam to replace and the whole path from
a dropped file to a landing, a partition and a manifest is the code an operator
runs.

The third shape of file drop, and what is worth watching is how it differs from
the other two. A HOBO export is matched to a deployment by the serial inside it;
a Kelp Watch export names nothing and is claimed by filename; this one declares
its own position, its own station and its own archive date, so the registry is
checked *against* the file. Three ways for that to disagree, and each of them
quarantines rather than landing a century of readings in the wrong place.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.fetchers import sio_shore_stations as sio

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_SOURCE = REPO_ROOT / "data" / "registry"
FIX = Path(__file__).parent / "fixtures" / "sio_shore_stations"
PINNED = FIX / "lajolla_temp_excerpt.csv"
OLDER = FIX / "lajolla_temp_2020_archive_excerpt.csv"
EDGES = FIX / "lajolla_temp_edge-cases.csv"

SITE = "SIO:LAJOLLA-PIER"


@pytest.fixture
def data_root(tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "registry").mkdir(parents=True)
    for name in ("sites.json", "parameters.json", "features.json", "polygons.geojson"):
        shutil.copy2(REGISTRY_SOURCE / name, root / "registry" / name)
    return root


def drop(data_root: Path, *sources: Path, rename: str | None = None) -> Path:
    """Put archives where the operator puts them."""
    incoming = data_root / "raw" / sio.SOURCE / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    for source in sources:
        shutil.copy2(source, incoming / (rename or source.name))
    return incoming


def amend(data_root: Path, site_id: str, **fields) -> None:
    """Edit one site record in the copied registry, the way an operator would."""
    path = data_root / "registry" / "sites.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for site in payload["sites"]:
        if site["site_id"] == site_id:
            for key, value in fields.items():
                if value is None:
                    site.pop(key, None)
                else:
                    site[key] = value
            break
    else:  # pragma: no cover -- the fixture registry has this site
        raise AssertionError(f"{site_id} is not in the committed registry")
    path.write_text(json.dumps(payload), encoding="utf-8")


def run(data_root: Path, *extra: str):
    result = CliRunner().invoke(
        main, ["ingest", "--source", sio.SOURCE, "--data-root", str(data_root), *extra]
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def manifest(data_root: Path) -> dict:
    files = sorted((data_root / "raw" / "_manifests").glob("*.json"))
    assert len(files) == 1, f"expected one manifest, found {len(files)}"
    return json.loads(files[0].read_text())


def entry(data_root: Path) -> dict:
    (only,) = manifest(data_root)["files"]
    return only


def landings(data_root: Path) -> list[Path]:
    root = data_root / "raw" / sio.SOURCE
    return sorted(p for p in root.rglob("*.csv") if "incoming" not in p.parts)


def stored(data_root: Path) -> pd.DataFrame:
    parts = sorted((data_root / "observations").rglob("*.parquet"))
    if not parts:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)


def checks(data_root: Path) -> dict[str, str]:
    return {c["name"]: c["status"] for c in entry(data_root)["checks"]}


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_pinned_archive_lands_raw_and_writes_observations(data_root):
    drop(data_root, PINNED)
    result = run(data_root)

    assert result.exit_code == 0, result.output
    assert "ingested" in result.output

    record = entry(data_root)
    assert record["outcome"] == "ingested"
    assert record["site_id"] == SITE
    assert record["fetcher"] == sio.FETCHER_NAME
    assert (record["rows_in"], record["rows_out"]) == (33, 54)
    assert checks(data_root) == {
        "site_match": "pass",
        "archive_pin": "pass",
        "sensor_depths": "pass",
    }


def test_the_landing_is_segregated_by_archive_date(data_root):
    """Each download is a cumulative snapshot of the whole record, so two of them
    must never be read as one series -- the directory is what makes mixing them
    impossible rather than merely discouraged, as `ver{n}` does for Kelp Watch."""
    drop(data_root, PINNED)
    run(data_root)

    (landed,) = landings(data_root)
    assert landed.parent.name == "2026-06-30"
    assert landed.name.endswith("__lajolla_temp_excerpt.csv")
    assert landed.read_bytes() == PINNED.read_bytes(), "landed untouched (hard rule 1)"


def test_the_original_stays_in_the_drop_directory(data_root):
    """Copied, never moved: the drop directory sits inside `raw/` and hard rule 1
    forbids deleting from it -- the operator clears `incoming/`, not the pipeline."""
    incoming = drop(data_root, PINNED)
    run(data_root)
    assert (incoming / PINNED.name).exists()


def test_re_dropping_the_same_bytes_is_a_no_op(data_root):
    """Content-addressed, so a second run of one archive rewrites its partitions
    with the same rows rather than doubling them."""
    drop(data_root, PINNED)
    run(data_root)
    first = stored(data_root)

    run(data_root)
    assert len(landings(data_root)) == 1
    assert len(stored(data_root)) == len(first) == 54


def test_the_manifest_records_the_archive_as_the_dataset_revision(data_root):
    """The pin is what makes a landing traceable to a citable dataset, so it goes
    in the audit trail beside the rows it produced (docs/03)."""
    drop(data_root, PINNED)
    run(data_root)
    assert entry(data_root)["dataset_revision"] == "2026-06-30"


def test_the_stored_rows_are_the_two_depths_under_this_source(data_root):
    drop(data_root, PINNED)
    run(data_root)

    frame = stored(data_root)
    assert set(frame.source) == {sio.SOURCE}
    assert set(frame.site_id) == {SITE}
    assert sorted(set(frame.depth_m)) == [0.5, 5.0]
    assert set(frame.parameter) == {"sea_water_temperature"}
    assert frame.fetch_run_id.nunique() == 1


def test_the_operator_is_told_the_timestamps_are_a_convention(data_root):
    """The headline decision of this source, so it is promoted to a run-level
    warning where Kelp Watch's always-firing notices deliberately are not: an
    operator reading a century of readings should be told that two thirds of the
    timestamps were assigned rather than recorded."""
    drop(data_root, PINNED)
    result = run(data_root)

    assert "10:38 PST" in result.output
    assert "sample_time" in result.output
    assert result.exit_code == 0, "a documented convention is not a failure"


def test_dry_run_writes_nothing_at_all(data_root):
    drop(data_root, PINNED)
    result = run(data_root, "--dry-run")

    assert result.exit_code == 0
    assert landings(data_root) == []
    assert stored(data_root).empty
    assert not (data_root / "raw" / "_manifests").exists()


def test_nothing_dropped_is_not_an_error(data_root):
    result = run(data_root)
    assert result.exit_code == 0
    assert "nothing to ingest" in result.output


# --------------------------------------------------------------------------
# Three ways the registry and the file can disagree
# --------------------------------------------------------------------------


def test_an_archive_from_another_snapshot_is_quarantined(data_root):
    """The pin exists to be checked, and this file declares 2022-07-07 where the
    registry pins 2026-06-30. Landing it would interleave two snapshots of one
    cumulative record."""
    drop(data_root, OLDER)
    result = run(data_root)

    record = entry(data_root)
    assert record["outcome"] == "quarantined"
    assert "archive_pin" in record["reason"]
    assert "2022-07-07" in record["reason"] and "2026-06-30" in record["reason"]
    assert checks(data_root)["site_match"] == "pass", "it is the right station, wrong archive"

    assert (data_root / "quarantine" / OLDER.name).exists()
    assert landings(data_root) == [], "quarantine is deliberately not raw/"
    assert stored(data_root).empty
    assert result.exit_code == 0, "a quarantine is the system working (docs/02)"


def test_an_unpinned_site_cannot_accept_a_file(data_root):
    """Per site rather than per run, unlike Kelp Watch's single global revision:
    the file is recorded with its reason, which is more use than a run that ends
    before saying which file it was about."""
    amend(data_root, SITE, archive=None)
    drop(data_root, PINNED)
    run(data_root)

    record = entry(data_root)
    assert record["outcome"] == "quarantined"
    assert "pins no archive.archived" in record["reason"]
    assert stored(data_root).empty


def test_an_archive_from_a_station_nobody_has_registered_is_quarantined(data_root):
    """The docs/06 s5 check-4 gate for this source. The program runs several
    stations in one format, and a file is claimed by the position it declares --
    so moving the registry's position away from the pier is the same thing as
    dropping another station's file."""
    amend(data_root, SITE, lat=33.6, lon=-117.9)
    drop(data_root, PINNED)
    run(data_root)

    record = entry(data_root)
    assert record["outcome"] == "quarantined"
    assert "site_match" in record["reason"]
    assert "no sio_shore_stations site in the registry is there" in record["reason"]
    assert record["site_id"] is None, "nothing was attributed"


def test_two_sites_at_one_position_quarantine_rather_than_pick_one(data_root):
    """Two stations cannot be in one place; picking whichever came first would
    hide a registry error under a century of readings."""
    path = data_root / "registry" / "sites.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    original = next(s for s in payload["sites"] if s["site_id"] == SITE)
    payload["sites"].append({**original, "site_id": "SIO:LAJOLLA-PIER-COPY"})
    path.write_text(json.dumps(payload), encoding="utf-8")

    drop(data_root, PINNED)
    run(data_root)

    record = entry(data_root)
    assert record["outcome"] == "quarantined"
    assert "matches 2 site records" in record["reason"]


def test_a_depth_the_registry_has_not_reviewed_is_quarantined(data_root):
    """`depth_m` is part of OBSERVATION_KEY, so a series landed at an unreviewed
    depth is permanent -- reviewed before the first landing, not after."""
    amend(data_root, SITE, sensor_depths_m={"sea_water_temperature": [0.5, 6.0]})
    drop(data_root, PINNED)
    run(data_root)

    record = entry(data_root)
    assert record["outcome"] == "quarantined"
    assert "sensor_depths" in record["reason"]
    assert "bottom 5 m" in record["reason"]
    assert stored(data_root).empty


# --------------------------------------------------------------------------
# Files that are not this source's
# --------------------------------------------------------------------------


def test_the_salinity_file_in_the_same_download_is_skipped_not_misread(data_root):
    """It shares the preamble, the legend and seven of the nine columns, and it
    arrives in the same download as the file we do want."""
    incoming = drop(data_root, PINNED)
    salt = incoming / "LaJolla_SALT_1916-202603.csv"
    text = PINNED.read_bytes().decode("utf-8-sig")
    salt.write_bytes(
        text.replace(
            "SURF_TEMP_C,SURF_FLAG,BOT_TEMP_C,BOT_FLAG",
            "SURF_SAL_PSU,SURF_FLAG,BOT_SAL_PSU,BOT_FLAG",
        ).encode("utf-8-sig")
    )

    result = run(data_root)
    outcomes = {Path(f["path"]).name: f["outcome"] for f in manifest(data_root)["files"]}

    assert outcomes[salt.name] == "skipped"
    assert outcomes[PINNED.name] == "ingested", "one bad file must not cost the good one"
    assert result.exit_code == 0
    assert len(stored(data_root)) == 54


def test_a_file_nothing_recognises_is_skipped(data_root):
    incoming = drop(data_root)
    (incoming / "notes.txt").write_text("dropped here by mistake\n", encoding="utf-8")

    run(data_root)
    assert entry(data_root)["outcome"] == "skipped"


def test_a_corrupt_archive_fails_loudly_without_ending_the_run(data_root):
    """A payload that arrived and could not be parsed is a bug or a format
    change, and both need a human -- so unlike a quarantine it sets the exit
    code (docs/02 fail-soft rules)."""
    incoming = drop(data_root, PINNED)
    broken = incoming / "LaJolla_TEMP_1916-202512.csv"
    text = PINNED.read_bytes().decode("utf-8-sig")
    broken.write_bytes(text.replace("1916,8,23,", "1916,8,22,", 1).encode("utf-8-sig"))

    result = run(data_root)
    outcomes = {Path(f["path"]).name: f["outcome"] for f in manifest(data_root)["files"]}

    assert outcomes[broken.name] == "failed"
    assert outcomes[PINNED.name] == "ingested"
    assert result.exit_code == 1


# --------------------------------------------------------------------------
# The options that do not apply here
# --------------------------------------------------------------------------


def test_station_and_year_are_refused_rather_than_ignored(data_root):
    """`--year 2023` silently doing nothing is how an operator comes to believe
    they have a year of data they never ingested."""
    for option in (["--station", "LaJolla"], ["--year", "2023"]):
        result = run(data_root, *option)
        assert result.exit_code != 0
        assert "file-drop source" in str(result.exception) or "file-drop source" in result.output


def test_a_path_can_name_one_archive_directly(data_root, tmp_path):
    """So an operator can ingest from wherever they unzipped the download."""
    elsewhere = tmp_path / "downloads"
    elsewhere.mkdir()
    shutil.copy2(PINNED, elsewhere / PINNED.name)

    result = run(data_root, "--path", str(elsewhere / PINNED.name))
    assert result.exit_code == 0
    assert entry(data_root)["outcome"] == "ingested"
    assert len(landings(data_root)) == 1


def test_the_source_is_offered_in_the_help_text(data_root):
    """Listed from the registries rather than a literal, so adding a source
    cannot leave the help claiming it does not exist."""
    result = CliRunner().invoke(main, ["ingest", "--help"])
    assert sio.SOURCE in result.output


# --------------------------------------------------------------------------
# The flag-5 decision, the first time it ever fires
# --------------------------------------------------------------------------


def test_a_flag_five_reading_lands_failed_and_says_so_on_the_console(data_root):
    """Zero rows in all fourteen recorded snapshots. The run that first exercises
    this decision should put it in front of the operator, not apply it silently."""
    drop(data_root, EDGES)
    result = run(data_root)

    assert entry(data_root)["outcome"] == "ingested"
    assert "source flag 5" in result.output
    assert "2024-01-03" in result.output

    frame = stored(data_root)
    failed = frame.loc[frame.qc_flag == 4]
    assert len(failed) == 2, "one surface, one bottom"
    assert failed.value.notna().all(), "kept on the record, not deleted (hard rule 4)"
