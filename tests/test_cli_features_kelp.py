"""`kelpcompare features` over the kelp half and the comparison (docs/03, docs/04).

The real commands against a `tmp_path` data root seeded with the committed
registry: a Kelp Watch ingest, then a features run. Nothing is stubbed, because
this source has no network seam -- the whole chain from a recorded export to a
comparison row is the code an operator runs.

The environmental half is exercised by `test_cli_features.py`; what is new here
is that a second, differently-keyed table joins onto it, and that neither half
is required for the other to build.
"""

from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from kelpcompare.cli import main
from kelpcompare.features.comparison import LAGS
from kelpcompare.features.kelp import quarterly_kelp_columns
from kelpcompare.fetchers import ndbc
from kelpcompare.fetchers.base import new_payload
from kelpcompare.storage import Zones, read_features

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


NDBC_FIX = Path(__file__).parent / "fixtures" / "ndbc"


@pytest.fixture
def with_environment(monkeypatch):
    """Serve the recorded NDBC archive in place of the network, as the NDBC
    suite does. The comparison needs an environmental half to join onto."""
    archive = (NDBC_FIX / "ljac1h2023_excerpt.txt").read_bytes()

    def fetch_archive(station, year, *, session=None, validators=None):
        return new_payload(
            "ndbc",
            station.upper(),
            f"{station.lower()}h{year}.txt.gz",
            f"file://{year}",
            gzip.compress(archive, mtime=0),
        )

    monkeypatch.setattr(ndbc, "fetch_archive", fetch_archive)


def ingested(data_root: Path, *sources: Path) -> Path:
    incoming = data_root / "raw" / "kelpwatch" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    for source in sources or (LAJOLLA, DELMAR):
        shutil.copy2(source, incoming / source.name)
    run(data_root, "ingest", "--source", "kelpwatch")
    return data_root


def run(data_root: Path, command: str, *extra: str):
    result = CliRunner().invoke(main, [command, "--data-root", str(data_root), *extra])
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result


def table(data_root: Path, name: str):
    return read_features(Zones.at(data_root), name)


def manifest(data_root: Path) -> dict:
    files = sorted((data_root / "raw" / "_manifests").glob("*features.json"))
    return json.loads(files[-1].read_text())


# --------------------------------------------------------------------------
# The kelp half
# --------------------------------------------------------------------------


def test_the_kelp_tables_are_built_from_the_landings(data_root):
    result = run(ingested(data_root), "features")

    assert result.exit_code == 0
    kelp = table(data_root, "quarterly_kelp")
    assert tuple(kelp.columns) == quarterly_kelp_columns()
    assert len(kelp) == 2 * 170  # two beds, 1984Q1-2026Q2
    assert set(kelp["polygon_id"]) == {"KELP:LA-JOLLA", "KELP:DEL-MAR"}
    assert not table(data_root, "climatology_kelp").empty


def test_a_cloud_gap_reaches_the_table_as_a_null_and_never_as_a_zero(data_root):
    """The whole chain, end to end: the export writes 0, the table says nothing.

    Del Mar 1985Q1 is the case. Fifteen rows of code between the CSV and here
    could have turned it back into a measurement.
    """
    run(ingested(data_root), "features")
    kelp = table(data_root, "quarterly_kelp")
    delmar = kelp.loc[kelp["polygon_id"] == "KELP:DEL-MAR"].set_index(["year", "quarter"])

    assert delmar.loc[(1985, 1), "kelp_area_m2"] != delmar.loc[(1985, 1), "kelp_area_m2"]  # NaN
    assert delmar.loc[(1985, 1), "n_cells_observed"] == 0
    assert delmar.loc[(1984, 1), "kelp_area_m2"] == 0.0  # observed, and empty
    assert int(kelp["kelp_area_m2"].isna().sum()) == 15  # 7 La Jolla + 8 Del Mar


