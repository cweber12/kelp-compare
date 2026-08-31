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

from kelpcompare.features.config import (
    ANALYSIS_ROLES,
    DEFAULT_ANALYSIS_ROLE,
    DEFAULT_NEIGHBOR_DEPTH_TOLERANCE_M,
    load_feature_config,
)
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


def test_the_neighbor_depth_tolerance_defaults_when_absent(tmp_path):
    """Optional rather than required: the default is a documented number, and
    requiring it would invalidate every features.json written before it."""
    loaded = config(tmp_path)

    assert loaded.neighbor_depth_tolerance_m == DEFAULT_NEIGHBOR_DEPTH_TOLERANCE_M


def test_the_neighbor_depth_tolerance_is_read_when_declared(tmp_path):
    loaded = config(tmp_path, policy={**POLICY, "neighbor_depth_tolerance_m": 2.5})

    assert loaded.neighbor_depth_tolerance_m == 2.5


def test_a_zero_neighbor_depth_tolerance_is_allowed(tmp_path):
    """Same-depth-only is a defensible position, not a mistake."""
    loaded = config(tmp_path, policy={**POLICY, "neighbor_depth_tolerance_m": 0})

    assert loaded.neighbor_depth_tolerance_m == 0.0


def test_a_negative_neighbor_depth_tolerance_is_refused(tmp_path):
    """It would make every pair incomparable, so the table would read as a record
    of disagreement rather than of a misconfigured file."""
    with pytest.raises(ValueError, match="neighbor_depth_tolerance_m"):
        config(tmp_path, policy={**POLICY, "neighbor_depth_tolerance_m": -1.0})


def test_a_missing_required_policy_key_is_still_refused(tmp_path):
    """Splitting required from optional must not make the required ones optional."""
    with pytest.raises(ValueError, match="missing"):
        config(tmp_path, policy={"coverage_floor": 0.6})


def test_the_committed_configuration_ships_the_default_tolerance():
    """docs/04 s1 says the file is silent on it until someone disagrees."""
    loaded = load_feature_config(COMMITTED)

    assert loaded.neighbor_depth_tolerance_m == DEFAULT_NEIGHBOR_DEPTH_TOLERANCE_M


# --------------------------------------------------------------------------
# The analysis role (docs/04 s5)
# --------------------------------------------------------------------------


def test_a_parameter_with_no_role_is_a_predictor(tmp_path):
    """Opt-out, not opt-in: a parameter nobody has argued about yet is one the
    screen surfaces, which fails louder than one silently withheld."""
    loaded = config(tmp_path)

    assert loaded.get("sea_water_temperature").role == DEFAULT_ANALYSIS_ROLE
    assert loaded.get("sea_water_temperature").is_control is False


def test_a_declared_control_is_read_as_one(tmp_path):
    loaded = config(
        tmp_path,
        parameters={"air_temperature": {"feature_set": "statistics", "role": "control"}},
    )

    assert loaded.get("air_temperature").is_control is True


def test_a_misspelled_role_is_refused_rather_than_defaulted(tmp_path):
    """Silent in both directions and visible in no output column: `controls`
    would leave a demoted parameter in the pre-registration pool."""
    message = refuses(
        tmp_path,
        parameters={"air_temperature": {"feature_set": "statistics", "role": "controls"}},
    )

    assert "controls" in message
    assert all(role in message for role in ANALYSIS_ROLES)


def test_the_pool_and_the_controls_partition_the_parameters(tmp_path):
    """Every declared parameter is in exactly one of them, so a reader cannot
    lose one to a role that was never considered."""
    loaded = config(
        tmp_path,
        parameters={
            "sea_water_temperature": TEMPERATURE,
            "air_temperature": {"feature_set": "statistics", "role": "control"},
            "wind_speed": {"feature_set": "statistics", "role": "control"},
        },
    )

    assert loaded.predictors == ("sea_water_temperature",)
    assert loaded.controls == ("air_temperature", "wind_speed")
    assert sorted([*loaded.predictors, *loaded.controls]) == list(loaded.names)


