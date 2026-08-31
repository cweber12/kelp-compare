"""The NDBC parser, against the recorded LJAC1 payloads. Never the network."""

import gzip
from pathlib import Path

import pandas as pd
import pytest

from kelpcompare.fetchers import ndbc
from kelpcompare.fetchers.base import SourceUnavailable, new_payload
from kelpcompare.parameters import load_parameters
from kelpcompare.storage import (
    FLAG_MISSING,
    FLAG_NOT_EVALUATED,
    OBSERVATION_COLUMNS,
    validate_frame,
)

FIX = Path(__file__).parent / "fixtures" / "ndbc"
ARCHIVE = FIX / "ljac1h2023_excerpt.txt"
REALTIME = FIX / "LJAC1_realtime_excerpt.txt"

SITE = "NDBC:LJAC1"
RUN = "20260825T120000000Z-ingest"
DEPTHS = {"sea_water_temperature": 3.4}  # station page: sea temp depth 3.4 m below MLLW


@pytest.fixture
def parameters():
    return load_parameters(Path("data/registry/parameters.json"))


def _payload(path: Path, *, gzipped: bool = False):
    body = path.read_bytes()
    return new_payload(
        "ndbc",
        "LJAC1",
        path.name,
        f"https://www.ndbc.noaa.gov/data/{path.name}",
        gzip.compress(body) if gzipped else body,
    )


def _parse(path: Path, parameters, **kwargs):
    return ndbc.parse(
        _payload(path, gzipped=kwargs.pop("gzipped", False)),
        parameters,
        site_id=SITE,
        depths_m=DEPTHS,
        run_id=RUN,
        **kwargs,
    )


# --------------------------------------------------------------------------
# The schema contract
# --------------------------------------------------------------------------


def test_archive_parses_into_the_docs03_schema(parameters):
    parsed = _parse(ARCHIVE, parameters)

    validate_frame(parsed.frame)  # raises unless it is exactly the docs/03 schema
    assert tuple(parsed.frame.columns) == OBSERVATION_COLUMNS
    assert parsed.layout == "archive"
    assert parsed.rows_in == 400
    assert set(parsed.frame["source"]) == {"ndbc"}
    assert set(parsed.frame["site_id"]) == {SITE}
    assert set(parsed.frame["fetch_run_id"]) == {RUN}


def test_every_mapped_parameter_is_stored(parameters):
    parsed = _parse(ARCHIVE, parameters)

    assert set(parsed.frame["parameter"]) == {
        "sea_water_temperature",
        "air_temperature",
        "wave_significant_height",
        "wave_peak_period",
        "wind_speed",
    }
    # One row per column per input row: nothing is dropped, including missing.
    assert len(parsed.frame) == 400 * 5


def test_timestamps_are_utc_aware_and_ascending(parameters):
    parsed = _parse(ARCHIVE, parameters)
    stamps = parsed.frame["timestamp"]

    assert str(stamps.dtype.tz) == "UTC"
    assert stamps.is_monotonic_increasing
    assert str(stamps.iloc[0]) == "2023-06-20 17:06:00+00:00"
    assert str(stamps.iloc[-1]) == "2023-06-22 11:24:00+00:00"


def test_realtime_comes_back_ascending_though_the_file_is_newest_first(parameters):
    """The one reordering this parser does, and the reason it is safe to.

    Every downstream stage sorts by time anyway; returning the file's own order
    would leave a frame whose first row is its last measurement, which is a trap
    for anything that peeks at `.iloc[0]`.
    """
    parsed = _parse(REALTIME, parameters)

    assert parsed.layout == "realtime"
    assert parsed.frame["timestamp"].is_monotonic_increasing
    assert str(parsed.frame["timestamp"].iloc[0]) == "2026-08-21 23:54:00+00:00"
    assert str(parsed.frame["timestamp"].iloc[-1]) == "2026-08-23 05:48:00+00:00"


# --------------------------------------------------------------------------
# Sentinels: the whole point of parsing this format carefully
# --------------------------------------------------------------------------


def _water(parsed):
    return parsed.frame.loc[parsed.frame["parameter"] == "sea_water_temperature"]


def test_numeric_sentinels_become_null_not_measurements(parameters):
    """999.0 °C is the archive's way of saying nothing, not a reading."""
    water = _water(_parse(ARCHIVE, parameters))

    assert water["value"].isna().sum() == 5
    assert not (water["value"] == 999.0).any()
    assert water["value"].max() == 18.7  # the real maximum, not the sentinel


