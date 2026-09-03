"""`kelpcompare ingest --source delmar_mooring` end to end.

The parser has its own suite; what is here is what only the command can get
wrong. Two things, and both are about the depth contract.

`delmar_mooring` sets `READS_DEPTH_FROM_PAYLOAD`, so the CLI has to hand it the
registry's declared depth *set* rather than the scalar `depths_m` a fixed-depth
station gets. Getting that backwards is a `TypeError` at best and a silently
undeclared sensor at worst, and it is decided in `cli._ingest_window` rather
than in the module -- so it is tested from the command.

The other is that nine series arrive under one `site_id` from one payload, and
`depth_m` is part of the storage key. A melt that lost the mapping would write
nine series under one depth, which is indistinguishable in a Parquet file from a
mooring with one sensor.

The registry here is written rather than copied from `data/registry/`, so these
pin the mechanism and go on passing whether or not the committed record still
looks like this. That the committed one does is `test_registry.py`'s business.

Network access is forbidden (CLAUDE.md): `fetch_archive` and `fetch_realtime`
are replaced, and the URL each window would have asked for is read back off the
manifest, which the real `archive_url` still writes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.fetchers import delmar_mooring
from kelpcompare.fetchers.base import SourceUnavailable, new_payload
from kelpcompare.storage import Zones, read_observations

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = REPO_ROOT / "tests" / "fixtures" / "delmar_mooring"
FULL_STRING = FIX / "delmar_temperature_2019-01-01T00-01.csv"
SPARSE = FIX / "delmar_temperature_2010-06-01T00-01.csv"

DATASET = "delmar_temperature"
DECLARED = [1.0, 6.0, 15.0, 21.0, 32.0, 45.0, 57.0, 72.0, 90.0]

SITE = {
    "site_id": "SCCOOS:DELMAR",
    "name": "Del Mar shelf mooring (SIO/SCCOOS)",
    "operator": "delmar_mooring",
    "station_code": DATASET,
    "lat": 32.93,
    "lon": -117.32,
    "sensor_depths_m": {"sea_water_temperature": DECLARED},
    "measured_parameters": ["sea_water_temperature"],
}


@pytest.fixture
def data_root(tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "registry").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "data" / "registry" / "parameters.json", root / "registry")
    (root / "registry" / "sites.json").write_text(json.dumps({"sites": [SITE]}), encoding="utf-8")
    return root


@pytest.fixture
def offline(monkeypatch):
    """Serve a recorded payload for every window, and record what was asked."""
    asked: list[tuple[str, int | None]] = []

    def archive(dataset_id, year, *, session=None, validators=None):
        asked.append((dataset_id, year))
        return new_payload(
            delmar_mooring.SOURCE,
            dataset_id,
            f"{dataset_id}_{year}.csv",
            delmar_mooring.archive_url(dataset_id, year),
            (SPARSE if year <= 2015 else FULL_STRING).read_bytes(),
        )

    def realtime(dataset_id, *, session=None, validators=None):
        asked.append((dataset_id, None))
        raise SourceUnavailable(
            f"{delmar_mooring.realtime_url(dataset_id)} matched no rows; this mooring's "
            "record ends 2021-05-05 and it is not currently reporting"
        )

    monkeypatch.setattr(delmar_mooring, "fetch_archive", archive)
    monkeypatch.setattr(delmar_mooring, "fetch_realtime", realtime)
    return asked


def run(data_root: Path, *args: str):
    result = CliRunner().invoke(
        main, ["ingest", "--source", "delmar_mooring", "--data-root", str(data_root), *args]
    )
    assert result.exit_code == 0, result.output
    return result


def manifest(data_root: Path) -> dict:
    files = sorted((data_root / "raw" / "_manifests").glob("*.json"))
    assert len(files) == 1, f"expected one manifest, found {len(files)} (hard rule 7)"
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_one_payload_lands_nine_series_keyed_by_depth(data_root, offline):
    run(data_root, "--year", "2019")

    rows = read_observations(Zones(data_root))
    assert len(rows) == 36
    assert sorted(rows["depth_m"].unique()) == DECLARED
    assert set(rows["source"]) == {"delmar_mooring"}
    assert set(rows["site_id"]) == {"SCCOOS:DELMAR"}
    # Four timestamps at each of nine depths, not thirty-six at one.
    assert rows.groupby("depth_m")["timestamp"].nunique().unique().tolist() == [4]


def test_the_cli_hands_this_fetcher_the_declared_depth_set(data_root, offline):
    """`READS_DEPTH_FROM_PAYLOAD` picks the other depth contract in
    `cli._ingest_window`. A site declaring fewer depths than the payload serves
    must land fewer series and say which it refused."""
    (data_root / "registry" / "sites.json").write_text(
        json.dumps(
            {"sites": [{**SITE, "sensor_depths_m": {"sea_water_temperature": [1.0, 15.0]}}]}
        ),
        encoding="utf-8",
    )

    run(data_root, "--year", "2019")

    rows = read_observations(Zones(data_root))
    assert sorted(rows["depth_m"].unique()) == [1.0, 15.0]
    warnings = " ".join(manifest(data_root).get("warnings", []))
    assert "were NOT stored" in warnings


def test_an_empty_sensor_lands_flagged_missing_rather_than_absent(data_root, offline):
    """The 2010 window carries two of nine sensors. The other seven are outages
    at declared depths and stay in the record, because `pct_coverage` has to be
    able to see them."""
    run(data_root, "--year", "2010")

    rows = read_observations(Zones(data_root))
    assert len(rows) == 36
    missing = rows[rows["qc_flag"] == 9]
    assert sorted(missing["depth_m"].unique()) == [6.0, 21.0, 32.0, 45.0, 57.0, 72.0, 90.0]


def test_a_closed_record_reports_the_realtime_window_as_a_gap(data_root, offline):
    """The mooring stopped in 2021, so the rolling window is legitimately empty.
    Recorded as a gap and not fatal (docs/01 §5)."""
    run(data_root)

    entry = manifest(data_root)
    gaps = " ".join(entry.get("gaps", []))
    assert "matched no rows" in gaps
    assert "2021-05-05" in gaps


def test_the_manifest_records_the_url_the_bytes_came_from(data_root, offline):
    run(data_root, "--year", "2019")

    paths = [f.get("path", "") for f in manifest(data_root)["files"]]
    assert any("erddap.sccoos.org" in path and "2019-01-01" in path for path in paths)


def test_the_run_is_addressed_by_the_dataset_id(data_root, offline):
    run(data_root, "--year", "2019")

    assert offline == [(DATASET, 2019)]