def test_roles_reports_every_parameter_including_the_defaulted_ones(tmp_path):
    """A screened row is labelled from whatever the table carries, so the
    mapping has to answer for a parameter that declared nothing."""
    loaded = config(
        tmp_path,
        parameters={
            "sea_water_temperature": TEMPERATURE,
            "wind_speed": {"feature_set": "statistics", "role": "control"},
        },
    )

    assert loaded.roles() == {
        "sea_water_temperature": "predictor",
        "wind_speed": "control",
    }


def test_a_role_does_not_change_what_gets_built(tmp_path):
    """A control is aggregated exactly as a predictor is; the role governs the
    analysis, never the pipeline, so demoting one cannot delete a row."""
    demoted = config(
        tmp_path,
        parameters={"sea_water_temperature": {**TEMPERATURE, "role": "control"}},
    ).get("sea_water_temperature")
    kept = config(tmp_path).get("sea_water_temperature")

    assert demoted.feature_set == kept.feature_set
    assert demoted.thresholds == kept.thresholds


def test_the_committed_configuration_demotes_the_met_parameters():
    """docs/04 s5: air temperature re-measures the water at r = 0.857, and scalar
    wind speed averages upwelling-favorable stress against its own negation."""
    loaded = load_feature_config(COMMITTED)

    assert loaded.controls == ("air_temperature", "wind_speed")
    assert "sea_water_temperature" in loaded.predictors


def test_the_demoted_parameters_are_still_built():
    """The demotion is a claim being withheld, not a row. A control keeps its
    feature set, so `kelpcompare features` aggregates it exactly as before."""
    loaded = load_feature_config(COMMITTED)

    for name in loaded.controls:
        assert loaded.get(name).feature_set == "statistics"


# --------------------------------------------------------------------------
# Per-series baseline windows (docs/04 s3)
# --------------------------------------------------------------------------


def overriding(window, *, site_id="NDBC:46254"):
    """A policy declaring one baseline override, everything else canonical."""
    return {**POLICY, "baseline_overrides": {site_id: window}}


def test_no_overrides_declared_leaves_every_series_on_the_canonical_window(tmp_path):
    """The shipped shape: the mechanism exists and changes nothing until used."""
    loaded = config(tmp_path)

    assert loaded.baseline_overrides == {}
    assert loaded.baseline_for("NDBC:LJAC1") == loaded.baseline
    assert loaded.baseline_for() == loaded.baseline


def test_a_declared_override_applies_to_that_site_and_no_other(tmp_path):
    loaded = config(tmp_path, policy=overriding({"start_year": 2016, "end_year": 2025}))

    window = loaded.baseline_for("NDBC:46254")
    assert (window.start_year, window.end_year) == (2016, 2025)
    # The canonical window is untouched, both as the default and for everyone else.
    assert loaded.baseline_for("NDBC:LJAC1") == loaded.baseline
    assert (loaded.baseline.start_year, loaded.baseline.end_year) == (2007, 2019)


def test_an_override_takes_min_years_from_the_canonical_window(tmp_path):
    """How thin is too thin belongs to the method, not to a station."""
    loaded = config(tmp_path, policy=overriding({"start_year": 2016, "end_year": 2025}))

    assert loaded.baseline_for("NDBC:46254").min_years == loaded.baseline.min_years == 10


def test_an_override_declaring_min_years_is_refused(tmp_path):
    message = refuses(
        tmp_path,
        policy=overriding({"start_year": 2016, "end_year": 2025, "min_years": 6}),
    )
    assert "min_years" in message


def test_an_override_narrower_than_min_years_is_refused(tmp_path):
    """A window that could never produce an anomaly is a disabled feature, not a window."""
    message = refuses(tmp_path, policy=overriding({"start_year": 2020, "end_year": 2025}))

    assert "no anomaly could ever be computed" in message
    assert "2020-2025" in message


def test_a_backwards_override_is_refused(tmp_path):
    message = refuses(tmp_path, policy=overriding({"start_year": 2025, "end_year": 2016}))
    assert "before it starts" in message


def test_an_override_missing_a_year_is_refused(tmp_path):
    message = refuses(tmp_path, policy=overriding({"start_year": 2016}))
    assert "end_year" in message


