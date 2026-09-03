"""The Del Mar shelf mooring fetcher (docs/02 "Del Mar shelf mooring").

Three fixtures, and the split between them is the same one the RTOMS and HOBO
pairs use: a recorded payload can prove what the source actually sends, and a
hand-edited one can contain a case the source does not currently produce, so
neither can do the other's job.

The two recorded windows are both real SCCOOS payloads and are here because this
mooring is not one shape over its record. `2019-01-01` has all nine depth
columns populated; `2010-06-01` has seven of them empty, because six sensors did
not exist until the 2018 refit and the 90 m one until 2016. The second is what
pins the behaviour that matters most for coverage: a declared column with no
readings is an outage that stays in the record flagged missing, not a row to
drop.

The third is hand-edited and is the one thing this source has never sent -- a
correctly-signed longitude. Every real row reports `+117.32` under
`degrees_east` for a mooring at −117.32, so the parse refuses a *negative*
longitude as evidence the provider has fixed its metadata and docs/02 needs
re-reading. That refusal cannot be exercised on recorded bytes by definition.

Network access is forbidden here (CLAUDE.md), so `fetch` is exercised through a
stub session and everything else runs off the recorded bytes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.cli import SOURCE_NAMES
from kelpcompare.fetchers import delmar_mooring
from kelpcompare.fetchers.base import SourceUnavailable, new_payload
from kelpcompare.parameters import load_parameters

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "delmar_mooring"
FULL_STRING = FIXTURES / "delmar_temperature_2019-01-01T00-01.csv"
SPARSE = FIXTURES / "delmar_temperature_2010-06-01T00-01.csv"
SIGN_CORRECTED = FIXTURES / "delmar_temperature_sign-corrected.csv"

DATASET = "delmar_temperature"
SITE = "SCCOOS:DELMAR"

#: What `sites.json` declares for SCCOOS:DELMAR.
DECLARED = (1.0, 6.0, 15.0, 21.0, 32.0, 45.0, 57.0, 72.0, 90.0)


@pytest.fixture
def parameters():
    return load_parameters(
        REPO_ROOT / "data" / "registry" / "parameters.json", sources=SOURCE_NAMES
    )


def payload_from(path: Path, *, label: str = "recorded"):
    return new_payload(
        delmar_mooring.SOURCE,
        DATASET,
        f"{DATASET}_{label}.csv",
        delmar_mooring.archive_url(DATASET, 2019),
        path.read_bytes(),
    )


def parse(path: Path, *, declared=DECLARED, parameters=None, **kwargs):
    return delmar_mooring.parse(
        payload_from(path),
        parameters,
        site_id=SITE,
        declared_depths=declared,
        run_id="test-run",
        **kwargs,
    )


# --------------------------------------------------------------------------
# The melt
# --------------------------------------------------------------------------


def test_each_depth_column_becomes_its_own_series(parameters):
    """Nine columns and four timestamps is thirty-six rows, not four."""
    parsed = parse(FULL_STRING, parameters=parameters)

    assert parsed.rows_in == 4
    assert len(parsed.frame) == 36
    assert sorted(parsed.frame["depth_m"].unique()) == list(DECLARED)
    assert set(parsed.frame["parameter"]) == {"sea_water_temperature"}
    assert set(parsed.frame["source"]) == {"delmar_mooring"}
    assert set(parsed.frame["site_id"]) == {SITE}


def test_the_column_name_is_where_the_depth_comes_from(parameters):
    """`T_32m` is 32 m, and the value on that row is the one under that header."""
    parsed = parse(FULL_STRING, parameters=parameters)
    first = parsed.frame[parsed.frame["timestamp"] == pd.Timestamp("2019-01-01T00:00:00Z")]
    by_depth = dict(zip(first["depth_m"], first["value"], strict=True))

    assert by_depth[1.0] == pytest.approx(16.56)
    assert by_depth[32.0] == pytest.approx(15.8)
    assert by_depth[90.0] == pytest.approx(11.6)


def test_timestamps_are_utc_and_rows_sort_by_time_then_depth(parameters):
    parsed = parse(FULL_STRING, parameters=parameters)

    assert str(parsed.frame["timestamp"].dt.tz) == "UTC"
    assert parsed.frame[["timestamp", "depth_m"]].equals(
        parsed.frame[["timestamp", "depth_m"]].sort_values(["timestamp", "depth_m"])
    )


# --------------------------------------------------------------------------
# A sensor that does not exist yet, versus one that failed
# --------------------------------------------------------------------------


def test_a_declared_column_with_no_readings_stays_in_the_record(parameters):
    """The 2010 window carries T_1m and T_15m; the other seven are empty.

    They are kept and flagged missing rather than dropped. This string reports
    every depth on one clock, so an absence at a declared depth is that sensor's
    own outage -- and dropping it would make `pct_coverage` read a quarter as
    complete when seven ninths of it never existed.
    """
    parsed = parse(SPARSE, parameters=parameters)

    assert parsed.rows_in == 4
    assert len(parsed.frame) == 36
    assert sorted(parsed.frame["depth_m"].unique()) == list(DECLARED)

    missing = parsed.frame[parsed.frame["qc_flag"] == 9]
    assert sorted(missing["depth_m"].unique()) == [6.0, 21.0, 32.0, 45.0, 57.0, 72.0, 90.0]
    assert missing["value"].isna().all()
    assert parsed.missing_counts == {"sea_water_temperature": 28}


def test_readings_land_not_evaluated_because_this_feed_carries_no_verdict(parameters):
    """No `_qc_agg`, no `_qc_tests`, no flag column -- so `kelpcompare qc` is the
    only opinion and every row with a value arrives unjudged."""
    parsed = parse(FULL_STRING, parameters=parameters)

    assert set(parsed.frame["qc_flag"]) == {2}
    assert set(parsed.frame["qc_tests"]) == {""}


# --------------------------------------------------------------------------
# The declared depth set filters columns, not rows
# --------------------------------------------------------------------------


def test_an_undeclared_column_is_refused_by_name_before_a_value_is_read(parameters):
    parsed = parse(FULL_STRING, declared=(1.0, 15.0), parameters=parameters)

    assert sorted(parsed.frame["depth_m"].unique()) == [1.0, 15.0]
    assert len(parsed.frame) == 8
    (warning,) = parsed.warnings
    assert "were NOT stored" in warning
    assert "6 m" in warning and "90 m" in warning


def test_an_empty_declared_set_stores_everything_and_says_so(parameters):
    parsed = parse(FULL_STRING, declared=(), parameters=parameters)

    assert sorted(parsed.frame["depth_m"].unique()) == list(DECLARED)
    (warning,) = parsed.warnings
    assert "declares no depths" in warning


def test_a_site_that_does_not_measure_this_parameter_yields_nothing(parameters):
    parsed = parse(FULL_STRING, parameters=parameters, measured_parameters=("water_level",))

    assert parsed.frame.empty
    assert "does not include 'sea_water_temperature'" in parsed.warnings[0]


# --------------------------------------------------------------------------
# What the parse refuses
# --------------------------------------------------------------------------


def test_a_declared_unit_stops_the_parse(parameters):
    """The pinned absence, inverted from every other fetcher's unit check.

    This provider has never published a unit for these columns, so degC is this
    project's inference. A unit appearing is the provider answering that
    question for the first time and has to be read by a human.
    """
    text = FULL_STRING.read_text(encoding="utf-8").splitlines()
    text[1] = "UTC,degrees_north,degrees_east," + ",".join(["degree_Fahrenheit"] * 9)
    payload = new_payload(
        delmar_mooring.SOURCE,
        DATASET,
        "edited.csv",
        "https://example.invalid",
        "\n".join(text).encode("utf-8"),
    )

    with pytest.raises(ValueError, match="has always declared no unit"):
        delmar_mooring.parse(
            payload, parameters, site_id=SITE, declared_depths=DECLARED, run_id="test-run"
        )


def test_a_corrected_longitude_stops_the_parse(parameters):
    """Refused rather than welcomed, and that is the point.

    Every real row is `+117.32` for a mooring at −117.32. A negative longitude
    means the provider fixed the sign, which is the day docs/02 and sites.json
    stop describing this feed -- so it fails loudly instead of quietly becoming
    correct.
    """
    with pytest.raises(ValueError, match="no longer uniformly positive"):
        parse(SIGN_CORRECTED, parameters=parameters)


def test_a_changed_column_list_stops_the_parse(parameters):
    text = FULL_STRING.read_text(encoding="utf-8").splitlines()
    text[0] = text[0].replace("T_90m", "T_100m")
    payload = new_payload(
        delmar_mooring.SOURCE,
        DATASET,
        "edited.csv",
        "https://example.invalid",
        "\n".join(text).encode("utf-8"),
    )

    with pytest.raises(ValueError, match="the dataset's variables have changed"):
        delmar_mooring.parse(
            payload, parameters, site_id=SITE, declared_depths=DECLARED, run_id="test-run"
        )


def test_a_body_that_is_not_a_tabledap_csv_stops_the_parse(parameters):
    payload = new_payload(
        delmar_mooring.SOURCE, DATASET, "edited.csv", "https://example.invalid", b"time\n"
    )

    with pytest.raises(ValueError, match="a tabledap CSV opens"):
        delmar_mooring.parse(
            payload, parameters, site_id=SITE, declared_depths=DECLARED, run_id="test-run"
        )


# --------------------------------------------------------------------------
# URLs and transport
# --------------------------------------------------------------------------


def test_the_archive_year_is_half_open_on_the_right():
    url = delmar_mooring.archive_url(DATASET, 2019)

    assert "erddap.sccoos.org" in url
    assert "2019-01-01T00%3A00%3A00Z" in url
    assert "time%3C2020-01-01T00%3A00%3A00Z" in url
    assert "time%3C=" not in url


def test_every_requested_variable_is_named_in_the_url():
    url = delmar_mooring.archive_url(DATASET, 2019)

    for variable in delmar_mooring.VARIABLES:
        assert variable in url


def test_the_realtime_window_asks_for_the_same_span_as_other_sources():
    assert f"now-{delmar_mooring.REALTIME_DAYS}days" in delmar_mooring.realtime_url(DATASET)


class _Response:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status at {self.status_code}")


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_an_empty_result_is_an_outage_not_a_retry():
    """ERDDAP answers "nothing matched" with 404 and a text body. That is an
    answer, so it is recorded as a gap and asked only once."""
    session = _Session(_Response(404, b"Error { code=404; }"))

    with pytest.raises(SourceUnavailable, match="matched no rows"):
        delmar_mooring.fetch_archive(DATASET, 2013, session=session)

    assert len(session.calls) == 1


def test_the_realtime_outage_says_the_record_is_closed():
    """A bare "no rows" would read as an outage. This record ends 2021-05-05."""
    session = _Session(_Response(404, b"Error { code=404; }"))

    with pytest.raises(SourceUnavailable, match="record ends 2021-05-05"):
        delmar_mooring.fetch_realtime(DATASET, session=session)


def test_a_server_error_is_retried_once_then_recorded_as_an_outage(monkeypatch):
    monkeypatch.setattr(delmar_mooring.time, "sleep", lambda _: None)
    session = _Session(_Response(503), _Response(503))

    with pytest.raises(SourceUnavailable, match="HTTP 503"):
        delmar_mooring.fetch_archive(DATASET, 2019, session=session)

    assert len(session.calls) == 2


def test_a_retry_that_succeeds_returns_the_payload(monkeypatch):
    monkeypatch.setattr(delmar_mooring.time, "sleep", lambda _: None)
    body = FULL_STRING.read_bytes()
    session = _Session(_Response(503), _Response(200, body, {"ETag": "abc"}))

    payload = delmar_mooring.fetch_archive(DATASET, 2019, session=session)

    assert payload.body == body
    assert payload.etag == "abc"
    assert payload.source == "delmar_mooring"


def test_the_fetch_names_this_project_in_its_user_agent():
    session = _Session(_Response(200, FULL_STRING.read_bytes()))

    delmar_mooring.fetch_archive(DATASET, 2019, session=session)

    (_, kwargs) = session.calls[0]
    assert kwargs["headers"]["User-Agent"].startswith("kelpcompare/")


def test_validators_are_accepted_and_ignored():
    """ERDDAP answers a conditional request with the whole body, so sending one
    would cost a round trip to learn nothing. Taking the argument keeps the CLI
    from having to know which sources support it."""
    session = _Session(_Response(200, FULL_STRING.read_bytes()))

    delmar_mooring.fetch_archive(
        DATASET, 2019, session=session, validators={"ETag": "abc", "Last-Modified": "then"}
    )

    (_, kwargs) = session.calls[0]
    assert "If-None-Match" not in kwargs["headers"]
    assert "If-Modified-Since" not in kwargs["headers"]
