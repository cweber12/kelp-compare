"""`features.json` as the home for feature configuration (docs/04 s2-s3, ADR-006).

ADR-006 puts the coverage floor, the climatology baseline, and the ecological
thresholds in a registry file rather than in code, so this module is where a
mis-declared one has to be caught. The bias is the parameter registry's: refuse
rather than ignore. A feature that silently did not get built is
indistinguishable, in the output table, from one that was built and came out
null -- and because the builder derives its column names from these thresholds,
a typo would rename a column rather than fail.

Every case builds its own configuration in `tmp_path`. The committed
`data/registry/features.json` gets one test of its own, because the defaults it
ships are what docs/03 promises the output columns are called.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kelpcompare.features.config import load_feature_config
from kelpcompare.parameters import load_parameters

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED = REPO_ROOT / "data" / "registry" / "features.json"

POLICY = {
    "coverage_floor": 0.6,
    "baseline": {"start_year": 2007, "end_year": 2019, "min_years": 10},
}
TEMPERATURE = {
    "feature_set": "temperature",
    "thresholds": {"days_above": [20.0, 23.0], "days_below": [14.0]},
}


#: Distinct from `{}`, which several cases pass on purpose to mean "declares nothing".
UNSET = object()


def config(tmp_path: Path, *, policy=POLICY, parameters=UNSET, **extra):
    """Write a configuration and load it, so a raise names a real file."""
    if parameters is UNSET:
        parameters = {"sea_water_temperature": TEMPERATURE}
    payload = {"policy": policy, "parameters": parameters}
    target = tmp_path / "features.json"
    target.write_text(json.dumps({**payload, **extra}), encoding="utf-8")
    return load_feature_config(target)


def refuses(tmp_path: Path, **kwargs) -> str:
    with pytest.raises(ValueError) as raised:
        config(tmp_path, **kwargs)
    message = str(raised.value)
    assert str(tmp_path / "features.json") in message, (
        f"the message does not name the file: {message}"
    )
    return message


# --------------------------------------------------------------------------
# What loads
# --------------------------------------------------------------------------


def test_the_policy_loads(tmp_path):
    loaded = config(tmp_path)
    assert loaded.coverage_floor == 0.6
    assert (loaded.baseline.start_year, loaded.baseline.end_year) == (2007, 2019)
    assert loaded.baseline.min_years == 10
    assert loaded.baseline.label == "2007-2019"
    assert loaded.baseline.span == 13


def test_the_baseline_window_is_inclusive_at_both_ends(tmp_path):
    baseline = config(tmp_path).baseline
    assert baseline.contains(2007) and baseline.contains(2019)
    assert not baseline.contains(2006) and not baseline.contains(2020)


def test_a_parameter_declares_its_feature_set_and_thresholds(tmp_path):
    entry = config(tmp_path).get("sea_water_temperature")
    assert entry.feature_set == "temperature"
    assert entry.of("days_above") == (20.0, 23.0)
    assert entry.of("days_below") == (14.0,)


def test_a_threshold_kind_that_was_not_declared_is_empty_never_a_default(tmp_path):
    """The parameter registry's rule, one layer up: silence is not a guess."""
    assert config(tmp_path).get("sea_water_temperature").of("degree_days_above") == ()


def test_a_parameter_with_no_entry_is_absent_rather_than_defaulted(tmp_path):
    loaded = config(tmp_path)
    assert "wind_speed" not in loaded
    assert loaded.get("wind_speed") is None


def test_the_statistics_set_is_universal_and_takes_no_thresholds(tmp_path):
    loaded = config(tmp_path, parameters={"wind_speed": {"feature_set": "statistics"}})
    assert loaded.get("wind_speed").feature_set == "statistics"
    assert loaded.get("wind_speed").thresholds == {}


def test_comment_keys_are_ignored_at_every_level(tmp_path):
    """The shipped file explains itself in `_comment` keys; they are not config."""
    policy = {**POLICY, "_comment": "why this floor", "baseline": {**POLICY["baseline"], "_c": "x"}}
    loaded = config(
        tmp_path,
        policy=policy,
        parameters={"sea_water_temperature": {**TEMPERATURE, "_comment": "why"}},
        _comment="top level",
    )
    assert loaded.coverage_floor == 0.6
    assert loaded.baseline.min_years == 10


# --------------------------------------------------------------------------
# What it refuses -- and says which key
# --------------------------------------------------------------------------


def test_an_unknown_top_level_key_is_refused(tmp_path):
    assert "'thresholds'" in refuses(tmp_path, thresholds={})


def test_an_unknown_policy_key_is_refused(tmp_path):
    assert "coverage_floo" in refuses(tmp_path, policy={**POLICY, "coverage_floo": 0.6})


def test_a_missing_policy_is_refused(tmp_path):
    assert "policy" in refuses(tmp_path, policy={})


def test_a_coverage_floor_outside_zero_to_one_is_refused(tmp_path):
    assert "60.0" in refuses(tmp_path, policy={**POLICY, "coverage_floor": 60})


def test_a_baseline_missing_a_key_is_refused(tmp_path):
    assert "min_years" in refuses(
        tmp_path, policy={**POLICY, "baseline": {"start_year": 2007, "end_year": 2019}}
    )