def test_the_anomalies_are_real_numbers_on_this_record(data_root):
    """Unlike the environmental half, the kelp record spans the whole 2007-2019
    baseline, so this is the first table in the project with anomalies in it."""
    run(ingested(data_root), "features")
    kelp = table(data_root, "quarterly_kelp")

    assert int(kelp["kelp_area_m2_anom"].notna().sum()) == len(kelp) - 15
    assert set(kelp.loc[kelp["kelp_area_m2_anom"].notna(), "baseline_years"]) == {13}


def test_every_kelp_row_carries_the_revision_it_came_from(data_root):
    run(ingested(data_root), "features")
    assert set(table(data_root, "quarterly_kelp")["kelp_watch_revision"]) == {23}


def test_the_manifest_records_each_polygons_quarter_counts(data_root):
    run(ingested(data_root), "features")
    entries = {
        entry["polygon_id"]: entry for entry in manifest(data_root)["series"] if entry["polygon_id"]
    }

    lajolla = entries["KELP:LA-JOLLA"]
    assert lajolla["quarters"] == 170
    assert lajolla["quarters_observed"] == 163
    assert lajolla["quarters_usable"] == 160
    assert (lajolla["first_quarter"], lajolla["last_quarter"]) == ("1984Q1", "2026Q2")
    assert lajolla["site_id"] is None  # a polygon is not a site


def test_the_run_reports_both_kinds_of_coverage_loss(data_root):
    result = run(ingested(data_root), "features")

    assert "no cloud-free observation" in result.output
    assert "coverage floor" in result.output
    assert result.exit_code == 0  # a cloud gap is the record, not a failure


# --------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------


def test_no_comparison_without_an_environmental_half(data_root):
    """A kelp-only project builds its kelp tables and stops, rather than writing
    an empty comparison that would read as "nothing correlates"."""
    run(ingested(data_root), "features")

    assert not (data_root / "features" / "comparison.parquet").exists()
    assert not table(data_root, "quarterly_kelp").empty


def test_a_kelp_only_project_is_not_an_error(data_root):
    result = run(ingested(data_root), "features")
    assert result.exit_code == 0
    assert "nothing to build" not in result.output


def test_an_environment_only_project_is_not_an_error(data_root):
    """No landings at all: the kelp half is skipped in silence, which is what
    every run on a machine with no exports will do."""
    result = run(data_root, "features")

    assert result.exit_code == 0
    assert "nothing to build" in result.output
    assert not (data_root / "features").exists()


def test_source_kelpwatch_builds_only_the_kelp_half(data_root):
    result = run(ingested(data_root), "features", "--source", "kelpwatch")

    assert result.exit_code == 0
    assert not table(data_root, "quarterly_kelp").empty
    assert not (data_root / "features" / "quarterly_env.parquet").exists()
    assert manifest(data_root)["sources"] == ["kelpwatch"]


# --------------------------------------------------------------------------
# Rebuilding
# --------------------------------------------------------------------------


