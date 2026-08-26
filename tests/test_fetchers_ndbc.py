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
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


class _Session:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.urls = response, error, []

    def get(self, url, timeout=None):
        self.urls.append(url)
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
        def get(self, url, timeout=None):
            self.urls.append(url)
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
