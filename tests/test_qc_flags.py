"""The flag roll-up: how per-test verdicts become `qc_flag` + `qc_tests` (docs/03).

These are the semantics CLAUDE.md hard rule 4 rests on. QC never deletes a row,
so the flag is the only thing standing between a bad reading and an analysis --
and the default filter is `qc_flag <= 2`, which means a wrongly-suspect row is
silently dropped from the science just as surely as a deleted one would be.

Two properties matter more than any single case here:

* a verdict already recorded cannot be relaxed by a later test that likes the
  row (docs/06 s3 -- an out-of-window install transient stays failed), and
* `qc_flag` is always the roll-up of exactly what `qc_tests` records, which is
  why one function produces both.
"""

from __future__ import annotations

import numpy as np
import pytest

from kelpcompare.qc.flags import format_tests, parse_tests, recorded_verdicts, summarize
from kelpcompare.storage import (
    FLAG_FAIL,
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    FLAG_PASS,
    FLAG_SUSPECT,
)

PASS = np.array([FLAG_PASS])
SUSPECT = np.array([FLAG_SUSPECT])
FAIL = np.array([FLAG_FAIL])
MISSING = np.array([FLAG_MISSING])
UNKNOWN = np.array([FLAG_NOT_EVALUATED])


def flag_of(**verdicts) -> int:
    """The rolled-up flag for a single row described by keyword verdicts."""
    flags, _ = summarize(verdicts, rows=1)
    return int(flags[0])


def qc_tests_of(**verdicts) -> str:
    _, text = summarize(verdicts, rows=1)
    return str(text[0])


# --------------------------------------------------------------------------
# The `name:status` encoding
# --------------------------------------------------------------------------


def test_a_single_verdict_parses():
    assert parse_tests("deployment_window:pass") == {"deployment_window": "pass"}


def test_verdicts_are_separated_by_semicolons():
    parsed = parse_tests("deployment_window:fail;spike:suspect")
    assert parsed == {"deployment_window": "fail", "spike": "suspect"}


def test_a_row_with_no_recorded_verdicts_parses_to_nothing():
    assert parse_tests("") == {}
    assert parse_tests(None) == {}


def test_an_unparseable_verdict_is_refused_rather_than_dropped():
    """Dropping it would silently relax whatever verdict it recorded."""
    with pytest.raises(ValueError, match="deployment_window"):
        parse_tests("deployment_window")


def test_an_unknown_status_word_is_refused():
    with pytest.raises(ValueError, match="probably"):
        parse_tests("spike:probably")


def test_verdicts_serialize_in_a_fixed_order_whatever_order_they_arrive_in():
    """Deterministic strings keep partition files byte-stable across reruns.

    `rate_of_change` earns its place here: it is the one pair in the documented
    order that alphabetical sorting would swap. Without it the assertion holds
    just as well against an accidentally-alphabetical order, and a change that
    rewrote every stored `qc_tests` string in the zone would go unnoticed.
    """
    text = format_tests(
        {
            "spike": "pass",
            "rate_of_change": "suspect",
            "deployment_window": "fail",
            "gross_range": "pass",
        }
    )
    assert text == "deployment_window:fail;gross_range:pass;spike:pass;rate_of_change:suspect"


def test_a_test_outside_the_known_order_still_serializes_deterministically():
    text = format_tests({"zzz_future_test": "pass", "deployment_window": "pass"})
    assert text == "deployment_window:pass;zzz_future_test:pass"


def test_the_encoding_round_trips():
    verdicts = {"deployment_window": "fail", "spike": "suspect", "gross_range": "pass"}
    assert parse_tests(format_tests(verdicts)) == verdicts


# --------------------------------------------------------------------------
# Rolling verdicts up -- docs/03 `qc_flag`
# --------------------------------------------------------------------------


def test_a_row_every_test_liked_passes():
    assert flag_of(deployment_window=PASS, gross_range=PASS, spike=PASS) == FLAG_PASS


def test_one_suspect_verdict_makes_the_row_suspect():
    assert flag_of(gross_range=PASS, spike=SUSPECT) == FLAG_SUSPECT


def test_one_failed_verdict_outranks_any_number_of_passes():
    assert flag_of(gross_range=PASS, spike=PASS, rate_of_change=FAIL) == FLAG_FAIL


def test_a_failure_outranks_a_suspicion():
    assert flag_of(spike=SUSPECT, rate_of_change=FAIL) == FLAG_FAIL


def test_a_row_no_test_could_judge_is_not_evaluated():
    assert flag_of(spike=UNKNOWN, rate_of_change=UNKNOWN) == FLAG_NOT_EVALUATED
    assert flag_of() == FLAG_NOT_EVALUATED


def test_a_missing_value_outranks_every_other_verdict():
    """Deliberately unlike `ioos_qc.qartod_compare`, which ranks MISSING lowest.

    docs/03 gives 9 to a row whose value is absent. There is nothing there to
    judge, so a window verdict about where the instrument was must not turn the
    absence into a measurement that failed.
    """
    assert flag_of(deployment_window=FAIL, gross_range=MISSING) == FLAG_MISSING


# --------------------------------------------------------------------------
# What must never happen: a recorded verdict relaxed
# --------------------------------------------------------------------------


def test_a_window_failure_survives_a_test_that_likes_the_value():
    """docs/06 s3: the install transient is a plausible reading taken in air."""
    assert flag_of(deployment_window=FAIL, gross_range=PASS, spike=PASS) == FLAG_FAIL


def test_the_flag_always_matches_what_the_tests_column_records():
    flags, text = summarize({"deployment_window": FAIL, "gross_range": PASS}, rows=1)
    assert int(flags[0]) == FLAG_FAIL
    assert parse_tests(str(text[0])) == {"deployment_window": "fail", "gross_range": "pass"}


def test_a_test_that_reached_no_verdict_is_left_out_of_the_record():
    """A spike test reaches no verdict at the ends of a series; say nothing."""
    assert qc_tests_of(gross_range=PASS, spike=UNKNOWN) == "gross_range:pass"


def test_a_row_with_no_verdicts_records_an_empty_string():
    assert qc_tests_of() == ""


# --------------------------------------------------------------------------
# Reading verdicts back out of stored rows
# --------------------------------------------------------------------------


def test_verdicts_already_stored_are_recovered_as_flags():
    verdicts = recorded_verdicts(
        ["deployment_window:pass", "deployment_window:fail", "deployment_window:pass"]
    )
    assert list(verdicts["deployment_window"]) == [FLAG_PASS, FLAG_FAIL, FLAG_PASS]


def test_a_row_that_never_recorded_a_test_reads_back_as_no_verdict():
    verdicts = recorded_verdicts(["deployment_window:pass;spike:fail", "deployment_window:pass"])
    assert list(verdicts["spike"]) == [FLAG_FAIL, FLAG_NOT_EVALUATED]


def test_rerunning_a_test_replaces_only_its_own_verdict():
    """The merge the qc stage performs: recorded verdicts, then fresh ones."""
    stored = recorded_verdicts(["deployment_window:fail;spike:suspect"])
    flags, text = summarize({**stored, "spike": PASS}, rows=1)
    assert parse_tests(str(text[0])) == {"deployment_window": "fail", "spike": "pass"}
    assert int(flags[0]) == FLAG_FAIL


def test_verdicts_are_recovered_for_a_column_that_recorded_nothing():
    assert recorded_verdicts(["", None]) == {}