def test_mm_sentinels_become_null_too(parameters):
    water = _water(_parse(REALTIME, parameters))

    assert water["value"].isna().sum() == 3
    assert (water["value"].min(), water["value"].max()) == (16.0, 21.8)


def test_a_missing_value_is_flagged_9_and_a_present_one_is_flagged_2(parameters):
    """docs/03: ingest writes 2 for a row it has no verdict on, 9 for an absent value."""
    water = _water(_parse(ARCHIVE, parameters))

    assert set(water.loc[water["value"].isna(), "qc_flag"]) == {FLAG_MISSING}
    assert set(water.loc[water["value"].notna(), "qc_flag"]) == {FLAG_NOT_EVALUATED}
    assert water["qc_flag"].dtype == "int8"


def test_a_public_station_records_no_ingest_time_verdict(parameters):
    """There is no deployment window to judge, so `qc_tests` stays empty."""
    parsed = _parse(ARCHIVE, parameters)

    assert set(parsed.frame["qc_tests"]) == {""}


def test_sentinels_are_matched_numerically_not_as_text(parameters):
    """A change in printed precision must not smuggle a sentinel through."""
    text = ARCHIVE.read_text(encoding="latin-1").splitlines()
    # Rewrite one real water temperature as the sentinel in a different spelling.
    row = text[3].split()
    row[14] = "999.00"
    text[3] = " ".join(row)
    payload = new_payload("ndbc", "LJAC1", "edited.txt", "file://edited", "\n".join(text).encode())

    water = _water(ndbc.parse(payload, parameters, site_id=SITE, depths_m=DEPTHS, run_id=RUN))

    assert water["value"].isna().sum() == 6  # the five real ones plus this


def test_an_unreadable_token_is_read_as_missing_and_reported(parameters):
    """Silently is the one way it must not happen (docs/02).

    `N/A` is chosen deliberately: it is one of the tokens pandas converts to NaN
    on its own unless `keep_default_na=False`. Without that, the token would
    reach the frame as a null nobody decided on and this warning would name
    `nan` instead of what the file actually said.
    """
    text = ARCHIVE.read_text(encoding="latin-1").splitlines()
    row = text[3].split()
    row[14] = "N/A"
    text[3] = " ".join(row)
    payload = new_payload("ndbc", "LJAC1", "edited.txt", "file://edited", "\n".join(text).encode())

    parsed = ndbc.parse(payload, parameters, site_id=SITE, depths_m=DEPTHS, run_id=RUN)

    assert _water(parsed)["value"].isna().sum() == 6
    assert any("N/A" in w and "WTMP" in w for w in parsed.warnings)


# --------------------------------------------------------------------------
# Units and depth
# --------------------------------------------------------------------------


def test_units_are_converted_to_the_registry_canonical_form(parameters):
    """NDBC says `sec` and `m/s`; the registry says `s` and `m s-1`."""
    parsed = _parse(ARCHIVE, parameters)
    period = parsed.frame.loc[parsed.frame["parameter"] == "wave_peak_period", "value"]
    wind = parsed.frame.loc[parsed.frame["parameter"] == "wind_speed", "value"]

    assert period.isna().all()  # LJAC1 is a shore station: no wave sensor
    assert wind.notna().any()
    assert wind.max() < 60.0  # inside the registry valid_range for m s-1


def test_a_unit_the_file_does_not_declare_as_expected_stops_the_parse(parameters):
    """A wind speed in knots stored as m/s survives into a publication."""
    text = ARCHIVE.read_text(encoding="latin-1").splitlines()
    text[1] = text[1].replace(" m/s ", " kts ", 1)
    payload = new_payload("ndbc", "LJAC1", "edited.txt", "file://edited", "\n".join(text).encode())

    with pytest.raises(ValueError, match="WSPD is declared in 'kts'"):
        ndbc.parse(payload, parameters, site_id=SITE, depths_m=DEPTHS, run_id=RUN)


def test_depth_comes_from_the_registry_and_only_for_the_parameter_that_has_one(parameters):
    """docs/02: comparing a 3.4 m intake to a 10 m logger is the error to prevent."""
    parsed = _parse(ARCHIVE, parameters)
    frame = parsed.frame

    assert set(frame.loc[frame["parameter"] == "sea_water_temperature", "depth_m"]) == {3.4}
    for met in ("air_temperature", "wind_speed"):
        assert frame.loc[frame["parameter"] == met, "depth_m"].isna().all()


