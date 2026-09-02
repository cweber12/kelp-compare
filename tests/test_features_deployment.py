"""Deployment-scale features (docs/03 `deployment`, docs/04 s1).

The rule this file exists to hold is that a deployment is judged against its own
window and never against the calendar. Every case is small enough that the
coverage arithmetic can be checked by hand, so a failure says which number went
wrong rather than that a number moved.

The end-to-end run against the real registry is in `test_cli_deployment.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.features.config import Baseline, FeatureConfig, ParameterFeatures
from kelpcompare.features.deployment import (
    build_deployment,
    deployment_columns,
    deployment_window,
)
from kelpcompare.features.quarterly import build_quarterly
from kelpcompare.registry import load_registry
from kelpcompare.storage import OBSERVATION_COLUMNS

NOW = pd.Timestamp("2026-09-02T00:00:00Z")

TEMPERATURE = ParameterFeatures(
    parameter="sea_water_temperature",
    feature_set="temperature",
    thresholds={
        "days_above": (20.0,),
        "days_below": (14.0,),
        "degree_days_above": (18.0,),
        "max_spell_above": (20.0,),
    },
)


def config(*, coverage_floor: float = 0.6) -> FeatureConfig:
    return FeatureConfig(
        path=Path("features.json"),
        coverage_floor=coverage_floor,
        baseline=Baseline(start_year=2007, end_year=2019, min_years=10),
        parameters={"sea_water_temperature": TEMPERATURE},
    )


def observations(
    stamps, values, *, site_id="PROJ:X", depth_m=8.0, qc_flag=1, source="project"
) -> pd.DataFrame:
    index = stamps if isinstance(stamps, pd.DatetimeIndex) else pd.to_datetime(stamps, utc=True)
    return pd.DataFrame(
        {
            "timestamp": index,
            "site_id": site_id,
            "parameter": "sea_water_temperature",
            "value": [float(v) for v in values],
            "depth_m": depth_m,
            "qc_flag": qc_flag,
            "qc_tests": "gross_range:pass",
            "source": source,
            "fetch_run_id": "20260101T000000000Z-ingest",
        }
    )[list(OBSERVATION_COLUMNS)]


def registry(tmp_path: Path, *sites: dict):
    target = tmp_path / "sites.json"
    target.write_text(json.dumps({"sites": list(sites)}), encoding="utf-8")
    return load_registry(target)


def project_site(*, window, depth_m=8.0, site_id="PROJ:X", tz="UTC", **extra):
    deployment = {
        "instrument": "HOBO TidbiT MX2204",
        "serial": "SN1",
        "deployment_number": 3,
        "depth_m": depth_m,
        "tz": tz,
        "window_local": list(window),
        "series_map": {"T": "sea_water_temperature"},
    }
    deployment.update(extra)
    return {"site_id": site_id, "operator": "project", "deployments": [deployment]}


def build(tmp_path, frame, *sites, qc_max_flag=2, now=NOW, cfg=None):
    return build_deployment(
        frame, registry(tmp_path, *sites), cfg or config(), qc_max_flag=qc_max_flag, now=now
    )


# --------------------------------------------------------------------------
# Coverage against the deployment's own window
# --------------------------------------------------------------------------


def test_a_logger_that_recorded_every_sample_reads_full_coverage(tmp_path):
    """Six hours at ten minutes is 37 samples across a closed window, and all 37
    are there -- so coverage is exactly 1.0 and nothing is clamped."""
    stamps = pd.date_range("2026-07-11T00:00Z", "2026-07-11T06:00Z", freq="10min")
    frame, warnings = build(
        tmp_path,
        observations(stamps, [15.0] * len(stamps)),
        project_site(window=("2026-07-11 00:00", "2026-07-11 06:00")),
    )
    row = frame.iloc[0]
    assert row["n_obs"] == 37
    assert row["expected_obs"] == 37.0
    assert row["pct_coverage"] == 1.0
    assert row["usable"]
    assert warnings == ()


def test_a_logger_that_stopped_early_is_under_covered_and_says_so(tmp_path):
    """Coverage below the floor is now instrument health rather than a calendar
    artifact: the window is the operator's declaration of when it was down."""
    stamps = pd.date_range("2026-07-11T00:00Z", "2026-07-11T02:00Z", freq="10min")
    frame, _ = build(
        tmp_path,
        observations(stamps, [15.0] * len(stamps)),
        project_site(window=("2026-07-11 00:00", "2026-07-11 06:00")),
    )
    row = frame.iloc[0]
    assert row["n_obs"] == 13
    assert row["expected_obs"] == 37.0
    assert row["pct_coverage"] == pytest.approx(13 / 37)
    assert not row["usable"]


