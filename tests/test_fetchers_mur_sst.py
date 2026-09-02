"""The JPL MUR L4 SST fetcher (docs/02 "JPL MUR L4 SST").

Three fixtures, and the split between them is the point. The two recorded ones
are real NOAA CoastWatch payloads and pin the *format* -- the two-line header,
the UDUNITS `degree_C` token, the axis order, and `NaN` where the land mask is.
`KELP:DEL-MAR` is recorded because it is the bed the obvious reduction fails on:
no MUR cell centre falls inside it at all. `KELP:LA-JOLLA` is recorded because
its request box contains shoreline, which is what a bbox mean would average in.

The hand-built one pins the edge cases three summer days do not happen to
contain: the declared `_FillValue` arriving as a number, and a day the product
covers nowhere over the bed. It uses one value per water cell on purpose, so its
expected mean is that value whatever the weights are -- an edited fixture must
not be the thing that proves the weighting.

That mirrors the RTOMS and HOBO pairs and exists for the same reason: a fixture
edited to contain an edge case can no longer prove what the source actually
sends, so it must not be the only one.

Network access is forbidden here (CLAUDE.md), so `fetch` is exercised through a
stub session and everything else runs off the recorded bytes.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

from kelpcompare.cli import SOURCE_NAMES
from kelpcompare.fetchers import mur_sst
from kelpcompare.fetchers.base import SourceUnavailable, new_payload
from kelpcompare.parameters import load_parameters
from kelpcompare.polygons import load_polygons
from kelpcompare.storage import FLAG_MISSING, FLAG_NOT_EVALUATED, OBSERVATION_COLUMNS

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "mur_sst"
DEL_MAR = FIXTURES / "del_mar_2020-07-01_03_excerpt.csv"
LA_JOLLA = FIXTURES / "la_jolla_2020-07-01_03_excerpt.csv"
EDGE_CASES = FIXTURES / "del_mar_edge-cases.csv"

RUN = "RUN-1"
MEASURED = ("sea_water_temperature",)


@pytest.fixture
def parameters():
    return load_parameters(
        REPO_ROOT / "data" / "registry" / "parameters.json", sources=SOURCE_NAMES
    )


@pytest.fixture(scope="module")
def beds():
    return load_polygons(REPO_ROOT / "data" / "registry" / "polygons.geojson").frame.set_index(
        "polygon_id"
    )


def geometry_of(beds, polygon_id: str):
    return beds.loc[polygon_id].geometry


def payload(path: Path = DEL_MAR, *, body: bytes | None = None, station: str = "KELP_DEL-MAR"):
    return new_payload(
        mur_sst.SOURCE,
        station,
        f"{station}_2020.csv",
        "https://example.invalid/griddap",
        body if body is not None else path.read_bytes(),
    )


def parse(beds, *, polygon_id="KELP:DEL-MAR", site_id="SST:DEL-MAR", depths_m=None, **kwargs):
    return mur_sst.parse(
        kwargs.pop("payload", None) or payload(),
        kwargs.pop("parameters"),
        site_id=site_id,
        geometry=kwargs.pop("geometry", geometry_of(beds, polygon_id)),
        depths_m={} if depths_m is None else depths_m,
        measured_parameters=kwargs.pop("measured_parameters", MEASURED),
        run_id=RUN,
        **kwargs,
    )


class StubSession:
    """A `requests.Session` as far as this fetcher is concerned."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, timeout=None, headers=None):
        self.requests.append((url, dict(headers or {})))
        answer = self.responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class StubResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


# --------------------------------------------------------------------------
# Where it asks
# --------------------------------------------------------------------------


def test_the_request_box_is_padded_by_a_whole_cell(beds):
    """A cell whose centre sits outside the bounds can still overlap them, so
    asking for exactly the bounds would drop cells the reduction weights."""
    geometry = geometry_of(beds, "KELP:DEL-MAR")
    minx, miny, maxx, maxy = geometry.bounds
    bounds = mur_sst.request_bounds(geometry)

    assert bounds == pytest.approx((minx - 0.01, miny - 0.01, maxx + 0.01, maxy + 0.01), abs=1e-12)