# --------------------------------------------------------------------------
# Layouts we have not verified
# --------------------------------------------------------------------------


def test_a_gzipped_archive_is_read_transparently(parameters):
    plain = _parse(ARCHIVE, parameters)
    zipped = _parse(ARCHIVE, parameters, gzipped=True)

    pd.testing.assert_frame_equal(plain.frame, zipped.frame)


def test_a_file_without_two_header_lines_is_refused(parameters):
    payload = new_payload("ndbc", "LJAC1", "x.txt", "file://x", b"2023 06 20 17 06 261\n")

    with pytest.raises(ValueError, match="two '#' header lines"):
        ndbc.parse(payload, parameters, site_id=SITE, depths_m=DEPTHS, run_id=RUN)


def test_a_pre2005_time_layout_is_refused_rather_than_misread(parameters):
    """Older archives have no minute column; the columns would silently shift."""
    body = b"#YYYY MM DD hh WDIR WSPD\n#yr mo dy hr degT m/s\n2004 06 20 17 261 1.9\n"
    payload = new_payload("ndbc", "LJAC1", "old.txt", "file://old", body)

    with pytest.raises(ValueError, match="time columns"):
        ndbc.parse(payload, parameters, site_id=SITE, depths_m=DEPTHS, run_id=RUN)


def test_columns_the_project_does_not_store_are_reported(parameters):
    """PRES and TIDE are deliberately unmapped; the manifest should say so."""
    parsed = _parse(ARCHIVE, parameters)

    assert "PRES" in parsed.unmapped_columns
    assert "TIDE" in parsed.unmapped_columns
    assert "WTMP" not in parsed.unmapped_columns


# --------------------------------------------------------------------------
# Fetching, without a network
# --------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class _Session:
    """A stand-in for `requests.Session`, recording what was asked and how.

    `sent` keeps the request headers, which is the only way to check a
    conditional request offline -- the whole mechanism is invisible in the
    response when it works.
    """

    def __init__(self, response=None, error=None):
        self.response, self.error, self.urls = response, error, []
        self.sent: list[dict] = []

    def get(self, url, timeout=None, headers=None):
        self.urls.append(url)
        self.sent.append(dict(headers or {}))
        if self.error:
            raise self.error
        return self.response


def test_realtime_url_and_landing_label():
    session = _Session(_Response(200, b"body"))

    payload = ndbc.fetch_realtime("ljac1", session=session)

    assert session.urls == ["https://www.ndbc.noaa.gov/data/realtime2/LJAC1.txt"]
    assert (payload.station, payload.label, payload.body) == ("LJAC1", "LJAC1.txt", b"body")


def test_archive_url_is_lowercase_station_and_year():
    session = _Session(_Response(200, b"body"))

    payload = ndbc.fetch_archive("LJAC1", 2023, session=session)

    assert session.urls == ["https://www.ndbc.noaa.gov/data/historical/stdmet/ljac1h2023.txt.gz"]
    assert payload.label == "ljac1h2023.txt.gz"


def test_a_transient_failure_is_retried_once(monkeypatch):
    """docs/02 "retry politely": one second chance, then record the gap."""
    slept = []
    monkeypatch.setattr(ndbc.time, "sleep", slept.append)

    class _Flaky(_Session):
        def get(self, url, timeout=None, headers=None):
            self.urls.append(url)
            self.sent.append(dict(headers or {}))
            return _Response(200, b"ok") if len(self.urls) > 1 else _Response(503)

    session = _Flaky()
    payload = ndbc.fetch_realtime("LJAC1", session=session)

    assert payload.body == b"ok"
    assert len(session.urls) == 2
    assert slept == [ndbc.RETRY_DELAY_SECONDS]


def test_a_404_is_not_retried(monkeypatch):
    """docs/01 §5: a missing NDBC year is a gap to record, never a crash.

    And it is asked for exactly once -- a year the station never published is an
    answer, so a second request could not change it.
    """
    monkeypatch.setattr(ndbc.time, "sleep", lambda _: None)
    session = _Session(_Response(404))

    with pytest.raises(SourceUnavailable, match="HTTP 404"):
        ndbc.fetch_archive("LJAC1", 1970, session=session)

    assert len(session.urls) == 1


