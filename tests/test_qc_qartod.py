"""The QARTOD tests over stored observation rows (docs/04 s1, ADR-004).

Synthetic series are built here in memory so each test states one behaviour with
the smallest series that shows it. The last section is different: it runs the
reference HOBO export through the real adapter and normalizer and asserts the
numbers docs/06 s5 check 6 predicts, which is the one case that proves the
thresholds in the committed registry are the right size for real water.

Nothing here touches the repo's own `data/` beyond reading the committed
registry, and nothing reaches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from kelpcompare.adapters import hobo_xlsx
from kelpcompare.cli import SOURCE_NAMES
from kelpcompare.normalize import to_observations
from kelpcompare.parameters import load_parameters
from kelpcompare.qc.flags import parse_tests
from kelpcompare.qc.qartod import evaluate
from kelpcompare.registry import find_deployments, load_registry
from kelpcompare.storage import (
    FLAG_FAIL,
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    FLAG_PASS,
    FLAG_SUSPECT,
    OBSERVATION_COLUMNS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures"
ORIGINAL = FIX / "Tidbit_1__22506632__2026-08-01_07_44_27_PDT__Data_PDT_.xlsx"
REGISTRY_SOURCE = REPO_ROOT / "data" / "registry"

#: The thresholds the committed registry declares, restated so a synthetic test
#: reads as a statement about behaviour rather than about the project's data.
SPIKE = {"suspect": 1.5, "fail": 3.0}
RATE = {"suspect_per_hour": 18.0, "fail_per_hour": 36.0}


def registry(tmp_path: Path, *, valid_range=(5.0, 35.0), qc: dict | None = None):
    """A one-parameter `parameters.json`, loaded.

    `valid_range=None` leaves the parameter with no gross-range thresholds,
    which is the only way to watch one test's verdicts reach the roll-up alone.
    """
    record: dict = {"unit": "degC"}
    if valid_range is not None:
        record["valid_range"] = list(valid_range)
    if qc is not None:
        record["qc"] = qc
    target = tmp_path / "parameters.json"
    target.write_text(
        json.dumps({"parameters": {"sea_water_temperature": record}}), encoding="utf-8"
    )
    return load_parameters(target)


def observations(
    values,
    *,
    site: str = "PROJ:TEST",
    parameter: str = "sea_water_temperature",
    depth: float | None = None,
    qc_tests: str = "deployment_window:pass",
    start: str = "2026-07-11 14:00",
    freq: str = "10min",
) -> pd.DataFrame:
    """docs/03 rows for one series, as ingest would have left them."""
    stamps = pd.date_range(start, periods=len(values), freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "site_id": site,
            "parameter": parameter,
            "value": np.asarray(values, dtype="float64"),
            "depth_m": depth,
            "qc_flag": np.int8(FLAG_NOT_EVALUATED),
            "qc_tests": qc_tests,
            "source": "project",
            "fetch_run_id": "20260824T000000000Z-ingest",
        }
    )[list(OBSERVATION_COLUMNS)]


def flags(frame: pd.DataFrame) -> list[int]:
    return [int(f) for f in frame["qc_flag"]]


def verdict(frame: pd.DataFrame, row: int, test: str) -> str | None:
    return parse_tests(frame["qc_tests"].iloc[row]).get(test)


# --------------------------------------------------------------------------
# Gross range -- the one test every parameter gets
# --------------------------------------------------------------------------


def test_a_reading_inside_the_valid_range_passes(tmp_path):
    outcome = evaluate(observations([18.0, 18.1, 18.2]), registry(tmp_path))
    assert flags(outcome.frame) == [FLAG_PASS, FLAG_PASS, FLAG_PASS]
    assert verdict(outcome.frame, 0, "gross_range") == "pass"


def test_a_reading_outside_the_valid_range_fails(tmp_path):
    outcome = evaluate(observations([18.0, 41.0, 18.2]), registry(tmp_path))
    assert flags(outcome.frame) == [FLAG_PASS, FLAG_FAIL, FLAG_PASS]
    assert verdict(outcome.frame, 1, "gross_range") == "fail"


def test_a_suspect_span_makes_an_extreme_but_valid_reading_suspect(tmp_path):
    parameters = registry(tmp_path, qc={"gross_range": {"suspect_span": [8.0, 30.0]}})
    outcome = evaluate(observations([18.0, 32.0, 18.2]), parameters)
    assert flags(outcome.frame) == [FLAG_PASS, FLAG_SUSPECT, FLAG_PASS]


def test_a_parameter_with_no_qc_block_still_gets_gross_range(tmp_path):
    """docs/03: absent thresholds silence the other tests, not this one."""
    outcome = evaluate(observations([18.0, 41.0, 18.2]), registry(tmp_path))
    assert verdict(outcome.frame, 1, "gross_range") == "fail"
    assert verdict(outcome.frame, 1, "spike") is None


# --------------------------------------------------------------------------
# Spike and rate of change
# --------------------------------------------------------------------------


def test_a_spike_fails_and_its_neighbours_come_out_suspect(tmp_path):
    """The average method judges a sample against the midpoint of the two around
    it, so a spike tall enough to fail displaces each neighbour by half its
    height. With the registry's 1.5/3.0 that is unavoidable: any spike over the
    fail threshold puts its neighbours over the suspect one. Worth stating
    plainly -- a single bad reading costs three rows from a `qc_flag <= 2`
    query, not one."""
    parameters = registry(tmp_path, qc={"spike": SPIKE})
    outcome = evaluate(observations([18.0, 18.0, 24.0, 18.0, 18.0]), parameters)
    assert verdict(outcome.frame, 2, "spike") == "fail"
    assert flags(outcome.frame) == [
        FLAG_PASS,  # an end of the series: the spike test reaches no verdict
        FLAG_SUSPECT,
        FLAG_FAIL,
        FLAG_SUSPECT,
        FLAG_PASS,
    ]


def test_a_series_too_short_to_have_neighbours_gets_no_spike_verdict(tmp_path):
    """`ioos_qc.spike_test` judges a sample against its two neighbours.

    The stored columns alone cannot hold this: below three points `spike_test`
    returns UNKNOWN, which the roll-up omits, so `qc_tests` reads the same
    whether the guard is there or not. What differs is the manifest -- the run
    says the test was skipped and why, instead of listing it among the tests
    that ran and finding nothing.
    """
    parameters = registry(tmp_path, qc={"spike": SPIKE})
    outcome = evaluate(observations([18.0, 24.0]), parameters)
    assert verdict(outcome.frame, 0, "spike") is None
    assert flags(outcome.frame) == [FLAG_PASS, FLAG_PASS]  # gross range still ran

    (series,) = outcome.series
    assert "spike" not in series.tests
    assert any("spike not run: 2 rows" in warning for warning in outcome.warnings)


def test_an_abrupt_step_between_samples_fails_rate_of_change(tmp_path):
    """36 degC/h over a 10-minute sample is a 6 degC step."""
    parameters = registry(tmp_path, qc={"rate_of_change": RATE})
    outcome = evaluate(observations([18.0, 25.0, 25.1]), parameters)
    assert verdict(outcome.frame, 1, "rate_of_change") == "fail"


def test_a_gentler_step_is_only_suspect(tmp_path):
    """18 degC/h over 10 minutes is 3 degC; 4 degC clears suspect, not fail."""
    parameters = registry(tmp_path, qc={"rate_of_change": RATE})
    outcome = evaluate(observations([18.0, 22.0, 22.1]), parameters)
    assert verdict(outcome.frame, 1, "rate_of_change") == "suspect"


def test_a_rate_of_change_block_with_no_suspect_threshold_is_reported_not_run(tmp_path):
    """QARTOD's rate test has no fail-only form; say so rather than guess one."""
    parameters = registry(tmp_path, qc={"rate_of_change": {"fail_per_hour": 36.0}})
    outcome = evaluate(observations([18.0, 25.0, 25.1]), parameters)
    assert verdict(outcome.frame, 1, "rate_of_change") is None
    assert any("rate_of_change" in warning for warning in outcome.warnings)


