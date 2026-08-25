"""`parameters.json` as the home for QC thresholds (docs/03, ADR-004).

ADR-004 puts threshold tuning in the parameter registry rather than in code, so
this module is where a mis-declared threshold has to be caught. The bias
throughout is to refuse rather than to ignore: a typo in a key name would
silently disable a QARTOD test, and a test that quietly never runs is
indistinguishable in the stored flags from a test that ran and passed.

Every case builds its own registry in `tmp_path`; the committed
`data/registry/parameters.json` is exercised through the ingest and qc tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kelpcompare.parameters import load_parameters


def registry(tmp_path: Path, parameters: dict) -> Path:
    target = tmp_path / "parameters.json"
    target.write_text(json.dumps({"parameters": parameters}), encoding="utf-8")
    return target


def one(tmp_path: Path, qc: dict | None = None, **extra):
    """A single-parameter registry, loaded, returning the `Parameter`."""
    record = {"unit": "degC", "valid_range": [5.0, 35.0], **extra}
    if qc is not None:
        record["qc"] = qc
    return load_parameters(registry(tmp_path, {"sea_water_temperature": record}))[
        "sea_water_temperature"
    ]


# --------------------------------------------------------------------------
# What the loader already promised
# --------------------------------------------------------------------------


def test_the_canonical_unit_and_valid_range_load(tmp_path):
    parameter = one(tmp_path)
    assert parameter.unit == "degC"
    assert parameter.valid_range == (5.0, 35.0)


def test_an_unknown_parameter_names_the_file_it_is_missing_from(tmp_path):
    parameters = load_parameters(registry(tmp_path, {"sea_water_temperature": {"unit": "degC"}}))
    with pytest.raises(KeyError, match="parameters.json"):
        parameters["chlorophyll_concentration"]


# --------------------------------------------------------------------------
# The qc block -- docs/03 "QC thresholds live here, not in code"
# --------------------------------------------------------------------------


def test_a_parameter_with_no_qc_block_declares_no_thresholds(tmp_path):
    """Absent is a decision: gross range still runs, the others do not."""
    qc = one(tmp_path).qc
    assert qc.spike is None
    assert qc.rate_of_change is None
    assert qc.gross_range is None


def test_spike_thresholds_load_in_the_parameters_own_unit(tmp_path):
    parameter = one(tmp_path, qc={"spike": {"suspect": 1.5, "fail": 3.0}})
    assert parameter.qc.spike.suspect == 1.5
    assert parameter.qc.spike.fail == 3.0


def test_a_spike_block_may_declare_only_one_threshold(tmp_path):
    """QARTOD allows a suspect-only or fail-only spike test; so do we."""
    parameter = one(tmp_path, qc={"spike": {"suspect": 1.5}})
    assert parameter.qc.spike.suspect == 1.5
    assert parameter.qc.spike.fail is None


def test_rate_of_change_converts_the_declared_per_hour_rate_to_per_second(tmp_path):
    """The registry speaks per hour; `ioos_qc` takes per second (docs/03)."""
    parameter = one(
        tmp_path, qc={"rate_of_change": {"suspect_per_hour": 18.0, "fail_per_hour": 36.0}}
    )
    rate = parameter.qc.rate_of_change
    assert rate.suspect_per_hour == 18.0
    assert rate.suspect_per_second == pytest.approx(0.005)
    assert rate.fail_per_second == pytest.approx(0.01)


def test_an_undeclared_rate_threshold_stays_undeclared_in_both_units(tmp_path):
    parameter = one(tmp_path, qc={"rate_of_change": {"suspect_per_hour": 18.0}})
    assert parameter.qc.rate_of_change.fail_per_hour is None
    assert parameter.qc.rate_of_change.fail_per_second is None


def test_a_gross_range_suspect_span_narrows_the_valid_range(tmp_path):
    parameter = one(tmp_path, qc={"gross_range": {"suspect_span": [8.0, 30.0]}})
    assert parameter.qc.gross_range.suspect_span == (8.0, 30.0)


# --------------------------------------------------------------------------
# Refusing rather than ignoring
# --------------------------------------------------------------------------


def test_a_misspelled_threshold_key_is_refused_not_ignored(tmp_path):
    """The failure this whole module exists to prevent: a test silently off."""
    with pytest.raises(ValueError, match="suspct"):
        one(tmp_path, qc={"spike": {"suspct": 1.5, "fail": 3.0}})


def test_a_threshold_block_with_nothing_in_it_is_refused(tmp_path):
    with pytest.raises(ValueError, match="spike"):
        one(tmp_path, qc={"spike": {}})


def test_a_test_the_qc_stage_does_not_run_is_refused(tmp_path):
    """`flat_line` is deferred (docs/04 s1). Declaring it would be a lie."""
    with pytest.raises(ValueError, match="flat_line"):
        one(tmp_path, qc={"flat_line": {"suspect_seconds": 7200}})


def test_a_refusal_names_the_file_and_the_parameter(tmp_path):
    with pytest.raises(ValueError, match="sea_water_temperature"):
        one(tmp_path, qc={"spike": {}})
    with pytest.raises(ValueError, match="parameters.json"):
        one(tmp_path, qc={"spike": {}})


def test_a_gross_range_suspect_span_must_have_two_bounds(tmp_path):
    with pytest.raises(ValueError, match="suspect_span"):
        one(tmp_path, qc={"gross_range": {"suspect_span": [8.0]}})