def test_the_same_record_is_usable_here_and_unusable_quarterly(tmp_path):
    """The defect this table corrects, stated as a comparison.

    Three weeks of complete 10-minute record: against its own window it is a full
    deployment, against Q3 it is 23% of a quarter and unusable.
    """
    stamps = pd.date_range("2026-07-11T00:00Z", "2026-08-01T00:00Z", freq="10min")
    frame = observations(stamps, [15.0] * len(stamps))

    deployment, _ = build(
        tmp_path, frame, project_site(window=("2026-07-11 00:00", "2026-08-01 00:00"))
    )
    quarterly = build_quarterly(frame, config(), qc_max_flag=2, now=NOW).frame

    assert deployment.iloc[0]["pct_coverage"] == 1.0
    assert deployment.iloc[0]["usable"]
    assert quarterly.iloc[0]["pct_coverage"] < 0.25
    assert not quarterly.iloc[0]["usable"]


# --------------------------------------------------------------------------
# The spell marker at a deployment boundary
# --------------------------------------------------------------------------


def test_a_warm_spell_running_to_the_start_of_the_record_is_not_a_floor(tmp_path):
    """Q3 opens on 1 July; the logger went in on the 11th. The unobserved days
    between are not a gap in the record -- the logger was not in the water -- so
    the spell is a measurement here and a floor in the quarterly table."""
    stamps = pd.date_range("2026-07-11T00:00Z", "2026-07-15T23:00Z", freq="h")
    frame = observations(stamps, [22.0] * len(stamps))

    deployment, _ = build(
        tmp_path, frame, project_site(window=("2026-07-11 00:00", "2026-07-15 23:00"))
    )
    quarterly = build_quarterly(frame, config(), qc_max_flag=2, now=NOW).frame

    assert deployment.iloc[0]["max_spell_above_20c_days"] == 5.0
    assert not deployment.iloc[0]["max_spell_above_20c_gap_interrupted"]
    assert quarterly.iloc[0]["max_spell_above_20c_days"] == 5.0
    assert quarterly.iloc[0]["max_spell_above_20c_gap_interrupted"]


# --------------------------------------------------------------------------
# What the table refuses to offer
# --------------------------------------------------------------------------


def test_the_table_has_no_anomaly_columns_at_all(tmp_path):
    """A project sensor cannot have a climatology for a decade (docs/04 s3,
    ADR-007). A null `_anom` twin would invite reading a thin baseline as a
    baseline; a column that does not exist cannot be misread."""
    assert not [name for name in deployment_columns(config()) if name.endswith("_anom")]


def test_the_key_is_validations_without_its_reference_columns():
    """So the two tables join on the deployment."""
    from kelpcompare.features.deployment import DEPLOYMENT_KEY
    from kelpcompare.features.validation import VALIDATION_KEY

    assert DEPLOYMENT_KEY == tuple(
        name for name in VALIDATION_KEY if not name.startswith("reference_")
    )


# --------------------------------------------------------------------------
# Windows, and what the registry has to supply
# --------------------------------------------------------------------------


def test_the_deployment_window_is_closed_at_both_ends(tmp_path):
    """Both edges are events that happened, and the closing sample is a real
    reading the registry means to include."""
    reg = registry(tmp_path, project_site(window=("2026-07-11 00:00", "2026-07-11 06:00")))
    window = deployment_window(reg.deployments[0])
    assert window.inclusive_end
    assert window.start == pd.Timestamp("2026-07-11T00:00Z")
    assert window.end == pd.Timestamp("2026-07-11T06:00Z")


def test_a_local_window_is_converted_to_utc(tmp_path):
    """Local time exists only inside adapters and the registry (hard rule 2)."""
    reg = registry(
        tmp_path,
        project_site(window=("2026-07-11 08:00", "2026-07-11 14:00"), tz="America/Los_Angeles"),
    )
    window = deployment_window(reg.deployments[0])
    assert window.start == pd.Timestamp("2026-07-11T15:00Z")