# --------------------------------------------------------------------------
# Rows the rate test never actually compared against anything
# --------------------------------------------------------------------------


def test_the_first_row_of_a_series_gets_no_rate_of_change_verdict(tmp_path):
    """There is no earlier reading to have changed from, so there is no rate."""
    parameters = registry(tmp_path, qc={"rate_of_change": RATE})
    outcome = evaluate(observations([18.0, 18.1, 18.2]), parameters)
    assert verdict(outcome.frame, 0, "rate_of_change") is None
    assert verdict(outcome.frame, 1, "rate_of_change") == "pass"


def test_a_row_resuming_after_a_gap_gets_no_rate_of_change_verdict(tmp_path):
    """The step is identical either way; only the gap before it differs.

    Recording the second one as a pass would be the worse of the two errors: a
    `qc_flag <= 2` query keeps it, and it keeps it at exactly the discontinuity
    a rate test exists to judge.
    """
    parameters = registry(tmp_path, qc={"rate_of_change": RATE})

    adjacent = evaluate(observations([18.0, 18.1, 25.0, 25.1]), parameters).frame
    assert verdict(adjacent, 2, "rate_of_change") == "fail"

    across_a_gap = evaluate(
        observations([18.0, 18.1, np.nan, np.nan, 25.0, 25.1]), parameters
    ).frame
    assert verdict(across_a_gap, 4, "rate_of_change") is None


