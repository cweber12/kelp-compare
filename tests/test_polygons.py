"""The analysis polygon registry (docs/03 "Kelp geometry and series").

Every case here is a hand-edit somebody will actually make: a purpose invented
on the spot, an id left off, a ring that crosses itself, a file exported from a
GIS tool in the projection it happened to be in. The loader's job is to refuse
each one *by name*, because none of them fails downstream. A polygon with no
geometry produces an all-null kelp series; one drawn in the wrong reference
system produces a perfectly ordinary series of the wrong water. Neither is
visible in the output table, which is why they have to be visible here.

Frames are written to `tmp_path` and loaded through the real loader rather than
constructed in memory, since what is under test is what the file is allowed to
say.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kelpcompare.polygons import (
    POLYGON_PURPOSES,
    WGS84,
    load_polygons,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED = REPO_ROOT / "data" / "registry" / "polygons.geojson"

#: A unit square off nowhere in particular. Coordinates are irrelevant to every
#: rule under test here, and inventing plausible-looking ones near a real kelp
#: bed would put a site coordinate in the repository that `sites.json` does not
#: carry (CLAUDE.md, "The repo is public").
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.1, 0.0], [0.1, 0.1], [0.0, 0.1], [0.0, 0.0]]],
}

#: A ring that crosses itself. Valid JSON, valid GeoJSON, not a valid polygon --
#: it encloses two lobes of opposite orientation and no reliable interior.
BOWTIE = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.1, 0.1], [0.1, 0.0], [0.0, 0.1], [0.0, 0.0]]],
}


#: Marks a property the feature should simply not declare, as against one it
#: declares as null -- two different hand-edits with two different messages.
_ABSENT = object()


def feature(geometry=SQUARE, **properties) -> dict:
    declared = {"polygon_id": "KELP:A", "purpose": "regional", "site_ids": ["NDBC:LJAC1"]}
    declared.update(properties)
    return {
        "type": "Feature",
        "properties": {k: v for k, v in declared.items() if v is not _ABSENT},
        "geometry": geometry,
    }


def write(tmp_path: Path, *features: dict, **collection) -> Path:
    payload = {"type": "FeatureCollection", "features": list(features), **collection}
    target = tmp_path / "polygons.geojson"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def load(tmp_path: Path, *features: dict, **collection):
    return load_polygons(write(tmp_path, *features, **collection))


def refuses(tmp_path: Path, *features: dict, **collection) -> str:
    with pytest.raises(ValueError) as raised:
        load(tmp_path, *features, **collection)
    return str(raised.value)


# --------------------------------------------------------------------------
# What a well-formed file yields
# --------------------------------------------------------------------------


def test_a_polygon_carries_its_id_purpose_and_the_sites_it_relates_to(tmp_path):
    (polygon,) = load(
        tmp_path,
        feature(polygon_id="KELP:LJ-REGIONAL", purpose="control", site_ids=["NDBC:LJAC1", "X:2"]),
    )
    assert polygon.polygon_id == "KELP:LJ-REGIONAL"
    assert polygon.purpose == "control"
    assert polygon.site_ids == ("NDBC:LJAC1", "X:2")


def test_optional_labels_are_carried_and_absent_ones_are_none(tmp_path):
    (polygon,) = load(tmp_path, feature(name="La Jolla regional", notes="provisional"))
    assert (polygon.name, polygon.notes) == ("La Jolla regional", "provisional")

    (bare,) = load(tmp_path, feature())
    assert (bare.name, bare.notes) == (None, None)


def test_the_geometry_frame_is_wgs84_and_carries_the_id_beside_the_shape(tmp_path):
    """The aggregation joins pixel centroids against this frame, so it has to
    state its own reference system rather than be assumed into one."""
    loaded = load(tmp_path, feature(polygon_id="KELP:A"), feature(polygon_id="KELP:B"))

    assert loaded.frame.crs == WGS84
    assert list(loaded.frame.columns) == ["polygon_id", "purpose", "geometry"]
    assert loaded.frame["polygon_id"].tolist() == ["KELP:A", "KELP:B"]
    assert loaded.frame.geometry.is_valid.all()


def test_file_order_is_preserved_so_a_run_over_every_polygon_is_reproducible(tmp_path):
    loaded = load(
        tmp_path,
        feature(polygon_id="KELP:C"),
        feature(polygon_id="KELP:A"),
        feature(polygon_id="KELP:B"),
    )
    assert loaded.ids == ("KELP:C", "KELP:A", "KELP:B")
    assert loaded.frame["polygon_id"].tolist() == ["KELP:C", "KELP:A", "KELP:B"]


def test_a_multipolygon_is_one_polygon_record(tmp_path):
    """A kelp bed split by a headland is one analysis area, not two."""
    multi = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"]]}
    loaded = load(tmp_path, feature(multi))

    assert len(loaded) == 1
    assert loaded.frame.geometry.iloc[0].geom_type == "MultiPolygon"


def test_lookup_by_id_and_by_site(tmp_path):
    """docs/03 integrity rule: joins go through registry keys, so which polygon
    pairs with which station is read off the file rather than string-matched."""
    loaded = load(
        tmp_path,
        feature(polygon_id="KELP:NEAR", purpose="near_site", site_ids=["PROJ:YELLOW-BUOY"]),
        feature(polygon_id="KELP:CTRL", purpose="control", site_ids=["NDBC:LJAC1"]),
    )

    assert loaded.get("KELP:NEAR").purpose == "near_site"
    assert loaded.get("KELP:MISSING") is None
    assert "KELP:CTRL" in loaded
    assert [p.polygon_id for p in loaded.for_site("NDBC:LJAC1")] == ["KELP:CTRL"]
    assert loaded.for_site("NDBC:NOBODY") == ()


def test_every_declared_purpose_is_accepted(tmp_path):
    """The vocabulary and the loader cannot drift apart while this passes."""
    for purpose in POLYGON_PURPOSES:
        (polygon,) = load(tmp_path, feature(purpose=purpose))
        assert polygon.purpose == purpose


def test_comment_keys_are_not_properties(tmp_path):
    """The shipped file explains itself in `_`-prefixed keys, as `features.json` does."""
    (polygon,) = load(tmp_path, feature(_comment="drawn 2026-08-26, provisional"))
    assert polygon.polygon_id == "KELP:A"


# --------------------------------------------------------------------------
# An empty registry is a project mid-way through, not an error
# --------------------------------------------------------------------------


def test_an_empty_collection_loads_and_yields_nothing(tmp_path):
    """`kelpcompare features` must not fail for having no polygons drawn yet."""
    loaded = load(tmp_path)

    assert len(loaded) == 0
    assert loaded.ids == ()
    assert loaded.frame.empty
    assert loaded.frame.crs == WGS84


def test_the_committed_registry_loads():
    """The file the repository ships. Empty today; this is what catches the day
    a polygon is added by hand in a shape the loader refuses."""
    loaded = load_polygons(COMMITTED)
    assert loaded.path == COMMITTED
    assert all(p.purpose in POLYGON_PURPOSES for p in loaded)


# --------------------------------------------------------------------------
# Refusals -- each one a hand-edit that would otherwise produce a series
# --------------------------------------------------------------------------


def test_an_unknown_purpose_is_refused(tmp_path):
    message = refuses(tmp_path, feature(purpose="offshore"))
    assert "'offshore'" in message
    assert "near_site" in message


def test_a_missing_purpose_is_refused_rather_than_defaulted(tmp_path):
    assert "purpose None" in refuses(tmp_path, feature(purpose=_ABSENT))


def test_a_missing_or_blank_id_is_refused(tmp_path):
    assert "polygon_id" in refuses(tmp_path, feature(polygon_id=_ABSENT))
    assert "polygon_id" in refuses(tmp_path, feature(polygon_id="   "))


def test_a_repeated_id_is_refused_rather_than_merged(tmp_path):
    """The id is what every kelp row is keyed on; two areas under one key would
    aggregate into one series without saying so."""
    message = refuses(tmp_path, feature(polygon_id="KELP:A"), feature(polygon_id="KELP:A"))
    assert "twice" in message
    assert "KELP:A" in message


def test_no_site_ids_is_refused(tmp_path):
    """A polygon paired with no site produces no comparison row and would vanish."""
    assert "site_ids" in refuses(tmp_path, feature(site_ids=[]))
    assert "site_ids" in refuses(tmp_path, feature(site_ids=_ABSENT))
    assert "site_ids" in refuses(tmp_path, feature(site_ids="NDBC:LJAC1"))


def test_a_repeated_site_id_is_refused(tmp_path):
    assert "twice" in refuses(tmp_path, feature(site_ids=["NDBC:LJAC1", "NDBC:LJAC1"]))


def test_an_unknown_property_is_refused_rather_than_ignored(tmp_path):
    """A `puprose` typo would otherwise read as a polygon with no purpose at all."""
    message = refuses(tmp_path, feature(puprose="control"))
    assert "puprose" in message


def test_the_offending_polygon_is_named_in_the_message(tmp_path):
    """Two polygons in a file, one bad: the message has to say which."""
    message = refuses(
        tmp_path,
        feature(polygon_id="KELP:GOOD"),
        feature(polygon_id="KELP:BAD", purpose="whatever"),
    )
    assert "KELP:BAD" in message
    assert "KELP:GOOD" not in message


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def test_a_polygon_with_no_geometry_is_refused(tmp_path):
    message = refuses(tmp_path, feature(geometry=None))
    assert "no geometry" in message


def test_a_point_or_a_line_is_refused(tmp_path):
    """No interior for a pixel centroid to fall inside."""
    point = {"type": "Point", "coordinates": [0.0, 0.0]}
    line = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.1, 0.1]]}

    assert "'Point'" in refuses(tmp_path, feature(point))
    assert "'LineString'" in refuses(tmp_path, feature(line))


def test_an_empty_polygon_is_refused(tmp_path):
    """It contains no pixel, so it would produce an all-null series, not an error."""
    message = refuses(tmp_path, feature({"type": "Polygon", "coordinates": []}))
    assert "empty" in message
    assert "KELP:A" in message


def test_a_self_intersecting_ring_is_refused(tmp_path):
    """A bow-tie encloses an area nobody drew; every pixel test against it is a guess."""
    message = refuses(tmp_path, feature(BOWTIE))
    assert "not valid geometry" in message
    assert "KELP:A" in message


def test_a_ring_too_short_to_close_names_the_polygon_it_came_from(tmp_path):
    """The failure happens inside geometry construction, so the loader has to go
    back and find which feature caused it."""
    stub = {"type": "Polygon", "coordinates": [[[0.0, 0.0], [0.1, 0.0]]]}
    message = refuses(tmp_path, feature(polygon_id="KELP:OK"), feature(stub, polygon_id="KELP:X"))
    assert "KELP:X" in message


# --------------------------------------------------------------------------
# The reference system
# --------------------------------------------------------------------------


def test_a_file_declaring_a_projected_crs_is_refused_rather_than_reprojected(tmp_path):
    """The failure this exists to stop is silent: the polygon still loads, still
    aggregates, and describes water hundreds of metres from where it was drawn."""
    crs = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32611"}}
    message = refuses(tmp_path, feature(), crs=crs)

    assert "32611" in message
    assert "WGS84" in message or WGS84 in message


def test_a_file_declaring_wgs84_explicitly_is_accepted(tmp_path):
    """Redundant under RFC 7946, but a GIS export may still write it."""
    for name in ("EPSG:4326", "urn:ogc:def:crs:OGC:1.3:CRS84"):
        loaded = load(tmp_path, feature(), crs={"type": "name", "properties": {"name": name}})
        assert len(loaded) == 1


# --------------------------------------------------------------------------
# The file as a whole
# --------------------------------------------------------------------------


def test_something_that_is_not_a_feature_collection_is_refused(tmp_path):
    target = tmp_path / "polygons.geojson"
    target.write_text(json.dumps(SQUARE), encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        load_polygons(target)
    assert "FeatureCollection" in str(raised.value)


def test_a_feature_without_properties_is_refused(tmp_path):
    bare = {"type": "Feature", "properties": None, "geometry": SQUARE}
    assert "properties" in refuses(tmp_path, bare)
