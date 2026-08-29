"""Neighbor validation (docs/04 §1, docs/03 `validation.parquet`).

The rule this file exists to hold is the per-statistic depth one: a reference at
another depth still gives a usable correlation and must not give a bias, because
a bias across a thermocline measures stratification and prints it as instrument
error. Every case is a hand-built pair whose answer can be worked out by hand,
so a failure says which statistic went wrong rather than that a number moved.

The second rule is the platform fold. `NDBC:LJAC1` and `COOPS:9410230` are one
NOS package, and a table that counted them twice would double the apparent
evidence for every project sensor.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from kelpcompare.features.validation import VALIDATION_COLUMNS, build_validation
from kelpcompare.registry import load_registry

WINDOW = ("2026-07-11 00:00", "2026-07-12 00:00")


def observations(*blocks: dict) -> pd.DataFrame:
    """One frame from several `{site_id, depth_m, values, start, freq}` blocks."""
    frames = []
    for block in blocks:
        values = block["values"]
        start = pd.Timestamp(block.get("start", "2026-07-11 08:00"))
        stamps = pd.date_range(start, periods=len(values), freq=block.get("freq", "10min"))
        frames.append(
            pd.DataFrame(
                {
                    "timestamp": stamps,
                    "site_id": block["site_id"],
                    "parameter": block.get("parameter", "sea_water_temperature"),
                    "value": [float(v) for v in values],
                    "depth_m": block["depth_m"],
                    "qc_flag": block.get("qc_flag", 1),
                    "source": block.get("source", "project"),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def registry(tmp_path: Path, *sites: dict):
    target = tmp_path / "sites.json"
    target.write_text(json.dumps({"sites": list(sites)}), encoding="utf-8")
    return load_registry(target)


def project_site(*, depth_m: float, refs: list[str], site_id: str = "PROJ:X", **extra):
    deployment = {
        "serial": "SN1",
        "deployment_number": 3,
        "depth_m": depth_m,
        "tz": "UTC",
        "window_local": list(WINDOW),
        "series_map": {"T": "sea_water_temperature"},
    }
    deployment.update(extra)
    return {"site_id": site_id, "neighbor_refs": refs, "deployments": [deployment]}


def station(site_id: str, *, depth: float | None = 3.4, platform: list[str] | None = None):
    record = {"site_id": site_id, "station_code": site_id.split(":")[-1], "operator": "ndbc"}
    if depth is not None:
        record["sensor_depths_m"] = {"sea_water_temperature": depth}
    if platform:
        record["same_platform_as"] = platform
    return record


def build(tmp_path, frame, *sites, tolerance_m=5.0, qc_max_flag=2):
    return build_validation(
        frame, registry(tmp_path, *sites), tolerance_m=tolerance_m, qc_max_flag=qc_max_flag
    )


def test_a_same_depth_reference_reports_all_three_statistics(tmp_path):
    """The logger reads exactly 1 degC above the reference at every bin, so bias
    is +1, RMSE is 1, and the two series are perfectly correlated."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13, 14, 15]},
        {"site_id": "NDBC:A", "depth_m": 5.0, "values": [10, 11, 12, 13, 14], "source": "ndbc"},
    )

    table, _ = build(
        tmp_path, frame, project_site(depth_m=5.0, refs=["NDBC:A"]), station("NDBC:A", depth=5.0)
    )

    (row,) = table.to_dict("records")
    assert row["depth_gap_m"] == 0.0
    assert row["depth_comparable"]
    assert row["n_pairs"] == 5
    assert row["bias"] == pytest.approx(1.0)
    assert row["rmse"] == pytest.approx(1.0)
    assert row["correlation"] == pytest.approx(1.0)


def test_a_deeper_reference_gives_correlation_but_refuses_bias_and_rmse(tmp_path):
    """The docs/04 §1 rule. The offset is real and large -- that is the point --
    and reporting it as bias would invert what this table is evidence for."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 16.76, "values": [15, 16, 17, 18, 19]},
        {"site_id": "NDBC:A", "depth_m": 3.4, "values": [20, 21, 22, 23, 24], "source": "ndbc"},
    )

    table, _ = build(
        tmp_path, frame, project_site(depth_m=16.76, refs=["NDBC:A"]), station("NDBC:A")
    )

    (row,) = table.to_dict("records")
    assert row["depth_gap_m"] == pytest.approx(13.36)
    assert not row["depth_comparable"]
    assert np.isnan(row["bias"]) and np.isnan(row["rmse"])
    assert row["correlation"] == pytest.approx(1.0)


def test_a_refused_bias_still_reports_how_much_data_there_was(tmp_path):
    """A null bias means the comparison was refused, not that the rows were
    missing. `n_pairs` populated either way is how a reader tells them apart."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 16.76, "values": [15, 16, 17, 18, 19]},
        {"site_id": "NDBC:A", "depth_m": 3.4, "values": [20, 21, 22, 23, 24], "source": "ndbc"},
    )

    table, _ = build(
        tmp_path, frame, project_site(depth_m=16.76, refs=["NDBC:A"]), station("NDBC:A")
    )

    assert table["n_pairs"].iloc[0] == 5