def test_suppressing_a_verdict_never_stops_a_missing_row_reading_as_missing(tmp_path):
    """A row inside a gap has a missing predecessor *and* a missing value.

    With no `valid_range` the rate test is the only one with thresholds, so its
    verdicts reach the roll-up alone and nothing else is holding the flag up.
    Suppressing by predecessor alone would drop the second gap row from 9 to 2 --
    a missing value that stops reading as missing.
    """
    parameters = registry(tmp_path, valid_range=None, qc={"rate_of_change": RATE})
    outcome = evaluate(observations([18.0, 18.1, np.nan, np.nan, 25.0], qc_tests=""), parameters)
    assert flags(outcome.frame) == [
        FLAG_NOT_EVALUATED,  # nothing before it to compare against
        FLAG_PASS,
        FLAG_MISSING,
        FLAG_MISSING,  # missing predecessor, but still a missing value first
        FLAG_NOT_EVALUATED,  # resumes after the gap
    ]


# --------------------------------------------------------------------------
# What the roll-up must not do to rows ingest already judged
# --------------------------------------------------------------------------


def test_a_window_failure_is_not_relaxed_by_tests_that_like_the_value(tmp_path):
    """docs/06 s3: a plausible temperature measured in air is still not water."""
    frame = observations([18.0, 18.1, 18.2], qc_tests="deployment_window:fail")
    outcome = evaluate(frame, registry(tmp_path))
    assert flags(outcome.frame) == [FLAG_FAIL, FLAG_FAIL, FLAG_FAIL]
    assert verdict(outcome.frame, 0, "gross_range") == "pass"
    assert verdict(outcome.frame, 0, "deployment_window") == "fail"


def test_a_missing_value_stays_missing(tmp_path):
    outcome = evaluate(observations([18.0, np.nan, 18.2]), registry(tmp_path))
    assert flags(outcome.frame) == [FLAG_PASS, FLAG_MISSING, FLAG_PASS]


def test_a_verdict_from_a_test_that_no_longer_runs_is_dropped(tmp_path):
    """The qc stage owns its own tests' verdicts; a stale one is not evidence."""
    frame = observations([18.0, 18.1, 18.2], qc_tests="deployment_window:pass;spike:fail")
    outcome = evaluate(frame, registry(tmp_path))
    assert verdict(outcome.frame, 0, "spike") is None
    assert flags(outcome.frame) == [FLAG_PASS, FLAG_PASS, FLAG_PASS]


def test_evaluating_twice_changes_nothing(tmp_path):
    parameters = registry(tmp_path, qc={"spike": SPIKE, "rate_of_change": RATE})
    once = evaluate(observations([18.0, 18.0, 24.0, 18.0, 18.0]), parameters).frame
    twice = evaluate(once, parameters).frame
    pd.testing.assert_frame_equal(once, twice)