def test_the_padding_holds_every_cell_that_overlaps_each_committed_bed(beds):
    """The property the padding exists for, enumerated against every committed
    bed rather than argued from the arithmetic.

    A cell dropped from the request is a cell silently missing from the mean, so
    the same outline would reduce differently depending on where its edges fell
    against the grid -- and differently for each bed.
    """
    step = mur_sst.CELL_DEGREES
    half = step / 2

    for polygon_id in beds.index:
        geometry = geometry_of(beds, polygon_id)
        minx, miny, maxx, maxy = geometry.bounds
        # Every MUR cell centre in a box generously wider than the bed itself.
        lats = np.arange(round((miny - 0.05) / step) * step, maxy + 0.05, step)
        lons = np.arange(round((minx - 0.05) / step) * step, maxx + 0.05, step)
        overlapping = [
            (lat, lon)
            for lat in lats
            for lon in lons
            if box(lon - half, lat - half, lon + half, lat + half).intersects(geometry)
        ]
        assert overlapping, polygon_id

        rminx, rminy, rmaxx, rmaxy = mur_sst.request_bounds(geometry)
        outside = [
            cell
            for lat, lon in overlapping
            if not (rminy <= lat <= rmaxy and rminx <= lon <= rmaxx)
            for cell in [(lat, lon)]
        ]
        assert outside == [], f"{polygon_id} would drop {outside}"


def test_an_archive_year_is_two_exact_stamps():
    """Exact, because griddap resolves a time value to the *nearest* grid point.
    A span ending 23:59:59Z is nine hours from the next day's 09:00:00Z against
    fifteen from that day's own -- a real 2020 ingest returned 367 days, ending
    2021-01-01, so two consecutive years each claimed it."""
    url = mur_sst.archive_url((-117.3, 32.8, -117.2, 32.9), 2020, today=date(2026, 8, 31))

    assert "jplMURSST41.csv?analysed_sst" in url
    assert "(2020-01-01T09:00:00Z):1:(2020-12-31T09:00:00Z)" in url
    # Latitude before longitude: the axis order is positional in the URL.
    assert url.index("(32.8000)") < url.index("(-117.3000)")


def test_two_consecutive_years_share_no_day():
    """The property the exact stamps exist for. Off-by-one here does not
    corrupt anything -- the rows dedupe on OBSERVATION_KEY -- it makes every
    year's manifest count wrong by one and explicable by nothing."""
    first = mur_sst.archive_url((-117.3, 32.8, -117.2, 32.9), 2020, today=date(2026, 8, 31))
    second = mur_sst.archive_url((-117.3, 32.8, -117.2, 32.9), 2021, today=date(2026, 8, 31))

    assert "(2020-12-31T09:00:00Z)" in first
    assert "(2021-01-01T09:00:00Z)" in second
    assert "2021" not in first.split("%5D")[0]


def test_the_first_year_starts_where_the_record_does():
    """MUR begins 2002-06-01. A start before that is answered 404, which `_get`
    would report as an outage -- losing the seven months that exist."""
    url = mur_sst.archive_url((-117.3, 32.8, -117.2, 32.9), 2002, today=date(2026, 8, 31))

    assert f"({mur_sst.RECORD_START}):1:(2002-12-31T09:00:00Z)" in url


def test_a_year_still_running_stops_at_the_last_stamp_rather_than_31_december():
    """That stamp does not exist yet and asking for it is answered 404, so the
    exact form would lose the whole of the current year rather than its tail."""
    url = mur_sst.archive_url((-117.3, 32.8, -117.2, 32.9), 2026, today=date(2026, 8, 31))

    assert "(2026-01-01T09:00:00Z):1:(last)" in url


def test_a_year_that_has_not_started_is_refused_rather_than_fetched():
    with pytest.raises(ValueError, match="has not started"):
        mur_sst.archive_url((-117.3, 32.8, -117.2, 32.9), 2027, today=date(2026, 8, 31))


def test_the_rolling_window_counts_back_from_the_end_of_the_record():
    """`now` is rejected on a griddap time axis (measured 2026-08-31), and an
    index offset cannot ask for a day the analysis has not published."""
    url = mur_sst.realtime_url((-117.3, 32.8, -117.2, 32.9))

    assert f"last-{mur_sst.REALTIME_DAYS - 1}:1:last" in url
    assert "now" not in url