def test_the_tolerance_is_what_decides_comparability(tmp_path):
    """Same pair, two configurations: the number comes from features.json, so a
    retune must move the verdict rather than needing a code change."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 8.23, "values": [15, 16, 17, 18, 19]},
        {"site_id": "NDBC:A", "depth_m": 3.4, "values": [16, 17, 18, 19, 20], "source": "ndbc"},
    )
    sites = (project_site(depth_m=8.23, refs=["NDBC:A"]), station("NDBC:A"))

    lenient, _ = build(tmp_path, frame, *sites, tolerance_m=5.0)
    strict, _ = build(tmp_path, frame, *sites, tolerance_m=1.0)

    assert lenient["depth_comparable"].iloc[0]
    assert not strict["depth_comparable"].iloc[0]
    assert np.isnan(strict["bias"].iloc[0])


def test_an_unknown_depth_gap_is_not_a_small_one(tmp_path):
    """A reference that never published a depth is refused, not assumed close."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13, 14, 15]},
        {
            "site_id": "NDBC:A",
            "depth_m": np.nan,
            "values": [10, 11, 12, 13, 14],
            "source": "ndbc",
        },
    )

    table, _ = build(
        tmp_path, frame, project_site(depth_m=5.0, refs=["NDBC:A"]), station("NDBC:A", depth=None)
    )

    (row,) = table.to_dict("records")
    assert np.isnan(row["depth_gap_m"])
    assert not row["depth_comparable"]
    assert np.isnan(row["bias"])
    assert not np.isnan(row["correlation"])


def test_one_platform_produces_one_row(tmp_path):
    """docs/04 §1: LJAC1 and 9410230 are one NOS package. Counting both would
    double the apparent evidence for every project sensor."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13, 14, 15]},
        {"site_id": "NDBC:LJAC1", "depth_m": 5.0, "values": [10, 11, 12, 13, 14], "source": "ndbc"},
        {
            "site_id": "COOPS:9410230",
            "depth_m": 5.0,
            "values": [10, 11, 12, 13, 14],
            "source": "coops",
        },
    )

    table, _ = build(
        tmp_path,
        frame,
        project_site(depth_m=5.0, refs=["NDBC:LJAC1", "COOPS:9410230"]),
        station("NDBC:LJAC1", depth=5.0, platform=["COOPS:9410230"]),
        station("COOPS:9410230", depth=5.0),
    )

    (row,) = table.to_dict("records")
    assert row["reference_site_id"] == "NDBC:LJAC1"
    assert row["collapsed_refs"] == "COOPS:9410230"


def test_the_platform_fold_works_from_either_side(tmp_path):
    """Only one of the pair has to declare `same_platform_as`. A registry that
    records the relationship once is not one that records it wrongly."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13, 14, 15]},
        {"site_id": "NDBC:LJAC1", "depth_m": 5.0, "values": [10, 11, 12, 13, 14], "source": "ndbc"},
        {
            "site_id": "COOPS:9410230",
            "depth_m": 5.0,
            "values": [10, 11, 12, 13, 14],
            "source": "coops",
        },
    )

    table, _ = build(
        tmp_path,
        frame,
        project_site(depth_m=5.0, refs=["NDBC:LJAC1", "COOPS:9410230"]),
        station("NDBC:LJAC1", depth=5.0),
        station("COOPS:9410230", depth=5.0, platform=["NDBC:LJAC1"]),
    )

    assert len(table) == 1


def test_two_independent_references_are_two_rows(tmp_path):
    """The fold must not swallow a genuinely separate station."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13, 14, 15]},
        {"site_id": "NDBC:A", "depth_m": 5.0, "values": [10, 11, 12, 13, 14], "source": "ndbc"},
        {"site_id": "NDBC:B", "depth_m": 5.0, "values": [10, 11, 12, 13, 14], "source": "ndbc"},
    )

    table, _ = build(
        tmp_path,
        frame,
        project_site(depth_m=5.0, refs=["NDBC:A", "NDBC:B"]),
        station("NDBC:A", depth=5.0),
        station("NDBC:B", depth=5.0),
    )

    assert sorted(table["reference_site_id"]) == ["NDBC:A", "NDBC:B"]
    assert list(table["collapsed_refs"]) == ["", ""]


def test_a_reference_at_two_depths_is_two_rows(tmp_path):
    """Agreement at 0.5 m says nothing about agreement at 5 m, which is why
    `reference_depth_m` is in the key."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13, 14, 15]},
        {"site_id": "SIO:P", "depth_m": 0.5, "values": [10, 11, 12, 13, 14], "source": "sio"},
        {"site_id": "SIO:P", "depth_m": 5.0, "values": [11, 12, 13, 14, 15], "source": "sio"},
    )

    table, _ = build(
        tmp_path, frame, project_site(depth_m=5.0, refs=["SIO:P"]), station("SIO:P", depth=5.0)
    )

    assert sorted(table["reference_depth_m"]) == [0.5, 5.0]
    assert table.set_index("reference_depth_m").loc[5.0, "bias"] == pytest.approx(0.0)