# --------------------------------------------------------------------------
# What counts as one series
# --------------------------------------------------------------------------


def test_each_site_is_tested_as_its_own_series(tmp_path):
    """A spike at one site must not borrow the neighbouring site's readings."""
    parameters = registry(tmp_path, qc={"spike": SPIKE})
    frame = pd.concat(
        [
            observations([18.0, 18.0, 24.0, 18.0, 18.0], site="PROJ:ONE"),
            observations([18.0, 18.0, 18.0, 18.0, 18.0], site="PROJ:TWO"),
        ],
        ignore_index=True,
    )
    outcome = evaluate(frame, parameters)
    two = outcome.frame.loc[outcome.frame["site_id"] == "PROJ:TWO"]
    assert set(two["qc_flag"]) == {FLAG_PASS}
    assert len(outcome.series) == 2


def test_a_series_that_crosses_a_year_boundary_is_tested_as_one_series(tmp_path):
    """Partitions split by year; a spike test that split with them would see
    a false edge at every new year."""
    parameters = registry(tmp_path, qc={"spike": SPIKE})
    frame = observations([18.0, 18.0, 24.0, 18.0, 18.0], start="2026-12-31 23:40")
    assert frame["timestamp"].dt.year.nunique() == 2

    outcome = evaluate(frame, parameters)
    assert verdict(outcome.frame, 2, "spike") == "fail"
    assert len(outcome.series) == 1


def test_a_series_whose_depth_is_null_is_still_tested(tmp_path):
    """The reviewed deployment has no depth; grouping must not drop it."""
    outcome = evaluate(observations([18.0, 41.0, 18.2], depth=None), registry(tmp_path))
    assert flags(outcome.frame)[1] == FLAG_FAIL
    assert len(outcome.series) == 1


# --------------------------------------------------------------------------
# The index the caller happened to be carrying
# --------------------------------------------------------------------------


def test_a_duplicated_index_is_evaluated_like_a_fresh_one(tmp_path):
    """`pd.concat` without `ignore_index=True` is the ordinary way to get one."""
    parameters = registry(tmp_path, qc={"spike": SPIKE})
    parts = [
        observations([18.0, 18.0, 24.0, 18.0, 18.0], site="PROJ:ONE"),
        observations([18.0, 41.0, 18.0, 18.0, 18.0], site="PROJ:TWO"),
    ]
    duplicated = pd.concat(parts)
    assert not duplicated.index.is_unique

    assert flags(evaluate(duplicated, parameters).frame) == flags(
        evaluate(pd.concat(parts, ignore_index=True), parameters).frame
    )


def test_two_series_that_share_index_labels_are_still_tested_apart(tmp_path):
    """Sharing a label must not make one site's spike the other's neighbour."""
    parameters = registry(tmp_path, qc={"spike": SPIKE})
    frame = pd.concat(
        [
            observations([18.0, 18.0, 24.0, 18.0, 18.0], site="PROJ:ONE"),
            observations([18.0, 18.0, 18.0, 18.0, 18.0], site="PROJ:TWO"),
        ]
    )
    outcome = evaluate(frame, parameters)
    two = outcome.frame.loc[outcome.frame["site_id"] == "PROJ:TWO"]
    assert set(two["qc_flag"]) == {FLAG_PASS}
    assert len(outcome.series) == 2


def test_only_the_qc_columns_change_under_a_duplicated_index(tmp_path):
    """Series of unequal length, so the labels line up on neither side."""
    parts = [observations([18.0, 41.0]), observations([19.0], site="PROJ:TWO")]
    frame = pd.concat(parts)
    outcome = evaluate(frame, registry(tmp_path))

    untouched = [c for c in OBSERVATION_COLUMNS if c not in ("qc_flag", "qc_tests")]
    pd.testing.assert_frame_equal(outcome.frame[untouched], frame[untouched])
    assert flags(outcome.frame) == [FLAG_PASS, FLAG_FAIL, FLAG_PASS]