def test_rebuilding_after_a_registry_edit_needs_no_second_download(data_root):
    """The reason `ingest` lands and `features` aggregates: a manual download
    must not be the price of changing which sites a polygon is compared against.
    """
    ingested(data_root)
    run(data_root, "features")
    before = len(table(data_root, "quarterly_kelp"))

    path = data_root / "registry" / "polygons.geojson"
    payload = json.loads(path.read_text())
    payload["features"] = [
        feature
        for feature in payload["features"]
        if feature["properties"]["polygon_id"] != "KELP:DEL-MAR"
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    run(data_root, "features")

    after = table(data_root, "quarterly_kelp")
    assert before == 2 * 170
    assert set(after["polygon_id"]) == {"KELP:LA-JOLLA"}


def test_two_runs_over_unchanged_inputs_write_the_same_bytes(data_root):
    """What makes `rebuild` mean anything on this half too."""
    ingested(data_root)
    run(data_root, "features")
    first = (data_root / "features" / "quarterly_kelp.parquet").read_bytes()

    run(data_root, "features")
    assert (data_root / "features" / "quarterly_kelp.parquet").read_bytes() == first


def test_an_export_landed_at_another_revision_is_not_read(data_root):
    """The registry says which revision is the source of record. A newer one may
    revise history, so reading two as one series would silently mix them."""
    ingested(data_root)
    path = data_root / "registry" / "polygons.geojson"
    payload = json.loads(path.read_text())
    payload["kelp_watch"]["revision"] = 24
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = run(data_root, "features")

    assert result.exit_code == 0
    assert "nothing to build" in result.output  # ver24 has no landings


def test_two_exports_of_one_polygon_at_one_revision_skip_it_loudly(data_root):
    """Both claim to be that bed's record at that version, and raw is
    append-only, so the fix is a revision bump rather than a deletion."""
    ingested(data_root)
    landed = next((data_root / "raw" / "kelpwatch" / "ver23" / "KELP_DEL-MAR").iterdir())
    shutil.copy2(LAJOLLA, landed.parent / "deadbeef__kelp_delmar.csv")

    result = run(data_root, "features")

    assert result.exit_code == 1
    assert "KELP:DEL-MAR" in result.output
    assert "Bump kelp_watch.revision" in result.output
    assert set(table(data_root, "quarterly_kelp")["polygon_id"]) == {"KELP:LA-JOLLA"}


def test_an_export_that_will_not_parse_costs_its_polygon_and_sets_the_code(data_root):
    ingested(data_root)
    landed = next((data_root / "raw" / "kelpwatch" / "ver23" / "KELP_DEL-MAR").iterdir())
    landed.write_text("year,quarter\n1984,1\n", encoding="utf-8")

    result = run(data_root, "features")

    assert result.exit_code == 1
    assert set(table(data_root, "quarterly_kelp")["polygon_id"]) == {"KELP:LA-JOLLA"}


def test_a_dry_run_writes_no_kelp_table(data_root):
    result = run(ingested(data_root), "features", "--dry-run")

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert not (data_root / "features").exists()


def test_the_lag_count_is_the_documented_one():
    """Held here as well as in the builder's own suite, so a change to the
    screen's shape has to be a deliberate edit in two places."""
    assert LAGS == (0, 1, 2, 3, 4)


# --------------------------------------------------------------------------
# The comparison, with both halves present
# --------------------------------------------------------------------------


def with_both_halves(data_root: Path) -> Path:
    ingested(data_root)
    run(data_root, "ingest", "--source", "ndbc", "--year", "2023")
    run(data_root, "qc")
    run(data_root, "features")
    return data_root


def test_the_comparison_is_written_when_both_halves_exist(data_root, with_environment):
    comparison = table(with_both_halves(data_root), "comparison")

    assert not comparison.empty
    # Every declared series x every kelp quarter x every lag, with no gaps. The
    # series count is read off the table rather than written down, so a station
    # added to a polygon's site_ids changes the size without editing this case.
    series = comparison[["polygon_id", "site_id", "parameter", "depth_m"]].drop_duplicates()
    assert len(comparison) == len(series) * 170 * len(LAGS)
    assert sorted(set(comparison["lag"])) == list(LAGS)
    assert set(comparison["polygon_id"]) == {"KELP:LA-JOLLA", "KELP:DEL-MAR"}


def test_only_the_pairs_the_registry_declares_appear(data_root, with_environment):
    """docs/03 integrity rule, at the far end: no analysis code has to match a
    polygon name against a station name, because nothing here did."""
    root = with_both_halves(data_root)
    comparison = table(root, "comparison")
    declared = {
        (feature["properties"]["polygon_id"], site)
        for feature in json.loads(
            (root / "registry" / "polygons.geojson").read_text(encoding="utf-8")
        )["features"]
        for site in feature["properties"]["site_ids"]
    }

    appeared = set(zip(comparison["polygon_id"], comparison["site_id"]))
    assert appeared <= declared, f"undeclared pairs reached the table: {appeared - declared}"
    assert "NDBC:LJAC1" in set(comparison["site_id"])


def test_the_environment_leads_kelp_by_the_lag_on_the_row(data_root, with_environment):
    """The recorded archive covers 2023Q2 only, which makes the direction visible
    end to end: the environmental side is populated on exactly the five kelp
    quarters that are 0 to 4 quarters *after* it."""
    comparison = table(with_both_halves(data_root), "comparison")
    matched = comparison.loc[comparison["env_usable"].notna()]

    assert set(zip(matched["env_year"], matched["env_quarter"])) == {(2023, 2)}
    assert sorted({(int(r.year), int(r.quarter)) for r in matched.itertuples()}) == [
        (2023, 2),
        (2023, 3),
        (2023, 4),
        (2024, 1),
        (2024, 2),
    ]


def test_a_lag_reaching_past_the_environmental_record_keeps_its_row(data_root, with_environment):
    """Most of this table is kelp with no environmental counterpart -- the record
    starts in 1984 and the station in 2023 -- and every one of those is a row."""
    comparison = table(with_both_halves(data_root), "comparison")

    unmatched = comparison.loc[comparison["env_usable"].isna()]
    matched = comparison.loc[comparison["env_usable"].notna()]

    # Every series that has an environmental record contributes one matched row
    # per lag; everything else is kept with a null environmental side.
    with_record = matched[["polygon_id", "site_id", "parameter", "depth_m"]].drop_duplicates()
    assert len(matched) == len(with_record) * len(LAGS)
    assert len(unmatched) == len(comparison) - len(matched)
    assert not unmatched.empty
    assert unmatched["kelp_area_m2_anom"].notna().any()  # the kelp side is intact


def test_the_comparison_is_regenerated_wholesale(data_root, with_environment):
    """A pair the registry no longer declares loses its rows rather than keeping
    them forever."""
    with_both_halves(data_root)
    before = len(table(data_root, "comparison"))

    path = data_root / "registry" / "polygons.geojson"
    payload = json.loads(path.read_text())
    payload["features"] = payload["features"][:1]
    path.write_text(json.dumps(payload), encoding="utf-8")
    run(data_root, "features")

    after = table(data_root, "comparison")
    assert len(after) < before
    assert set(after["polygon_id"]) == {payload["features"][0]["properties"]["polygon_id"]}


def test_a_source_scoped_rerun_still_regenerates_the_whole_comparison(data_root, with_environment):
    """It is read back from the zone rather than taken from the run's outcomes,
    so a `--source ndbc` rebuild still reflects every polygon beside it."""
    with_both_halves(data_root)
    before = len(table(data_root, "comparison"))

    run(data_root, "features", "--source", "ndbc")
    assert len(table(data_root, "comparison")) == before


def test_two_runs_write_the_same_comparison_bytes(data_root, with_environment):
    with_both_halves(data_root)
    first = (data_root / "features" / "comparison.parquet").read_bytes()

    run(data_root, "features")
    assert (data_root / "features" / "comparison.parquet").read_bytes() == first


def test_the_run_reports_how_much_comparison_it_produced(data_root, with_environment):
    ingested(data_root)
    run(data_root, "ingest", "--source", "ndbc", "--year", "2023")
    run(data_root, "qc")
    result = run(data_root, "features")

    assert "comparison:" in result.output
    assert "polygon-quarters" in result.output


def test_repeated_runs_do_not_grow_the_kelp_climatology(data_root):
    """The table has no natural `source`-free identity to dedupe on, so a write
    that could not supersede its own rows would add a build's worth every run --
    silently, and only visible as a row count."""
    ingested(data_root)
    counts = []
    for _ in range(3):
        run(data_root, "features")
        counts.append(len(table(data_root, "climatology_kelp")))

    assert len(set(counts)) == 1, f"climatology_kelp grew across runs: {counts}"


def test_every_written_table_is_byte_identical_across_two_runs(data_root, with_environment):
    """Applied to all five rather than to one: the first version of this suite
    checked only quarterly_kelp, and the table that was doubling was a different
    one."""
    with_both_halves(data_root)
    before = {p.name: p.read_bytes() for p in (data_root / "features").glob("*.parquet")}
    assert len(before) == 5

    run(data_root, "features")
    after = {p.name: p.read_bytes() for p in (data_root / "features").glob("*.parquet")}
    assert after == before