def test_a_year_before_the_record_starts_is_refused_rather_than_fetched():
    """ERDDAP answers an out-of-range time constraint with a 404, which `_get`
    would report as an outage -- putting a phantom gap in the manifest for a
    year that was never going to exist."""
    with pytest.raises(ValueError, match="cannot serve 2001"):
        mur_sst.fetch_archive((-117.3, 32.8, -117.2, 32.9), 2001, station="KELP_DEL-MAR")


def test_a_fetch_lands_the_bytes_it_was_sent_and_says_which_bed():
    session = StubSession(StubResponse(200, b"time,latitude,longitude,analysed_sst\n"))
    got = mur_sst.fetch_archive(
        (-117.3, 32.8, -117.2, 32.9), 2020, station="KELP_DEL-MAR", session=session
    )

    assert got.source == mur_sst.SOURCE
    assert got.station == "KELP_DEL-MAR"
    assert got.label == "KELP_DEL-MAR_2020.csv"
    assert got.body == b"time,latitude,longitude,analysed_sst\n"
    assert session.requests[0][1]["User-Agent"] == mur_sst.USER_AGENT


def test_no_validator_is_carried_off_a_response():
    """Measured 2026-08-31: this host serves no ETag, and its `Last-Modified` is
    the response's own generation time -- two requests a minute apart returned
    two different values. Carrying it would put "when we asked" into the
    validator cache wearing the costume of "what version this is".
    """
    session = StubSession(
        StubResponse(200, b"x", {"ETag": '"abc"', "Last-Modified": "Mon, 31 Aug 2026 18:54:14 GMT"})
    )
    got = mur_sst.fetch_realtime((-117.3, 32.8, -117.2, 32.9), station="B", session=session)

    assert (got.etag, got.last_modified) == (None, None)


def test_a_retryable_status_is_asked_once_more_then_reported_as_an_outage(monkeypatch):
    monkeypatch.setattr(mur_sst.time, "sleep", lambda _seconds: None)
    session = StubSession(StubResponse(503), StubResponse(503))

    with pytest.raises(SourceUnavailable, match="HTTP 503"):
        mur_sst.fetch_realtime((-117.3, 32.8, -117.2, 32.9), station="B", session=session)
    assert len(session.requests) == 2


def test_a_404_is_an_answer_and_is_not_retried():
    """ERDDAP answers a query that matched nothing with a 404 and a text body
    saying so, which is a statement about the data rather than an outage."""
    session = StubSession(StubResponse(404, b"Error: nothing matched"))

    with pytest.raises(SourceUnavailable, match="HTTP 404"):
        mur_sst.fetch_realtime((-117.3, 32.8, -117.2, 32.9), station="B", session=session)
    assert len(session.requests) == 1


# --------------------------------------------------------------------------
# One row per bed per day
# --------------------------------------------------------------------------


def test_a_recorded_payload_reduces_to_one_row_per_day(beds, parameters):
    parsed = parse(beds, parameters=parameters)

    assert parsed.rows_in == 60  # 20 cells x 3 days
    assert len(parsed.frame) == 3
    assert list(parsed.frame.columns) == list(OBSERVATION_COLUMNS)
    assert parsed.layout == mur_sst.DATASET_ID
    assert list(parsed.frame["site_id"].unique()) == ["SST:DEL-MAR"]
    assert list(parsed.frame["source"].unique()) == [mur_sst.SOURCE]
    assert list(parsed.frame["fetch_run_id"].unique()) == [RUN]


def test_the_del_mar_bed_has_a_series_although_no_cell_centre_falls_inside_it(beds, parameters):
    """The measurement the whole reduction is chosen for.

    `KELP:DEL-MAR` is narrower than the 1 km grid along its length, so the
    obvious rule -- average the cells whose centre is inside the outline -- has
    nothing to average and this bed would carry no satellite leg at all.
    """
    geometry = geometry_of(beds, "KELP:DEL-MAR")
    body = DEL_MAR.read_text(encoding="utf-8").splitlines()
    centres = {
        (float(parts[1]), float(parts[2])) for parts in (line.split(",") for line in body[2:])
    }
    inside = [c for c in centres if geometry.contains(Point(c[1], c[0]))]

    assert inside == []
    assert len(parse(beds, parameters=parameters).frame) == 3