@pytest.mark.parametrize(
    "index",
    [
        pytest.param(pd.RangeIndex(3), id="range"),
        pytest.param(pd.Index([7, 7, 7]), id="duplicated"),
        pytest.param(pd.Index(["a", "b", "c"]), id="labels"),
    ],
)
def test_the_frame_comes_back_with_the_index_it_was_given(tmp_path, index):
    """docs/03 says nothing about the index, so it is the caller's to keep."""
    frame = observations([18.0, 41.0, 18.2]).set_axis(index)
    outcome = evaluate(frame, registry(tmp_path))
    assert outcome.frame.index.equals(index)
    assert flags(outcome.frame) == [FLAG_PASS, FLAG_FAIL, FLAG_PASS]


# --------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------


def test_a_parameter_the_registry_does_not_know_is_skipped_with_a_warning(tmp_path):
    frame = observations([18.0, 18.1], parameter="chlorophyll_concentration")
    outcome = evaluate(frame, registry(tmp_path))
    assert flags(outcome.frame) == [FLAG_NOT_EVALUATED, FLAG_NOT_EVALUATED]
    assert any("chlorophyll_concentration" in warning for warning in outcome.warnings)


def test_an_empty_frame_is_not_an_error(tmp_path):
    outcome = evaluate(observations([]), registry(tmp_path))
    assert outcome.frame.empty
    assert outcome.series == ()


def test_a_frame_whose_timestamps_are_not_utc_is_refused(tmp_path):
    """The same storage-boundary invariant `write_observations` enforces."""
    frame = observations([18.0, 18.1])
    frame["timestamp"] = frame["timestamp"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="timestamp"):
        evaluate(frame, registry(tmp_path))


def test_each_series_reports_what_ran_for_the_manifest(tmp_path):
    parameters = registry(tmp_path, qc={"spike": SPIKE})
    outcome = evaluate(observations([18.0, 18.0, 24.0, 18.0, 18.0]), parameters)
    (series,) = outcome.series
    assert series.site_id == "PROJ:TEST"
    assert series.parameter == "sea_water_temperature"
    assert series.rows == 5
    assert set(series.tests) == {"gross_range", "spike"}
    assert series.flag_counts == {
        str(FLAG_PASS): 2,
        str(FLAG_SUSPECT): 2,
        str(FLAG_FAIL): 1,
    }


# --------------------------------------------------------------------------
# The reference deployment -- docs/06 s5 check 6
# --------------------------------------------------------------------------


@pytest.fixture
def reference() -> pd.DataFrame:
    """The reviewed HOBO export, normalized exactly as `ingest` leaves it."""
    registry_file = load_registry(REGISTRY_SOURCE / "sites.json")
    parameters = load_parameters(REGISTRY_SOURCE / "parameters.json", sources=SOURCE_NAMES)
    deployment = find_deployments(registry_file, "22506632")[0]
    batch = to_observations(
        hobo_xlsx.parse(ORIGINAL),
        deployment,
        parameters,
        source="project",
        run_id="20260824T000000000Z-ingest",
    )
    return batch.frame


@pytest.fixture
def evaluated(reference) -> pd.DataFrame:
    parameters = load_parameters(REGISTRY_SOURCE / "parameters.json", sources=SOURCE_NAMES)
    return evaluate(reference, parameters).frame.sort_values("timestamp").reset_index(drop=True)


def test_the_gross_range_test_flags_nothing_in_the_reference_deployment(evaluated):
    """docs/06 s5 check 6, and the reason the redundancy below matters."""
    statuses = {parse_tests(text).get("gross_range") for text in evaluated["qc_tests"]}
    assert statuses == {"pass"}


def test_every_in_water_reading_passes_qc(evaluated):
    usable = evaluated.loc[evaluated["qc_flag"] <= 2]
    assert len(usable) == 3022
    assert set(usable["qc_flag"]) == {FLAG_PASS}
    assert round(usable["value"].min(), 2) == 17.76