def test_a_persistent_outage_gives_up_after_the_retry(monkeypatch):
    monkeypatch.setattr(ndbc.time, "sleep", lambda _: None)
    session = _Session(error=TimeoutError("read timed out"))

    with pytest.raises(SourceUnavailable, match="TimeoutError"):
        ndbc.fetch_realtime("LJAC1", session=session)

    assert len(session.urls) == 2


# --------------------------------------------------------------------------
# What the station actually measures -- issue #21
# --------------------------------------------------------------------------

LJAC1_MEASURES = ("sea_water_temperature", "air_temperature", "wind_speed")


def test_a_declared_station_stores_only_the_parameters_it_has_sensors_for(parameters):
    """The stdmet format has fixed columns, so WVHT and DPD are in every file
    holding the sentinel. The registry is what says they mean nothing here."""
    parsed = _parse(ARCHIVE, parameters, measured_parameters=LJAC1_MEASURES)

    assert set(parsed.frame["parameter"]) == set(LJAC1_MEASURES)
    assert parsed.undeclared_parameters == ("wave_significant_height", "wave_peak_period")
    assert parsed.warnings == ()


def test_a_declared_sensor_reporting_the_sentinel_still_gets_its_rows(parameters):
    """The distinction a declaration buys: an outage is recorded, an absent
    instrument is not. A per-payload "this column looked empty" rule could not
    tell the two apart."""
    parsed = _parse(ARCHIVE, parameters, measured_parameters=LJAC1_MEASURES)
    air = parsed.frame.loc[parsed.frame["parameter"] == "air_temperature"]

    assert len(air) == parsed.rows_in
    assert air["value"].isna().any()
    assert parsed.missing_counts["air_temperature"] > 0


def test_an_undeclared_station_stores_everything_and_says_the_registry_is_silent(parameters):
    """An unrecorded fact must not quietly become missing data."""
    parsed = _parse(ARCHIVE, parameters)

    assert len(set(parsed.frame["parameter"])) == 5
    assert parsed.undeclared_parameters == ()
    assert any("declares no measured_parameters" in w for w in parsed.warnings)


def test_a_declaration_the_parameter_vocabulary_does_not_know_is_reported(parameters):
    """A typo here subtracts a real series rather than adding a fictional one,
    so it cannot be allowed to pass silently."""
    parsed = _parse(ARCHIVE, parameters, measured_parameters=("sea_water_temp",))

    assert parsed.frame.empty
    assert any("sea_water_temp" in w and "not in" in w for w in parsed.warnings)


def test_a_parse_that_yields_no_rows_still_returns_the_storage_schema(parameters):
    """An empty result is a docs/03 frame, not a bag of `object` columns.

    `write_observations` documents "no rows this run" as normal and then refused
    it: the empty branch built columns pandas typed `object`, and hard rule 2 is
    enforced on the dtype, so the refusal was a dtype accident rather than a
    decision (#51). What stops a zero-row window now is the ingest CLI, which
    can tell an empty payload from an empty declaration.
    """
    parsed = _parse(ARCHIVE, parameters, measured_parameters=("water_level",))

    assert parsed.frame.empty
    validate_frame(parsed.frame)  # would raise on the object-dtype timestamp


def test_declaring_one_parameter_leaves_the_others_out_without_extra_warnings(parameters):
    """A station with no such sensor must not also collect a warning about the
    column being absent from a file it was never going to be read from."""
    parsed = _parse(ARCHIVE, parameters, measured_parameters=("sea_water_temperature",))

    assert set(parsed.frame["parameter"]) == {"sea_water_temperature"}
    assert parsed.warnings == ()
    assert len(parsed.undeclared_parameters) == 4


# --------------------------------------------------------------------------
# Asking instead of downloading
# --------------------------------------------------------------------------
#
# Both NDBC endpoints serve ETag and Last-Modified and honour conditional
# requests, so a re-run of an unchanged window can cost one round trip and no
# payload. The whole mechanism is invisible in a successful response, which is
# why these assert on what was *sent*.


HEADERS = {"ETag": '"e3dc8-52be5aaf150c0"', "Last-Modified": "Tue, 16 Feb 2016 16:31:39 GMT"}


