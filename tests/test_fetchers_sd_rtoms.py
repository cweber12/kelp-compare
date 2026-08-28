"""The City of San Diego RTOMS fetcher (docs/02 "City of San Diego RTOMS").

Two fixtures, and the split between them is the point. The recorded one is a
real CeNCOOS payload and pins the *format* -- the two-line header, the CF unit
token, the profile bins sharing a vertical axis with the temperature sensors.
The hand-built one pins the *edge cases* a one-hour window does not happen to
contain: a suspect and a fail flag, a sensor depth nobody has declared, and the
same physical position reported at two depths across a redeployment.

That mirrors the HOBO pair in `tests/fixtures/` and exists for the same reason:
a fixture edited to contain an edge case can no longer prove what the source
actually sends, so it must not be the only one.

Network access is forbidden here (CLAUDE.md), so `fetch` is exercised through a
stub session and everything else runs off the recorded bytes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.fetchers import sd_rtoms
from kelpcompare.fetchers.base import SourceUnavailable, new_payload
from kelpcompare.parameters import load_parameters

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "sd_rtoms"
RECORDED = FIXTURES / "south-bay-ocean-outfall_2025-06-01T00-01.csv"
EDGE_CASES = FIXTURES / "south-bay-ocean-outfall_edge-cases.csv"

DATASET = "south-bay-ocean-outfall"
SITE = "SDRTOMS:SBOO"
RUN = "RUN-1"

#: What `sites.json` declares for SDRTOMS:SBOO.
DECLARED = (1.0, 10.0, 18.0, 20.0, 25.0, 26.0)
MEASURED = ("sea_water_temperature",)


@pytest.fixture
def parameters():
    return load_parameters(REPO_ROOT / "data" / "registry" / "parameters.json")


def payload(path: Path = RECORDED, *, body: bytes | None = None):
    return new_payload(
        sd_rtoms.SOURCE,
        DATASET,
        path.name,
        f"https://erddap.cencoos.org/erddap/tabledap/{DATASET}.csv",
        path.read_bytes() if body is None else body,
    )


def parse(parameters, path: Path = RECORDED, *, declared=DECLARED, body: bytes | None = None):
    return sd_rtoms.parse(
        payload(path, body=body),
        parameters,
        site_id=SITE,
        declared_depths=declared,
        measured_parameters=MEASURED,
        run_id=RUN,
    )


def edited(path: Path, old: str, new: str) -> bytes:
    """The fixture with one substitution -- so a test says what it changed."""
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} is not in {path.name}; the fixture has moved"
    return text.replace(old, new).encode("utf-8")


# --------------------------------------------------------------------------
# The recorded payload
# --------------------------------------------------------------------------


def test_the_recorded_payload_parses_into_the_docs03_schema(parameters):
    from kelpcompare.storage import OBSERVATION_COLUMNS

    parsed = parse(parameters)
    assert tuple(parsed.frame.columns) == OBSERVATION_COLUMNS
    assert not parsed.frame.empty
    assert set(parsed.frame["site_id"]) == {SITE}
    assert set(parsed.frame["parameter"]) == {"sea_water_temperature"}
    assert set(parsed.frame["source"]) == {sd_rtoms.SOURCE}
    assert set(parsed.frame["fetch_run_id"]) == {RUN}


def test_a_clean_recorded_payload_produces_no_warnings(parameters):
    """The recorded window is ordinary data, so anything reported here is a bug.

    Worth asserting rather than assuming: every warning this module raises is
    about something a human is meant to act on, so a module that cried wolf on
    a normal payload would train the operator to ignore all of them.
    """
    assert parse(parameters).warnings == ()


def test_only_the_declared_sensor_depths_survive_the_profile_axis(parameters):
    """The datasets flatten ADCP velocity bins onto the same z axis as temperature."""
    parsed = parse(parameters)
    assert parsed.rows_in == 293
    assert len(parsed.frame) == 45
    assert set(parsed.frame["depth_m"]) <= set(DECLARED)


def test_depth_is_the_negation_of_z(parameters):
    """z is altitude, positive up; docs/03 depth_m is positive down."""
    parsed = parse(parameters)
    assert (parsed.frame["depth_m"] > 0).all()
    assert parsed.frame["depth_m"].max() == 26.0


def test_timestamps_are_utc(parameters):
    parsed = parse(parameters)
    assert str(parsed.frame["timestamp"].dt.tz) == "UTC"


# --------------------------------------------------------------------------
# QC -- the provider's QARTOD is the docs/03 vocabulary already
# --------------------------------------------------------------------------


def test_the_provider_flags_pass_through_and_its_tests_are_decoded(parameters):
    parsed = parse(parameters, EDGE_CASES)
    by_depth = parsed.frame.set_index(["timestamp", "depth_m"])

    suspect = by_depth.loc[(pd.Timestamp("2025-06-01T00:10:00Z"), 1.0)]
    assert suspect["qc_flag"] == 3
    assert suspect["qc_tests"] == "gross_range:suspect"

    fail = by_depth.loc[(pd.Timestamp("2025-06-01T00:20:00Z"), 1.0)]
    assert fail["qc_flag"] == 4
    assert fail["qc_tests"] == "gross_range:fail"


def test_several_tests_in_one_string_are_all_decoded(parameters):
    parsed = parse(parameters, EDGE_CASES)
    row = parsed.frame.set_index(["timestamp", "depth_m"]).loc[
        (pd.Timestamp("2025-06-01T00:40:00Z"), 1.0)
    ]
    assert set(row["qc_tests"].split(";")) == {
        "gap:pass",
        "gross_range:pass",
        "rate_of_change:suspect",
    }


def test_a_row_with_no_reading_is_missing_whatever_the_provider_called_it(parameters):
    """docs/03 gives 9 to an absent value; this feed writes 2 on many of them.

    The provider is internally inconsistent here -- its own qc_tests records the
    gross range test as 9 on exactly the rows its qc_agg calls 2 -- and docs/03's
    rule decides it. Landing them at 2 would put holes through the default
    `qc_flag <= 2` analysis filter as though they were data.
    """
    parsed = parse(parameters)
    absent = parsed.frame["value"].isna()
    assert absent.any()
    assert (parsed.frame.loc[absent, "qc_flag"] == 9).all()


def test_an_outage_at_a_declared_depth_stays_in_the_record(parameters):
    """Flags, never deletions (hard rule 4). A sensor that did not report is a row."""
    parsed = parse(parameters)
    assert int(parsed.frame["value"].isna().sum()) == 17
    assert parsed.missing_counts["sea_water_temperature"] == 17


def test_a_reading_whose_flag_disagrees_with_its_own_tests_is_reported(parameters):
    parsed = parse(parameters, EDGE_CASES)
    assert any("disagrees with rolling up their own qc_tests" in w for w in parsed.warnings)


def test_an_unknown_flag_stops_the_parse(parameters):
    """A vocabulary change must reach a human, not be stored as though understood."""
    body = edited(
        EDGE_CASES,
        ",1,22212222222\n2025-06-01T00:00:00Z,32.53171,-117.18631,-25.0",
        ",7,22212222222\n2025-06-01T00:00:00Z,32.53171,-117.18631,-25.0",
    )
    with pytest.raises(ValueError, match="not a docs/03 qc_flag"):
        parse(parameters, EDGE_CASES, body=body)


# --------------------------------------------------------------------------
# The declared depth set is a check, not a source of values
# --------------------------------------------------------------------------


def test_an_undeclared_depth_carrying_a_reading_is_refused_and_named(parameters):
    """A refit puts a sensor somewhere new; depth_m is permanent once it lands."""
    parsed = parse(parameters, EDGE_CASES)
    assert 99.0 not in set(parsed.frame["depth_m"])
    (warning,) = [w for w in parsed.warnings if "not declared in sensor_depths_m" in w]
    assert "99 m" in warning


def test_a_nominal_depth_that_drifted_lands_as_two_series(parameters):
    """25 m and 26 m are the same position across a redeployment, and stay apart.

    Rounding them together would write a depth the mooring never reported into a
    field docs/03 makes part of the storage key and therefore permanent.
    """
    parsed = parse(parameters, EDGE_CASES)
    assert {25.0, 26.0} <= set(parsed.frame["depth_m"])


def test_an_undeclared_station_stores_every_reading_and_says_so(parameters):
    """An unrecorded fact must not quietly become missing data."""
    parsed = parse(parameters, EDGE_CASES, declared=())
    assert 99.0 in set(parsed.frame["depth_m"])
    assert any("declares no depths" in w for w in parsed.warnings)


# --------------------------------------------------------------------------
# Format surprises stop the parse
# --------------------------------------------------------------------------


def test_an_unexpected_unit_stops_the_parse(parameters):
    body = edited(RECORDED, "degree_Celsius", "degree_Fahrenheit")
    with pytest.raises(ValueError, match="unverified unit"):
        parse(parameters, RECORDED, body=body)


def test_a_changed_variable_list_stops_the_parse(parameters):
    body = edited(RECORDED, "sea_water_temperature_qc_agg", "sea_water_temperature_qartod")
    with pytest.raises(ValueError, match="variables have changed"):
        parse(parameters, RECORDED, body=body)


def test_a_positive_z_stops_the_parse(parameters):
    """A sign convention flipping upstream would otherwise land negative depths."""
    body = edited(EDGE_CASES, "-1.0,17.976", "1.0,17.976")
    with pytest.raises(ValueError, match="sign convention"):
        parse(parameters, EDGE_CASES, body=body)


def test_a_station_that_does_not_declare_this_parameter_stores_nothing(parameters):
    parsed = sd_rtoms.parse(
        payload(),
        parameters,
        site_id=SITE,
        declared_depths=DECLARED,
        measured_parameters=("wind_speed",),
        run_id=RUN,
    )
    assert parsed.frame.empty
    assert any("does not include" in w for w in parsed.warnings)


# --------------------------------------------------------------------------
# URLs and retrieval
# --------------------------------------------------------------------------


def test_the_archive_window_is_half_open_on_the_right():
    """Two consecutive years must not both claim midnight on 1 January."""
    url = sd_rtoms.archive_url(DATASET, 2023)
    assert "time%3E=2023-01-01T00%3A00%3A00Z" in url
    assert "time%3C2024-01-01T00%3A00%3A00Z" in url


def test_every_requested_variable_is_named_in_the_url():
    url = sd_rtoms.realtime_url(DATASET)
    for variable in sd_rtoms.VARIABLES:
        assert variable in url


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {}


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append((url, headers))
        return self.responses.pop(0)


def test_a_retrieval_carries_the_project_user_agent():
    session = _Session(_Response(200, b"body", {"Last-Modified": "Fri, 28 Aug 2026 21:29:38 GMT"}))
    sd_rtoms.fetch_realtime(DATASET, session=session)
    (_, headers) = session.calls[0]
    assert headers["User-Agent"].startswith("kelpcompare/")


def test_no_conditional_request_is_sent_even_when_validators_are_known():
    """ERDDAP answers If-Modified-Since with the whole body and a 200 (docs/02).

    The argument is accepted so the ingest CLI does not have to know which
    sources support conditional requests, and deliberately does nothing.
    """
    session = _Session(_Response(200, b"body"))
    sd_rtoms.fetch_archive(
        DATASET, 2023, session=session, validators={"etag": "x", "last_modified": "y"}
    )
    (_, headers) = session.calls[0]
    assert "If-None-Match" not in headers
    assert "If-Modified-Since" not in headers


def test_a_window_the_mooring_did_not_report_is_an_outage_not_a_crash():
    session = _Session(_Response(404, b"nothing matched"))
    with pytest.raises(SourceUnavailable):
        sd_rtoms.fetch_archive(DATASET, 1999, session=session)
    assert len(session.calls) == 1, "a 404 is an answer and must not be retried"


def test_a_server_error_is_retried_once_then_recorded_as_a_gap(monkeypatch):
    monkeypatch.setattr(sd_rtoms.time, "sleep", lambda _: None)
    session = _Session(_Response(503), _Response(503))
    with pytest.raises(SourceUnavailable):
        sd_rtoms.fetch_realtime(DATASET, session=session)
    assert len(session.calls) == 2