def test_the_out_of_window_readings_stay_failed(evaluated):
    excluded = evaluated.loc[evaluated["qc_flag"] == FLAG_FAIL]
    assert len(excluded) == 7
    assert round(excluded["value"].min(), 2) == 14.78


def test_the_install_transient_is_caught_twice_over(evaluated):
    """docs/06 s5 check 6 predicts exactly this: the deployment window and the
    QARTOD tests condemning one reading independently."""
    transient = evaluated["value"].idxmin()
    recorded = parse_tests(evaluated["qc_tests"].iloc[transient])
    assert round(evaluated["value"].iloc[transient], 2) == 14.78
    assert recorded["deployment_window"] == "fail"
    assert recorded["gross_range"] == "pass"
    assert recorded["spike"] == "fail"
    assert recorded["rate_of_change"] == "suspect"


# --------------------------------------------------------------------------
# The wave parameters -- asymmetric by decision, not by omission (docs/04 s1)
# --------------------------------------------------------------------------


@pytest.fixture
def committed():
    """The registry as shipped. These assert the *decision*, not just the file."""
    return load_parameters(REGISTRY_SOURCE / "parameters.json", sources=SOURCE_NAMES)


def test_wave_height_carries_spike_and_deliberately_no_rate_of_change(committed):
    qc = committed["wave_significant_height"].qc
    assert (qc.spike.suspect, qc.spike.fail) == (1.0, 2.0)
    assert qc.rate_of_change is None


def test_wave_period_carries_neither_neighbour_test(committed):
    qc = committed["wave_peak_period"].qc
    assert qc.spike is None
    assert qc.rate_of_change is None


def test_a_real_storm_build_passes_the_spike_test(committed):
    """The 2023-02-22 ramp at NDBC:46254, which the threshold has to survive.

    Significant height climbs 2.03 -> 4.86 m in five hours. The spike statistic
    judges a sample against the midpoint of its neighbours, and on a ramp that
    midpoint tracks the ramp, so the steepest value here is 0.43 m -- well under
    the 1.0 m suspect threshold. Flagging this would remove the storm the wave
    data exists to record.
    """
    ramp = [2.03, 2.39, 2.75, 2.99, 3.01, 3.89, 4.00, 4.16, 4.52, 4.86]
    frame = observations(ramp, parameter="wave_significant_height", freq="30min")
    outcome = evaluate(frame, committed)
    inner = [verdict(outcome.frame, i, "spike") for i in range(1, len(ramp) - 1)]
    assert set(inner) == {"pass"}


def test_a_single_sample_excursion_is_suspect_and_its_neighbours_are_not(committed):
    """The 2018-01-11 shape at NDBC:46254: 2.98 m inside a flat 1.3 m sea.

    Its spike statistic is 1.70 m. The neighbours' are 0.85 and 0.895, so unlike
    the temperature thresholds -- where docs/04 s1 records that one bad reading
    costs three rows -- this threshold sits above them and costs exactly one.
    """
    series = [1.36, 1.26, 1.38, 1.33, 2.98, 1.23, 1.27]
    frame = observations(series, parameter="wave_significant_height", freq="30min")
    outcome = evaluate(frame, committed)
    assert verdict(outcome.frame, 4, "spike") == "suspect"
    assert verdict(outcome.frame, 3, "spike") == "pass"
    assert verdict(outcome.frame, 5, "spike") == "pass"


def test_a_hopping_peak_period_draws_no_neighbour_verdict_at_all(committed):
    """A peak period jumping between competing swell trains is not a fault.

    Silence here is the decision docs/04 s1 records, and it is visible in
    `qc_tests` as an omission rather than as a pass the series never earned.
    """
    hops = [8.0, 18.0, 8.5, 17.0, 9.0]
    frame = observations(hops, parameter="wave_peak_period", freq="30min")
    outcome = evaluate(frame, committed)
    rows = range(len(hops))
    assert all(verdict(outcome.frame, i, "spike") is None for i in rows)
    assert all(verdict(outcome.frame, i, "rate_of_change") is None for i in rows)
    assert all(verdict(outcome.frame, i, "gross_range") == "pass" for i in rows)