def test_timestamps_are_the_products_daily_utc_stamp(beds, parameters):
    stamps = parse(beds, parameters=parameters).frame["timestamp"]

    assert list(stamps) == [
        pd.Timestamp("2020-07-01T09:00:00Z"),
        pd.Timestamp("2020-07-02T09:00:00Z"),
        pd.Timestamp("2020-07-03T09:00:00Z"),
    ]


def test_the_value_is_the_area_weighted_mean_over_the_beds_cells(beds, parameters):
    """Pinned against the numbers a hand-rolled reduction produced from the same
    payload on 2026-08-31, so a refactor that changes the weighting is visible."""
    values = parse(beds, parameters=parameters).frame["value"]

    assert list(values) == pytest.approx([20.0006, 20.7961, 20.8526], abs=5e-4)


def test_a_bed_whose_request_box_holds_shoreline_does_not_average_the_land_in(beds, parameters):
    """`KELP:LA-JOLLA`'s box contains land cells, which arrive as `NaN`. The
    weighted mean is over the cells inside the *outline* that carry a value, so
    the land mask reduces coverage rather than the temperature."""
    parsed = parse(
        beds,
        parameters=parameters,
        polygon_id="KELP:LA-JOLLA",
        site_id="SST:LA-JOLLA",
        payload=payload(LA_JOLLA, station="KELP_LA-JOLLA"),
    )

    assert parsed.rows_in == 189
    assert list(parsed.frame["value"]) == pytest.approx([19.9711, 20.3289, 20.2919], abs=5e-4)
    assert any("96.3%" in warning for warning in parsed.warnings)


def test_the_cell_count_reported_is_cells_not_cell_days(beds, parameters):
    """The count is a fact about the grid's reach into this bed, so it must not
    scale with how many days the payload happens to hold."""
    (report,) = [w for w in parse(beds, parameters=parameters).warnings if "cell(s)" in w]

    assert "over 5 cell(s)" in report


def test_ingested_rows_are_not_evaluated_and_carry_no_verdicts(beds, parameters):
    """docs/03: ingest writes flag 2 for a row it has no verdict on. The QARTOD
    tests run later, in `kelpcompare qc`, from `parameters.json` bounds."""
    frame = parse(beds, parameters=parameters).frame

    assert set(frame["qc_flag"]) == {FLAG_NOT_EVALUATED}
    assert set(frame["qc_tests"]) == {""}


# --------------------------------------------------------------------------
# Depth: declared nowhere, and that is the record
# --------------------------------------------------------------------------


def test_depth_is_null_because_the_provider_publishes_none(beds, parameters):
    """PO.DAAC publishes `sea_surface_foundation_temperature` and no depth, and
    docs/03 reserves a null `depth_m` for exactly that. A guessed 0.0 would
    claim a skin temperature this product explicitly is not."""
    frame = parse(beds, parameters=parameters).frame

    assert frame["depth_m"].isna().all()


def test_a_declared_depth_would_be_honoured_rather_than_overridden(beds, parameters):
    """Read from the registry rather than hardcoded, so the docs/04 s1 follow-on
    can declare one without editing this module -- though `depth_m` is part of
    `OBSERVATION_KEY`, so doing so is one-way."""
    frame = parse(beds, parameters=parameters, depths_m={"sea_water_temperature": 1.0}).frame

    assert list(frame["depth_m"]) == [1.0, 1.0, 1.0]


# --------------------------------------------------------------------------
# The edge cases three summer days do not contain
# --------------------------------------------------------------------------


def test_the_declared_fill_value_is_read_as_missing_rather_than_as_a_reading(beds, parameters):
    """A -7.768 that reached the weighted mean would drag a bed's daily value by
    the fill's whole distance from the water, and QC runs after the reduction."""
    parsed = parse(beds, parameters=parameters, payload=payload(EDGE_CASES))

    assert parsed.frame["value"].iloc[1] == pytest.approx(19.0)
    assert any("_FillValue" in warning for warning in parsed.warnings)