def test_a_fractional_override_year_is_refused_rather_than_truncated(tmp_path):
    message = refuses(tmp_path, policy=overriding({"start_year": 2016.5, "end_year": 2025}))
    assert "not a whole year" in message


def test_an_empty_override_entry_is_refused(tmp_path):
    message = refuses(tmp_path, policy=overriding({}))
    assert "NDBC:46254" in message


def test_an_overrides_block_that_is_not_a_block_is_refused(tmp_path):
    message = refuses(tmp_path, policy={**POLICY, "baseline_overrides": ["NDBC:46254"]})
    assert "site_id -> window" in message


def test_the_error_names_the_site_whose_override_is_wrong(tmp_path):
    """With several declared, a reader has to be told which one to go and fix."""
    policy = {
        **POLICY,
        "baseline_overrides": {
            "NDBC:46254": {"start_year": 2016, "end_year": 2025},
            "NDBC:46266": {"start_year": 2020, "end_year": 2025},
        },
    }
    message = refuses(tmp_path, policy=policy)

    assert "NDBC:46266" in message
    assert "NDBC:46254" not in message


def test_every_committed_baseline_override_is_well_formed():
    """The invariants any declared window must satisfy, whatever is declared.

    Asserted over whatever the file holds rather than against a written list of
    sites, so declaring a window for a station does not edit this case. What it
    still catches is the two ways an override goes wrong silently: naming a
    site the site registry does not have, so the window applies to nothing; and
    drifting away from the project-wide minimum, which docs/04 s3 keeps a
    property of the method rather than of a station.
    """
    loaded = load_feature_config(COMMITTED)
    sites = {
        site["site_id"]
        for site in json.loads(
            (REPO_ROOT / "data" / "registry" / "sites.json").read_text(encoding="utf-8")
        )["sites"]
    }

    for site_id, window in loaded.baseline_overrides.items():
        assert site_id in sites, f"{site_id} has a baseline window but no site record"
        assert window.min_years == loaded.baseline.min_years
        assert window.span >= window.min_years


def test_the_committed_configuration_declares_one_baseline_override():
    """`NDBC:46254` is the only series given a window of its own.

    Its record begins 2015-02-12, so it holds at most five candidate years
    inside the canonical 2007-2019 and can supply no baseline there. 2015-2025
    is its whole record to the last complete year (docs/04 s3, ADR-007).
    """
    loaded = load_feature_config(COMMITTED)

    assert set(loaded.baseline_overrides) == {"NDBC:46254"}
    window = loaded.baseline_for("NDBC:46254")
    assert (window.start_year, window.end_year) == (2015, 2025)


def test_the_committed_override_inherits_the_project_minimum():
    """min_years is not per station, which is what leaves 46254's Q1 null.

    Nine usable Q1s against a minimum of ten, measured on the ingested rows:
    2015 is 0.52 covered because the record starts in February, and 2018 is
    0.20 -- 23 days observed. A per-station minimum would paper over exactly
    that, and the quarter it would paper over is winter.
    """
    loaded = load_feature_config(COMMITTED)

    assert loaded.baseline_for("NDBC:46254").min_years == loaded.baseline.min_years == 10


def test_the_second_waverider_is_left_without_an_override():
    """`NDBC:46266` holds six complete years in every candidate window, so no
    declared window lifts it over the minimum before 2030. Left out on purpose
    rather than given one that could only produce nulls."""
    loaded = load_feature_config(COMMITTED)

    assert "NDBC:46266" not in loaded.baseline_overrides
    assert loaded.baseline_for("NDBC:46266") == loaded.baseline


def test_the_declared_window_ends_at_a_complete_year():
    """Fixed, so next year's data cannot move an anomaly already computed.

    The window ends in 2025 while the record runs into 2026 -- the same shape
    the canonical window has against LJAC1, and the property ADR-007 exists to
    protect.
    """
    window = load_feature_config(COMMITTED).baseline_for("NDBC:46254")

    assert window.end_year == 2025
    assert window.span >= window.min_years