def test_a_deployment_with_no_window_is_warned_about_rather_than_guessed_at(tmp_path):
    """A coverage figure against a window nobody declared would be a guess
    wearing a measurement's clothes."""
    stamps = pd.date_range("2026-07-11T00:00Z", periods=10, freq="10min")
    site = {
        "site_id": "PROJ:X",
        "operator": "project",
        "deployments": [
            {
                "serial": "SN1",
                "deployment_number": 3,
                "depth_m": 8.0,
                "series_map": {"T": "sea_water_temperature"},
            }
        ],
    }
    frame, warnings = build(tmp_path, observations(stamps, [15.0] * 10), site)
    assert frame.empty
    assert any("no deployment window or timezone" in warning for warning in warnings)


def test_rows_outside_the_window_do_not_reach_the_row(tmp_path):
    """`site_id` and `depth_m` do not tell two deployments of one logger apart;
    the window is how `deployment_number` reaches the rows."""
    inside = pd.date_range("2026-07-11T00:00Z", "2026-07-11T06:00Z", freq="10min")
    outside = pd.date_range("2026-07-20T00:00Z", periods=50, freq="10min")
    frame, _ = build(
        tmp_path,
        pd.concat(
            [
                observations(inside, [15.0] * len(inside)),
                observations(outside, [25.0] * len(outside)),
            ],
            ignore_index=True,
        ),
        project_site(window=("2026-07-11 00:00", "2026-07-11 06:00")),
    )
    row = frame.iloc[0]
    assert row["n_obs"] == 37
    assert row["max"] == 15.0


def test_a_logger_still_in_the_water_is_distinguishable_from_one_recovered(tmp_path):
    """Under-covered for a reason that is not a fault -- the counterpart of
    `quarter_complete` for a quarter still in progress."""
    stamps = pd.date_range("2026-07-11T00:00Z", "2026-07-11T06:00Z", freq="10min")
    frame, _ = build(
        tmp_path,
        observations(stamps, [15.0] * len(stamps)),
        project_site(window=("2026-07-11 00:00", "2026-07-11 06:00")),
        now=pd.Timestamp("2026-07-11T03:00Z"),
    )
    assert not frame.iloc[0]["deployment_complete"]


def test_a_deployment_with_no_rows_in_its_window_is_warned_about(tmp_path):
    """Silently absent and recorded-but-empty are different states, and only one
    of them is a registry problem."""
    stamps = pd.date_range("2026-07-20T00:00Z", periods=10, freq="10min")
    frame, warnings = build(
        tmp_path,
        observations(stamps, [15.0] * 10),
        project_site(window=("2026-07-11 00:00", "2026-07-11 06:00")),
    )
    assert frame.empty
    assert any("no sea_water_temperature rows" in warning for warning in warnings)


def test_an_empty_build_still_writes_the_full_schema(tmp_path):
    """A build with nothing to do must not write a file whose columns depend on
    what it found."""
    frame, _ = build(
        tmp_path,
        observations(pd.date_range("2026-07-20T00:00Z", periods=10, freq="10min"), [15.0] * 10),
        project_site(window=("2026-07-11 00:00", "2026-07-11 06:00")),
    )
    assert tuple(frame.columns) == deployment_columns(config())


def test_qc_failed_rows_are_excluded_like_they_are_quarterly(tmp_path):
    """Coverage is measured on what survived QC, so a window that sampled
    perfectly and failed QC throughout is under-covered rather than complete."""
    stamps = pd.date_range("2026-07-11T00:00Z", "2026-07-11T06:00Z", freq="10min")
    values = [15.0] * len(stamps)
    good = observations(stamps[:19], values[:19])
    bad = observations(stamps[19:], values[19:], qc_flag=4)
    frame, _ = build(
        tmp_path,
        pd.concat([good, bad], ignore_index=True),
        project_site(window=("2026-07-11 00:00", "2026-07-11 06:00")),
    )
    assert frame.iloc[0]["n_obs"] == 19
    assert frame.iloc[0]["pct_coverage"] < 1.0