def test_both_sides_are_binned_to_the_coarser_cadence(tmp_path):
    """A 10-minute logger against a 6-minute station is compared at 10 minutes.
    Binning to the finer one would leave most bins one-sided, which is the
    exact-timestamp join under another name."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [10] * 6, "freq": "10min"},
        {
            "site_id": "NDBC:A",
            "depth_m": 5.0,
            "values": [10] * 10,
            "freq": "6min",
            "source": "ndbc",
        },
    )

    table, _ = build(
        tmp_path, frame, project_site(depth_m=5.0, refs=["NDBC:A"]), station("NDBC:A", depth=5.0)
    )

    (row,) = table.to_dict("records")
    assert row["cadence_s"] == 600
    assert row["n_pairs"] == 6


def test_rows_outside_the_deployment_window_are_excluded_even_when_unfiltered(tmp_path):
    """`site_id` and `depth_m` do not tell two deployments of one logger apart;
    the window does. A `--qc-max-flag 9` run must not merge them."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13], "start": "2026-07-11 08:00"},
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [90, 91, 92], "start": "2026-08-01 08:00"},
        {
            "site_id": "NDBC:A",
            "depth_m": 5.0,
            "values": [10, 11, 12],
            "start": "2026-07-11 08:00",
            "source": "ndbc",
        },
    )

    table, _ = build(
        tmp_path,
        frame,
        project_site(depth_m=5.0, refs=["NDBC:A"]),
        station("NDBC:A", depth=5.0),
        qc_max_flag=9,
    )

    assert table["n_pairs"].iloc[0] == 3
    assert table["bias"].iloc[0] == pytest.approx(1.0)


def test_the_qc_filter_applies_to_both_sides(tmp_path):
    """A reference row nobody trusts must not quietly enter the comparison."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13, 14, 15]},
        {
            "site_id": "NDBC:A",
            "depth_m": 5.0,
            "values": [10, 11, 12, 13, 14],
            "source": "ndbc",
            "qc_flag": 4,
        },
    )

    table, warnings = build(
        tmp_path, frame, project_site(depth_m=5.0, refs=["NDBC:A"]), station("NDBC:A", depth=5.0)
    )

    assert table.empty
    assert any("NDBC:A" in warning for warning in warnings)


def test_correlation_is_null_where_it_would_not_mean_anything(tmp_path):
    """Two points always correlate perfectly; a flat series has no variance.
    Printing 1.0 for either would read as a finding."""
    flat = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [12, 12, 12, 12]},
        {"site_id": "NDBC:A", "depth_m": 5.0, "values": [10, 11, 12, 13], "source": "ndbc"},
    )
    two = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12]},
        {"site_id": "NDBC:A", "depth_m": 5.0, "values": [10, 11], "source": "ndbc"},
    )
    sites = (project_site(depth_m=5.0, refs=["NDBC:A"]), station("NDBC:A", depth=5.0))

    flat_table, _ = build(tmp_path, flat, *sites)
    two_table, _ = build(tmp_path, two, *sites)

    assert np.isnan(flat_table["correlation"].iloc[0])
    assert np.isnan(two_table["correlation"].iloc[0])
    assert two_table["bias"].iloc[0] == pytest.approx(1.0)


def test_a_reference_naming_no_registered_station_warns_rather_than_raising(tmp_path):
    """One bad reference must not cost the run every other pair."""
    frame = observations(
        {"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13, 14, 15]},
        {"site_id": "NDBC:A", "depth_m": 5.0, "values": [10, 11, 12, 13, 14], "source": "ndbc"},
    )

    table, warnings = build(
        tmp_path,
        frame,
        project_site(depth_m=5.0, refs=["NDBC:GONE", "NDBC:A"]),
        station("NDBC:A", depth=5.0),
    )

    assert len(table) == 1
    assert any("NDBC:GONE" in warning for warning in warnings)


def test_a_site_with_no_references_warns_and_produces_nothing(tmp_path):
    """Empty `neighbor_refs` means undeclared. Guessing the nearest station would
    make the table look complete on a site nobody has reviewed."""
    frame = observations({"site_id": "PROJ:X", "depth_m": 5.0, "values": [11, 12, 13]})

    table, warnings = build(tmp_path, frame, project_site(depth_m=5.0, refs=[]))

    assert table.empty
    assert any("neighbor_refs" in warning for warning in warnings)


def test_an_empty_observations_zone_still_has_the_declared_schema(tmp_path):
    """A build with nothing to do writes a readable table, not one whose columns
    depend on what it happened to find."""
    table, _ = build(tmp_path, pd.DataFrame(), project_site(depth_m=5.0, refs=["NDBC:A"]))

    assert list(table.columns) == list(VALIDATION_COLUMNS)
    assert table.empty