def test_the_validators_the_server_sent_are_carried_on_the_payload():
    """Carried rather than recorded here: the fetcher does not know whether the
    window went on to ingest, and a validator stored before that could skip a
    window whose rows never landed."""
    session = _Session(_Response(200, b"body", HEADERS))
    payload = ndbc.fetch_archive("LJAC1", 2023, session=session)

    assert payload.etag == '"e3dc8-52be5aaf150c0"'
    assert payload.last_modified == "Tue, 16 Feb 2016 16:31:39 GMT"


def test_response_headers_are_read_case_insensitively():
    """HTTP header names are case-insensitive and servers differ."""
    session = _Session(_Response(200, b"body", {"etag": '"lower"', "LAST-MODIFIED": "Tue"}))
    payload = ndbc.fetch_realtime("LJAC1", session=session)

    assert (payload.etag, payload.last_modified) == ('"lower"', "Tue")


def test_a_server_that_offers_no_validator_leaves_them_null():
    session = _Session(_Response(200, b"body"))
    payload = ndbc.fetch_realtime("LJAC1", session=session)

    assert (payload.etag, payload.last_modified) is not None
    assert payload.etag is None and payload.last_modified is None


def test_known_validators_are_sent_as_a_conditional_request():
    session = _Session(_Response(200, b"body", HEADERS))
    ndbc.fetch_archive(
        "LJAC1",
        2023,
        session=session,
        validators={"etag": '"held"', "last_modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
    )

    sent = session.sent[0]
    assert sent["If-None-Match"] == '"held"'
    assert sent["If-Modified-Since"] == "Mon, 01 Jan 2024 00:00:00 GMT"


def test_only_the_validator_we_hold_is_sent():
    """A server may have offered one and not the other."""
    session = _Session(_Response(200, b"body"))
    ndbc.fetch_archive("LJAC1", 2023, session=session, validators={"etag": '"held"'})

    assert session.sent[0]["If-None-Match"] == '"held"'
    assert "If-Modified-Since" not in session.sent[0]


def test_knowing_nothing_sends_no_condition():
    session = _Session(_Response(200, b"body"))
    ndbc.fetch_archive("LJAC1", 2023, session=session)

    assert "If-None-Match" not in session.sent[0]
    assert "If-Modified-Since" not in session.sent[0]


def test_a_304_is_not_modified_and_not_an_outage():
    """`SourceUnavailable` means a hole in the record and is noted as a gap;
    this means the opposite, and conflating them would put a phantom gap in the
    manifest of every re-run."""
    session = _Session(_Response(304))

    with pytest.raises(ndbc.NotModified):
        ndbc.fetch_archive("LJAC1", 2023, session=session, validators={"etag": '"held"'})

    assert not issubclass(ndbc.NotModified, SourceUnavailable)


def test_a_304_is_not_retried():
    """It is an answer, and the fastest possible one. Asking again would be the
    impoliteness this whole mechanism exists to avoid."""
    session = _Session(_Response(304))

    with pytest.raises(ndbc.NotModified):
        ndbc.fetch_realtime("LJAC1", session=session, validators={"etag": '"held"'})

    assert len(session.urls) == 1


def test_a_stale_validator_still_gets_the_whole_file():
    """The property that makes this safe: it is a cache check, never a promise
    not to look. NDBC does occasionally re-issue an archive year after QC."""
    session = _Session(_Response(200, b"the revised file", HEADERS))
    payload = ndbc.fetch_archive("LJAC1", 2023, session=session, validators={"etag": '"stale"'})

    assert payload.body == b"the revised file"
    assert payload.etag == HEADERS["ETag"]


# --------------------------------------------------------------------------
# Saying who is asking
# --------------------------------------------------------------------------


def test_every_request_identifies_the_project():
    session = _Session(_Response(200, b"body"))
    ndbc.fetch_realtime("LJAC1", session=session)

    assert session.sent[0]["User-Agent"].startswith("kelpcompare/")


def test_a_contact_from_the_environment_reaches_the_header(monkeypatch):
    """From the environment rather than from the source file, because the
    repository is public and an address committed to it is published."""
    monkeypatch.setenv(ndbc.CONTACT_ENV, "someone@example.org")
    assert ndbc.user_agent() == f"kelpcompare/{ndbc.__version__} (+someone@example.org)"


def test_no_contact_still_identifies_the_project(monkeypatch):
    """An anonymous but identifiable client beats `python-requests`, and beats a
    run that will not start."""
    monkeypatch.delenv(ndbc.CONTACT_ENV, raising=False)
    assert ndbc.user_agent() == f"kelpcompare/{ndbc.__version__}"


def test_a_blank_contact_is_treated_as_none(monkeypatch):
    monkeypatch.setenv(ndbc.CONTACT_ENV, "   ")
    assert "(+" not in ndbc.user_agent()


# --------------------------------------------------------------------------
# The URLs a caller needs before it can ask about them
# --------------------------------------------------------------------------


def test_the_url_helpers_agree_with_what_the_fetchers_request():
    """The caller looks a validator up by URL, so it has to be able to build the
    same string the fetch will use."""
    session = _Session(_Response(200, b"body"))

    ndbc.fetch_realtime("ljac1", session=session)
    ndbc.fetch_archive("LJAC1", 2023, session=session)

    assert session.urls == [ndbc.realtime_url("LJAC1"), ndbc.archive_url("ljac1", 2023)]


# --------------------------------------------------------------------------
# The two nearshore Waveriders parse with no change to this module
# --------------------------------------------------------------------------

WAVERIDER_DEPTHS = {"sea_water_temperature": 0.46}  # station pages: 0.46 m below water line
WAVERIDER_DECLARES = ("sea_water_temperature", "wave_significant_height", "wave_peak_period")

WAVERIDER_FILES = [
    ("NDBC:46254", FIX / "46254h2015_excerpt.txt", True),
    ("NDBC:46254", FIX / "46254_realtime_excerpt.txt", False),
    ("NDBC:46266", FIX / "46266h2019_excerpt.txt", True),
    ("NDBC:46266", FIX / "46266_realtime_excerpt.txt", False),
]


def _parse_waverider(site_id, path, parameters, *, gzipped):
    body = path.read_bytes()
    payload = new_payload(
        "ndbc",
        site_id.split(":")[1],
        path.name,
        f"https://www.ndbc.noaa.gov/data/{path.name}",
        gzip.compress(body) if gzipped else body,
    )
    return ndbc.parse(
        payload,
        parameters,
        site_id=site_id,
        depths_m=WAVERIDER_DEPTHS,
        run_id=RUN,
        measured_parameters=WAVERIDER_DECLARES,
    )


@pytest.mark.parametrize(
    ("site_id", "path", "gzipped"), WAVERIDER_FILES, ids=lambda v: getattr(v, "name", v)
)
def test_a_waverider_payload_parses_with_no_change_to_this_module(
    site_id, path, gzipped, parameters
):
    """The claim that landing these two stations needed no fetcher work.

    Both layouts, both stations, through the same `parse` LJAC1 goes through.
    """
    parsed = _parse_waverider(site_id, path, parameters, gzipped=gzipped)
    frame = parsed.frame

    assert not frame.empty
    assert set(frame["site_id"]) == {site_id}
    assert set(frame["parameter"]) == set(WAVERIDER_DECLARES)


@pytest.mark.parametrize(
    ("site_id", "path", "gzipped"), WAVERIDER_FILES, ids=lambda v: getattr(v, "name", v)
)
def test_a_waverider_water_temperature_survives_the_parse_intact(
    site_id, path, gzipped, parameters
):
    """No sentinel reaches the record as a measurement, and the surface depth
    the registry declares is what lands on the row."""
    frame = _parse_waverider(site_id, path, parameters, gzipped=gzipped).frame
    water = frame.loc[frame["parameter"] == "sea_water_temperature"]

    assert water["value"].notna().all()
    assert water["value"].between(5.0, 35.0).all()
    assert set(water["depth_m"]) == {0.46}


@pytest.mark.parametrize(
    ("site_id", "path", "gzipped"), WAVERIDER_FILES, ids=lambda v: getattr(v, "name", v)
)
def test_a_waverider_lands_no_rows_for_the_sensors_it_does_not_have(
    site_id, path, gzipped, parameters
):
    """`measured_parameters` is the gate: the met columns are present in the
    layout and sentinel in every row, and must produce no observations at all
    rather than a column of nulls."""
    frame = _parse_waverider(site_id, path, parameters, gzipped=gzipped).frame

    assert "air_temperature" not in set(frame["parameter"])
    assert "wind_speed" not in set(frame["parameter"])