def test_a_day_the_product_covers_nowhere_stays_in_the_record_as_missing(beds, parameters):
    """docs/03 keeps outages rather than deleting them: dropping the row would
    turn an outage in a daily product into a gap the docs/04 s3 coverage
    arithmetic cannot see."""
    parsed = parse(beds, parameters=parameters, payload=payload(EDGE_CASES))

    assert len(parsed.frame) == 3
    assert parsed.frame["value"].iloc[2] != parsed.frame["value"].iloc[2]  # NaN
    assert parsed.frame["qc_flag"].iloc[2] == FLAG_MISSING
    assert parsed.missing_counts == {"sea_water_temperature": 1}
    assert any("no value in any cell" in warning for warning in parsed.warnings)


def test_coverage_is_reported_per_day_rather_than_once_for_the_bed(beds, parameters):
    """The land mask takes a standing bite out of most beds; a day the product
    does not cover takes more. One number for the payload would hide the second
    behind the first."""
    (report,) = [
        w
        for w in parse(beds, parameters=parameters, payload=payload(EDGE_CASES)).warnings
        if "covering" in w
    ]

    assert "covering 0.0% to 94.9%" in report


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


def test_a_unit_this_module_has_not_verified_stops_the_parse(beds, parameters):
    body = EDGE_CASES.read_bytes().replace(b"degree_C", b"degree_F")

    with pytest.raises(ValueError, match="degree_F"):
        parse(beds, parameters=parameters, payload=payload(body=body))


def test_reordered_axes_stop_the_parse_rather_than_swapping_the_hemisphere(beds, parameters):
    """The axis order is positional in the request URL, so a reordering here
    means latitude and longitude may have swapped -- which puts every bed in the
    wrong ocean and raises nothing downstream."""
    body = EDGE_CASES.read_bytes().replace(
        b"time,latitude,longitude,analysed_sst", b"time,longitude,latitude,analysed_sst", 1
    )

    with pytest.raises(ValueError, match="latitude and longitude may have swapped"):
        parse(beds, parameters=parameters, payload=payload(body=body))


def test_a_regridded_product_stops_the_parse(beds, parameters):
    """A regrid keeps every column name and every unit and changes only the
    step, and each cell would then be weighted by a footprint of the wrong
    size -- reweighting the whole record silently."""
    body = EDGE_CASES.read_bytes().replace(b"32.95,", b"32.99,").replace(b"32.96,", b"33.03,")

    with pytest.raises(ValueError, match="regridded"):
        parse(beds, parameters=parameters, payload=payload(body=body))


def test_a_payload_that_misses_the_bed_entirely_is_a_bug_not_a_gap(beds, parameters):
    """The request box and the geometry disagreeing is this module asking for
    the wrong rectangle, which must not be recorded as an upstream hole."""
    with pytest.raises(ValueError, match="no cell in this payload overlaps"):
        parse(beds, parameters=parameters, polygon_id="KELP:IMPERIAL-BEACH")


def test_a_bed_with_no_recorded_outline_is_refused(beds, parameters):
    """docs/03 makes geometry optional, and this is the stage that cannot
    proceed without one: there is nothing to reduce the grid over."""
    with pytest.raises(ValueError, match="no recorded outline"):
        parse(beds, parameters=parameters, geometry=None)


def test_a_truncated_payload_is_refused_by_shape(beds, parameters):
    with pytest.raises(ValueError, match="names line and a units line"):
        parse(beds, parameters=parameters, payload=payload(body=b"time,latitude\n"))


def test_a_station_that_does_not_declare_this_parameter_yields_nothing(beds, parameters):
    """The registry gate every fetcher applies: a site stores only what it
    declares an instrument -- here, a variable -- for."""
    parsed = parse(beds, parameters=parameters, measured_parameters=("water_level",))

    assert parsed.frame.empty
    assert parsed.rows_in == 0
    assert any("does not include" in warning for warning in parsed.warnings)


def test_a_parameter_missing_from_the_registry_stops_the_parse(beds, parameters, tmp_path):
    empty = tmp_path / "parameters.json"
    empty.write_text('{"parameters": {"water_level": {"unit": "m"}}}', encoding="utf-8")

    with pytest.raises(ValueError, match="sea_water_temperature"):
        parse(beds, parameters=load_parameters(empty))
