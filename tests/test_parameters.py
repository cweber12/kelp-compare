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


#: A source vocabulary, invented here. The parser checks `by_source` names
#: against whatever set its caller hands it and knows nothing about where the
#: project's real sources are enumerated (ADR-008), which is the property this
#: fake vocabulary states.
SOURCES = ("ndbc", "project", "sio_shore_stations")


def one(tmp_path: Path, qc: dict | None = None, *, sources=SOURCES, **extra):
    """A single-parameter registry, loaded, returning the `Parameter`."""
    record = {"unit": "degC", "valid_range": [5.0, 35.0], **extra}
    if qc is not None:
        record["qc"] = qc
    return load_parameters(registry(tmp_path, {"sea_water_temperature": record}), sources=sources)[
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


# --------------------------------------------------------------------------
# Per-source exceptions -- ADR-008
# --------------------------------------------------------------------------

SPIKE_AND_RATE = {
    "spike": {"suspect": 1.5, "fail": 3.0},
    "rate_of_change": {"suspect_per_hour": 18.0, "fail_per_hour": 36.0},
}


def test_a_source_may_be_excepted_from_a_test_the_parameter_declares(tmp_path):
    parameter = one(
        tmp_path,
        qc={**SPIKE_AND_RATE, "by_source": {"sio_shore_stations": {"spike": None}}},
    )
    assert parameter.qc.suppressed_for("sio_shore_stations") == {"spike"}


def test_the_thresholds_themselves_are_untouched_by_an_exception(tmp_path):
    """An exception subtracts a test; it never edits a number (ADR-008)."""
    parameter = one(
        tmp_path,
        qc={**SPIKE_AND_RATE, "by_source": {"sio_shore_stations": {"spike": None}}},
    )
    assert parameter.qc.spike.suspect == 1.5
    assert parameter.qc.rate_of_change.suspect_per_hour == 18.0


def test_one_entry_may_except_a_source_from_more_than_one_test(tmp_path):
    parameter = one(
        tmp_path,
        qc={
            **SPIKE_AND_RATE,
            "by_source": {"sio_shore_stations": {"spike": None, "rate_of_change": None}},
        },
    )
    assert parameter.qc.suppressed_for("sio_shore_stations") == {"spike", "rate_of_change"}


def test_a_source_with_no_entry_has_nothing_suppressed(tmp_path):
    parameter = one(
        tmp_path,
        qc={**SPIKE_AND_RATE, "by_source": {"sio_shore_stations": {"spike": None}}},
    )
    assert parameter.qc.suppressed_for("ndbc") == frozenset()
    assert parameter.qc.suppressed_for(None) == frozenset()


def test_gross_range_may_be_excepted_because_valid_range_is_what_runs_it(tmp_path):
    """The one test with no block of its own is still a test a source can lose."""
    parameter = one(tmp_path, qc={"by_source": {"ndbc": {"gross_range": None}}})
    assert parameter.qc.suppressed_for("ndbc") == {"gross_range"}


def test_the_declared_exceptions_are_enumerable_for_a_run(tmp_path):
    """A run has to notice an exception that matched nothing, so it can list them."""
    parameters = load_parameters(
        registry(
            tmp_path,
            {
                "sea_water_temperature": {
                    "unit": "degC",
                    "valid_range": [5.0, 35.0],
                    "qc": {
                        **SPIKE_AND_RATE,
                        "by_source": {
                            "sio_shore_stations": {"rate_of_change": None, "spike": None}
                        },
                    },
                },
                "air_temperature": {"unit": "degC", "valid_range": [-10.0, 50.0]},
            },
        ),
        sources=SOURCES,
    )
    (exception,) = parameters.source_exceptions
    assert exception.parameter == "sea_water_temperature"
    assert exception.source == "sio_shore_stations"
    assert exception.tests == ("rate_of_change", "spike")


def test_a_parameter_with_no_exceptions_declares_none(tmp_path):
    assert one(tmp_path, qc=SPIKE_AND_RATE).qc.by_source == {}


# --------------------------------------------------------------------------
# Refusing rather than ignoring, again -- an unapplied exception is the bug
# --------------------------------------------------------------------------


def test_a_source_name_the_caller_does_not_know_is_refused(tmp_path):
    """The typo whose silent failure looks exactly like the bug regressing."""
    with pytest.raises(ValueError, match="sio_shore_station"):
        one(
            tmp_path,
            qc={**SPIKE_AND_RATE, "by_source": {"sio_shore_station": {"spike": None}}},
        )


def test_a_refused_source_name_lists_the_ones_the_caller_knows(tmp_path):
    with pytest.raises(ValueError, match="ndbc, project, sio_shore_stations"):
        one(tmp_path, qc={**SPIKE_AND_RATE, "by_source": {"typo": {"spike": None}}})


def test_an_exception_supplying_a_threshold_instead_of_null_is_refused(tmp_path):
    """Removal only: the format cannot express a second set of numbers."""
    with pytest.raises(ValueError, match="never"):
        one(
            tmp_path,
            qc={
                **SPIKE_AND_RATE,
                "by_source": {"sio_shore_stations": {"spike": {"suspect": 5.0}}},
            },
        )


@pytest.mark.parametrize("expected", ["sea_water_temperature", "sio_shore_stations", "parameters"])
def test_a_supplied_threshold_names_the_parameter_the_source_and_the_file(tmp_path, expected):
    with pytest.raises(ValueError, match=expected):
        one(
            tmp_path,
            qc={**SPIKE_AND_RATE, "by_source": {"sio_shore_stations": {"spike": 5.0}}},
        )


def test_excepting_a_source_from_a_test_the_qc_stage_does_not_run_is_refused(tmp_path):
    with pytest.raises(ValueError, match="flat_line"):
        one(
            tmp_path,
            qc={**SPIKE_AND_RATE, "by_source": {"ndbc": {"flat_line": None}}},
        )


def test_excepting_a_source_from_a_test_the_parameter_never_runs_is_refused(tmp_path):
    """It would remove nothing, and read in the registry as though it did."""
    with pytest.raises(ValueError, match="spike"):
        one(tmp_path, qc={"by_source": {"ndbc": {"spike": None}}})


def test_excepting_gross_range_where_there_is_no_valid_range_is_refused(tmp_path):
    with pytest.raises(ValueError, match="gross_range"):
        one(tmp_path, valid_range=None, qc={"by_source": {"ndbc": {"gross_range": None}}})


def test_an_exception_block_with_no_sources_in_it_is_refused(tmp_path):
    with pytest.raises(ValueError, match="by_source"):
        one(tmp_path, qc={**SPIKE_AND_RATE, "by_source": {}})


def test_a_source_excepted_from_nothing_is_refused(tmp_path):
    with pytest.raises(ValueError, match="nothing"):
        one(tmp_path, qc={**SPIKE_AND_RATE, "by_source": {"ndbc": {}}})


def test_exceptions_cannot_be_read_without_a_source_vocabulary(tmp_path):
    """A caller that supplied no vocabulary gets a refusal, not an unchecked name."""
    with pytest.raises(ValueError, match="sources="):
        one(
            tmp_path,
            sources=None,
            qc={**SPIKE_AND_RATE, "by_source": {"sio_shore_stations": {"spike": None}}},
        )


def test_a_registry_with_no_exceptions_loads_without_a_source_vocabulary(tmp_path):
    """Units and ranges are most callers' whole interest in this file."""
    assert one(tmp_path, sources=None, qc=SPIKE_AND_RATE).unit == "degC"