def test_a_baseline_that_ends_before_it_starts_is_refused(tmp_path):
    baseline = {"start_year": 2019, "end_year": 2007, "min_years": 3}
    assert "before it starts" in refuses(tmp_path, policy={**POLICY, "baseline": baseline})


def test_a_minimum_the_window_cannot_reach_is_refused(tmp_path):
    """Otherwise every anomaly is null for a reason nothing in the output states."""
    baseline = {"start_year": 2007, "end_year": 2010, "min_years": 10}
    assert "no anomaly could ever be computed" in refuses(
        tmp_path, policy={**POLICY, "baseline": baseline}
    )


def test_a_baseline_year_that_is_not_a_year_is_refused(tmp_path):
    baseline = {"start_year": "2007", "end_year": 2019, "min_years": 10}
    assert "not a whole year" in refuses(tmp_path, policy={**POLICY, "baseline": baseline})


def test_a_configuration_that_builds_nothing_is_refused(tmp_path):
    assert "half-finished edit" in refuses(tmp_path, parameters={})


def test_a_parameter_with_no_feature_set_is_refused(tmp_path):
    """Declared, never inferred from the unit -- docs/03's rule for parameters."""
    message = refuses(tmp_path, parameters={"sea_water_temperature": {"thresholds": {}}})
    assert "feature_set" in message


def test_an_unimplemented_feature_set_is_refused_rather_than_skipped(tmp_path):
    """A declared-but-unbuilt set would sit in the registry looking like coverage."""
    message = refuses(tmp_path, parameters={"wave_significant_height": {"feature_set": "waves"}})
    assert "does not build" in message
    assert "fetcher that would feed it does not exist" in message


def test_a_feature_set_nobody_has_heard_of_is_refused_without_promising_it_later(tmp_path):
    message = refuses(tmp_path, parameters={"sea_water_temperature": {"feature_set": "vibes"}})
    assert "'vibes'" in message
    assert "does not exist yet" not in message


def test_an_unknown_threshold_kind_is_refused(tmp_path):
    entry = {"feature_set": "temperature", "thresholds": {"days_over": [20.0]}}
    assert "days_over" in refuses(tmp_path, parameters={"sea_water_temperature": entry})


def test_a_temperature_entry_with_no_thresholds_is_refused(tmp_path):
    entry = {"feature_set": "temperature", "thresholds": {}}
    message = refuses(tmp_path, parameters={"sea_water_temperature": entry})
    assert "statistics" in message


def test_thresholds_on_a_set_that_takes_none_are_refused(tmp_path):
    entry = {"feature_set": "statistics", "thresholds": {"days_above": [20.0]}}
    assert "takes none" in refuses(tmp_path, parameters={"wind_speed": entry})


def test_an_empty_threshold_list_is_refused(tmp_path):
    entry = {"feature_set": "temperature", "thresholds": {"days_above": []}}
    assert "non-empty list" in refuses(tmp_path, parameters={"sea_water_temperature": entry})


def test_a_threshold_that_is_not_a_number_is_refused(tmp_path):
    entry = {"feature_set": "temperature", "thresholds": {"days_above": ["20c"]}}
    assert "not a number" in refuses(tmp_path, parameters={"sea_water_temperature": entry})


def test_a_threshold_declared_twice_is_refused(tmp_path):
    """Two identical thresholds would name one column twice."""
    entry = {"feature_set": "temperature", "thresholds": {"days_above": [20.0, 20.0]}}
    assert "twice" in refuses(tmp_path, parameters={"sea_water_temperature": entry})


def test_an_empty_parameter_entry_is_refused(tmp_path):
    assert "empty entry" in refuses(tmp_path, parameters={"sea_water_temperature": {}})


# --------------------------------------------------------------------------
# The committed file
# --------------------------------------------------------------------------


def test_the_committed_configuration_declares_the_docs_04_thresholds():
    """docs/03 names the output columns; these values are what produce those names."""
    loaded = load_feature_config(COMMITTED)
    temperature = loaded.get("sea_water_temperature")
    assert temperature.feature_set == "temperature"
    assert temperature.of("days_above") == (20.0, 23.0)
    assert temperature.of("max_spell_above") == (20.0,)
    assert temperature.of("degree_days_above") == (18.0,)
    assert temperature.of("days_below") == (14.0,)


def test_the_committed_configuration_uses_the_baseline_the_prd_settled():
    """2007-2019, not the 1984-2013 docs/04 s3 proposed: LJAC1 begins in 2007."""
    baseline = load_feature_config(COMMITTED).baseline
    assert (baseline.start_year, baseline.end_year, baseline.min_years) == (2007, 2019, 10)


def test_the_committed_configuration_covers_every_controlled_parameter():
    """A parameter with no entry is skipped and warned about; none should be."""
    parameters = load_parameters(REPO_ROOT / "data" / "registry" / "parameters.json")
    assert set(load_feature_config(COMMITTED).names) == set(parameters.names)


def test_air_temperature_is_not_given_the_kelp_stress_thresholds():
    """Same unit as sea water, different feature set. Never inferred from `degC`."""
    assert load_feature_config(COMMITTED).get("air_temperature").feature_set == "statistics"
