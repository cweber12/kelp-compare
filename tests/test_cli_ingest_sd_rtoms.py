"""`kelpcompare ingest --source sd_rtoms` where one site spans two datasets.

The RTOMS parser has its own suite; what is here is the thing only the command
can get wrong. City of San Diego publishes Point Loma as two ERDDAP datasets
with an overlap, and in the overlap they agree on a reading and disagree on the
depth they file it under (docs/02). Landing both over one window would store
one reading twice under two permanent names, so the registry gives each dataset
a window and the command has to honour it (docs/03, "A station's record may span
more than one dataset").

The registry these cases run against is written here rather than copied from
`data/registry/`, so they pin the *mechanism* and go on passing whether or not
any real site still spans two datasets. That the committed Point Loma record
does is pinned in `test_registry.py`, where it is a fact about the data.

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
from kelpcompare.fetchers import sd_rtoms
from kelpcompare.fetchers.base import SourceUnavailable, new_payload
from kelpcompare.storage import Zones, read_observations

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = REPO_ROOT / "tests" / "fixtures" / "sd_rtoms"
HISTORIC = FIX / "point-loma-ocean-outfall-histori_2020-06-01T00-01.csv"

OLDER = "point-loma-ocean-outfall-histori"
CURRENT = "point-loma-ocean-outfall-real-ti"
BOUNDARY = "2021-11-04T00:00:00Z"

SITE = {
    "site_id": "SDRTOMS:PLOO",
    "name": "Point Loma Ocean Outfall RTOMS mooring",
    "operator": "sd_rtoms",
    "station_code": CURRENT,
    "lat": 32.66996,
    "lon": -117.32676,
    "predecessor_datasets": [{"station_code": OLDER, "covers_until": BOUNDARY}],
    "sensor_depths_m": {"sea_water_temperature": [1.0, 10.0, 20.0, 45.0, 60.0, 75.0, 89.0]},
    "measured_parameters": ["sea_water_temperature"],
}


def registry_with(*sites: dict, root: Path) -> Path:
    (root / "registry").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "data" / "registry" / "parameters.json", root / "registry")
    (root / "registry" / "sites.json").write_text(
        json.dumps({"sites": list(sites)}), encoding="utf-8"
    )
    return root


@pytest.fixture
def data_root(tmp_path) -> Path:
    return registry_with(SITE, root=tmp_path / "data")


@pytest.fixture
def offline(monkeypatch):
    """Serve the recorded Point Loma payload for every window, and record the asks."""
    asked: list[tuple[str, int | None, str | None, str | None]] = []

    def archive(dataset_id, year, *, session=None, validators=None, since=None, until=None):
        asked.append((dataset_id, year, since, until))
        return new_payload(
            sd_rtoms.SOURCE,
            dataset_id,
            f"{dataset_id}_{year}.csv",
            sd_rtoms.archive_url(dataset_id, year, since=since, until=until),
            HISTORIC.read_bytes(),
        )

    def realtime(dataset_id, *, session=None, validators=None):
        asked.append((dataset_id, None, None, None))
        return new_payload(
            sd_rtoms.SOURCE,
            dataset_id,
            f"{dataset_id}_realtime.csv",
            sd_rtoms.realtime_url(dataset_id),
            HISTORIC.read_bytes(),
        )

    monkeypatch.setattr(sd_rtoms, "fetch_archive", archive)
    monkeypatch.setattr(sd_rtoms, "fetch_realtime", realtime)
    return asked


def run(data_root: Path, *extra: str):
    result = CliRunner().invoke(
        main, ["ingest", "--source", "sd_rtoms", "--data-root", str(data_root), *extra]
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def manifest(data_root: Path) -> dict:
    files = sorted((data_root / "raw" / "_manifests").glob("*.json"))
    assert len(files) == 1, f"expected one manifest, found {len(files)}"
    return json.loads(files[0].read_text())


# --------------------------------------------------------------------------
# Which dataset is asked for which window
# --------------------------------------------------------------------------


def test_a_year_before_the_boundary_reaches_only_the_older_dataset(data_root, offline):
    """The current dataset holds nothing then, and asking it would record an
    outage against a mooring that was reporting perfectly well."""
    assert run(data_root, "--year", "2020").exit_code == 0
    assert [(code, year) for code, year, _, _ in offline] == [(OLDER, 2020)]


def test_a_year_after_the_boundary_reaches_only_the_current_dataset(data_root, offline):
    """The one that matters. The older dataset still *serves* 2022, carrying the
    other dataset's readings under its own depth labels -- so a run that asked
    it would land the deep sensor twice, at two permanent depths a metre apart
    (docs/02)."""
    assert run(data_root, "--year", "2022").exit_code == 0
    assert [(code, year) for code, year, _, _ in offline] == [(CURRENT, 2022)]


def test_the_year_the_boundary_falls_inside_is_split_between_them(data_root, offline):
    """Both halves fetched, oldest first, and they meet at the boundary exactly
    -- no instant asked twice and none asked of neither."""
    assert run(data_root, "--year", "2021").exit_code == 0
    assert offline == [
        (OLDER, 2021, None, BOUNDARY),
        (CURRENT, 2021, BOUNDARY, None),
    ]


def test_the_realtime_feed_belongs_to_the_current_dataset_alone(data_root, offline):
    """A superseded dataset has a fixed end, so "the last 45 days" of it is
    either nothing or the last 45 days it holds, labelled as current."""
    assert run(data_root).exit_code == 0
    assert [(code, year) for code, year, _, _ in offline] == [(CURRENT, None)]


def test_the_boundary_reaches_the_url_the_manifest_records(data_root, offline):
    """The clip is in the URL, so a landed window can be re-requested by copying
    a string out of the manifest and gets back exactly the rows it holds."""
    run(data_root, "--year", "2021")
    urls = [entry["path"] for entry in manifest(data_root)["files"]]
    assert any("time%3C2021-11-04T00%3A00%3A00Z" in url and OLDER in url for url in urls)
    assert any("time%3E=2021-11-04T00%3A00%3A00Z" in url and CURRENT in url for url in urls)


def test_a_site_with_one_dataset_is_asked_for_the_window_it_always_was(tmp_path, offline):
    """The clip is what a spanning site asks for. Every other station must land
    under the URL it always did -- `raw/` is addressed by it and the validator
    cache is keyed on it."""
    single = {**SITE, "site_id": "SDRTOMS:SBOO"}
    del single["predecessor_datasets"]
    root = registry_with(single, root=tmp_path / "data")

    assert run(root, "--year", "2023").exit_code == 0
    assert offline == [(CURRENT, 2023, None, None)]
    (entry,) = manifest(root)["files"]
    assert entry["path"] == sd_rtoms.archive_url(CURRENT, 2023)


def test_both_datasets_rows_land_under_one_site_id(data_root, offline):
    """The reason this is one site record and not two: `site_id` is part of
    `OBSERVATION_KEY`, so a second record would split one mooring's series
    permanently on the identifier."""
    run(data_root, "--year", "2021")

    stored = read_observations(Zones.at(data_root), source=sd_rtoms.SOURCE)
    assert set(stored["site_id"]) == {"SDRTOMS:PLOO"}
    assert not stored.empty


def test_a_fetcher_that_cannot_clip_refuses_the_site_by_name(tmp_path, monkeypatch, offline):
    """`predecessor_datasets` is a general registry field and most sources have
    no idea what a second dataset would mean; the refusal has to name the site
    rather than arrive as a keyword-argument error."""
    monkeypatch.delattr(sd_rtoms, "CLIPS_WINDOW_TO_DATASET")
    root = registry_with(SITE, root=tmp_path / "data")

    result = run(root, "--year", "2020")
    (entry,) = manifest(root)["files"]
    assert result.exit_code != 0
    assert "SDRTOMS:PLOO" in entry["reason"]
    assert "predecessor_datasets" in entry["reason"]
    # A window that failed before its URL was resolved keeps the placeholder the
    # entry was opened with, so that is the only thing naming which dataset of a
    # spanning site could not be asked for.
    assert entry["path"] == f"SDRTOMS:PLOO {OLDER} 2020"


def test_an_outage_on_one_dataset_does_not_cost_the_other(data_root, monkeypatch, offline):
    """docs/01 s5: one window's outage costs that window. A site spanning two
    datasets doubles the ways a run can be interrupted, so it has to stay true
    across them."""

    def flaky(dataset_id, year, *, session=None, validators=None, since=None, until=None):
        if dataset_id == OLDER:
            raise SourceUnavailable("the mooring did not report that window")
        offline.append((dataset_id, year, since, until))
        return new_payload(
            sd_rtoms.SOURCE,
            dataset_id,
            f"{dataset_id}_{year}.csv",
            sd_rtoms.archive_url(dataset_id, year, since=since, until=until),
            HISTORIC.read_bytes(),
        )

    monkeypatch.setattr(sd_rtoms, "fetch_archive", flaky)
    assert run(data_root, "--year", "2021").exit_code == 0
    outcomes = {
        OLDER if OLDER in entry["path"] else CURRENT: entry["outcome"]
        for entry in manifest(data_root)["files"]
    }
    assert outcomes == {OLDER: "skipped", CURRENT: "ingested"}
