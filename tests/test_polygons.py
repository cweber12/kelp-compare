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
from shapely.geometry import Point

from kelpcompare.polygons import (
    POLYGON_GEOMETRIES,
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
    declared = {
        "polygon_id": "KELP:A",
        "purpose": "regional",
        "site_ids": ["NDBC:LJAC1"],
        "source_file": "kelp_a.csv",
    }
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
        feature(polygon_id="KELP:NEAR", purpose="near_site", site_ids=["PROJ:TIDBIT-1"]),
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
    """The file the repository ships: the six San Diego county beds exported
    from kelpwatch.org, each claiming the file its rows arrive in."""
    loaded = load_polygons(COMMITTED)

    assert loaded.path == COMMITTED
    assert len(loaded) == 6
    assert all(p.purpose in POLYGON_PURPOSES for p in loaded)
    assert all(p.source_file.endswith(".csv") for p in loaded)
    assert all(p.site_ids for p in loaded)

    # The labels as they stand. What they *say* -- that La Jolla is the bed
    # holding a reference station -- is false against the recorded outlines, and
    # relabelling is https://github.com/cweber12/kelp-compare/issues/86 rather
    # than a correction to make here.
    assert [p.polygon_id for p in loaded if p.purpose == "regional"] == ["KELP:LA-JOLLA"]
    assert loaded.for_file("kelp_lajolla.csv").polygon_id == "KELP:LA-JOLLA"


def test_the_committed_registry_pairs_every_bed_with_both_public_references():
    """Pairing is what lets a series reach the docs/04 s4.1 screen, and losing
    one is silent. A station no polygon names is still fetched, flagged and
    aggregated into `quarterly_env`; it is only at the comparison join that it
    pairs with nothing, and the result is a `comparison.parquet` that is simply
    smaller than it should be, with no column anywhere saying why. The Shore
    Stations record sat in that state through a rebuild, which is why the
    pairing is asserted here rather than left to the file to remember.

    The registry names sites, not series, so naming the pier pairs every series
    it produced -- both sensor depths. That is the decision, and it is the one
    an edit trimming this list back to a single depth would quietly reverse.
    """
    for polygon in load_polygons(COMMITTED):
        assert set(polygon.site_ids) == {
            "NDBC:LJAC1",
            "SIO:LAJOLLA-PIER",
            "PROJ:TIDBIT-1",
            "PROJ:TIDBIT-2",
        }


def test_the_committed_registry_pins_the_revision_the_exports_came_from():
    kelp_watch = load_polygons(COMMITTED).kelp_watch
    assert kelp_watch.revision == 23
    assert kelp_watch.doi == "10.6073/pasta/2c1218b7ebe6967da52000adf02f6a8b"


def test_the_committed_registry_claims_every_recorded_fixture():
    """The fixtures and the registry must not drift: a fixture the registry does
    not claim would be quarantined by the ingest suite for a reason that is a
    registry gap rather than the case under test."""
    loaded = load_polygons(COMMITTED)
    recorded = (REPO_ROOT / "tests" / "fixtures" / "kelpwatch").glob("*.csv")
    assert all(loaded.for_file(path.name) is not None for path in recorded)


def test_comment_keys_inside_the_pinned_revision_are_not_configuration():
    loaded = load_polygons(COMMITTED)
    assert loaded.kelp_watch.revision == 23  # the shipped block carries a _comment


# --------------------------------------------------------------------------
# The recorded outlines -- what a reconstruction has to keep being true
# --------------------------------------------------------------------------


def test_every_committed_bed_carries_a_real_outline():
    """No bed may quietly go back to `null`.

    The outlines were reconstructed from Kelp Watch's own classified cells and
    each one was checked against the /aggregate endpoint (docs/02, "How the six
    bed outlines were reconstructed"). That check cannot run here -- tests never
    reach the network -- so what is asserted is the part a later hand-edit could
    break without anyone noticing: that an outline is still there, that it still
    encloses area, and that it still says how it was verified.
    """
    loaded = load_polygons(COMMITTED)

    assert all(polygon.has_geometry for polygon in loaded)
    assert set(loaded.frame.geometry.geom_type) <= set(POLYGON_GEOMETRIES)
    assert loaded.frame.geometry.is_valid.all()
    assert (~loaded.frame.geometry.is_empty).all()

    recorded = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert all("_verified" in feature["properties"] for feature in recorded["features"])


def test_the_committed_outlines_are_off_san_diego_and_not_transposed():
    """A lon/lat swap or a reprojection survives every other check in this file.

    It produces a perfectly valid polygon of the wrong water -- the failure the
    CRS refusal exists for, arriving through the coordinates instead of through
    the `crs` member. San Diego county's kelp sits between 32.4 and 33.1 N and
    -117.4 and -117.0 E; a transposed outline lands in the Mediterranean.
    """
    bounds = load_polygons(COMMITTED).frame.total_bounds  # minx, miny, maxx, maxy

    assert -117.4 < bounds[0] and bounds[2] < -117.0
    assert 32.4 < bounds[1] and bounds[3] < 33.1


def test_no_two_committed_beds_overlap():
    """Six exports, six disjoint areas, and no cell counted twice.

    The beds were derived as spatially isolated clusters at least 677 m apart,
    so an overlap here is not a near-miss -- it means an outline was widened or
    moved onto a neighbour, and the two beds' canopy series would then share
    cells while still reading as independent controls in docs/04 s4.5.
    """
    frame = load_polygons(COMMITTED).frame
    geometries = list(frame.geometry)

    overlaps = [
        (frame.polygon_id.iloc[i], frame.polygon_id.iloc[j])
        for i in range(len(geometries))
        for j in range(i + 1, len(geometries))
        if geometries[i].intersects(geometries[j])
    ]
    assert overlaps == []


def test_only_the_project_sensors_are_inside_the_la_jolla_bed():
    """The containment `_provisional` asserts, computed rather than repeated.

    This is the claim the registry got wrong until the outlines landed: it said
    NDBC:LJAC1's published position is inside the La Jolla bed, and it is 1.7 km
    north of it. Both project sensors *are* inside, which is what makes
    `near_site` declarable and is the open question in
    https://github.com/cweber12/kelp-compare/issues/86. Pinning it here means a
    later edit to either registry has to face the pairing rather than silently
    invert it.
    """
    frame = load_polygons(COMMITTED).frame
    bed = frame.loc[frame.polygon_id == "KELP:LA-JOLLA", "geometry"].iloc[0]
    sites = json.loads((REPO_ROOT / "data" / "registry" / "sites.json").read_text(encoding="utf-8"))
    placed = {s["site_id"]: (s["lon"], s["lat"]) for s in sites["sites"] if "lat" in s}

    def contains(site_id: str) -> bool:
        return bed.contains(Point(*placed[site_id]))

    assert contains("PROJ:TIDBIT-1")
    assert contains("PROJ:TIDBIT-2")
    assert not contains("NDBC:LJAC1")
    assert not contains("SIO:LAJOLLA-PIER")


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


def test_a_polygon_may_declare_that_its_outline_is_not_recorded_yet(tmp_path):
    """The export arrives already summed over the selected geometry, so no number
    depends on the outline. Forcing an invented one in to satisfy the loader
    would be worse than recording its absence."""
    (polygon,) = load(tmp_path, feature(geometry=None))

    assert polygon.has_geometry is False
    assert polygon.polygon_id == "KELP:A"


def test_a_feature_with_no_geometry_member_at_all_is_refused(tmp_path):
    """Every GeoJSON Feature has the member (RFC 7946); null is how you say
    "not recorded", and omitting it is an unfinished edit."""
    bare = feature()
    del bare["geometry"]

    message = refuses(tmp_path, bare)
    assert "geometry" in message
    assert "null" in message


def test_a_drawn_polygon_says_so(tmp_path):
    (polygon,) = load(tmp_path, feature())
    assert polygon.has_geometry is True


def test_a_point_or_a_line_is_refused(tmp_path):
    """No interior for a pixel centroid to fall inside."""
    point = {"type": "Point", "coordinates": [0.0, 0.0]}
    line = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.1, 0.1]]}

    assert "'Point'" in refuses(tmp_path, feature(point))
    assert "'LineString'" in refuses(tmp_path, feature(line))


def test_an_empty_polygon_is_refused_even_though_a_null_one_is_not(tmp_path):
    """An outline of no extent is a mistake; an absent one is a fact. The loader
    must not let the two collapse into each other."""
    message = refuses(tmp_path, feature({"type": "Polygon", "coordinates": []}))
    assert "empty" in message
    assert "KELP:A" in message
    assert "null" in message  # ...and it says which one to use instead


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


# --------------------------------------------------------------------------
# Which export belongs to which polygon
# --------------------------------------------------------------------------
#
# A Kelp Watch CSV names the geometry it describes nowhere in the file
# (docs/02), so the registry is the only thing that can say. Getting this wrong
# attributes one bed's forty years to another polygon, and nothing downstream
# looks wrong.


def test_a_polygon_declares_the_export_its_rows_arrive_in(tmp_path):
    (polygon,) = load(tmp_path, feature(source_file="kelp_lajolla.csv"))
    assert polygon.source_file == "kelp_lajolla.csv"


def test_a_file_is_matched_to_its_polygon_by_name(tmp_path):
    loaded = load(
        tmp_path,
        feature(polygon_id="KELP:LAJOLLA", source_file="kelp_lajolla.csv"),
        feature(polygon_id="KELP:DELMAR", source_file="kelp_delmar.csv"),
    )
    assert loaded.for_file("kelp_delmar.csv").polygon_id == "KELP:DELMAR"
    assert loaded.for_file("kelp_nobody.csv") is None


def test_a_file_is_matched_wherever_it_was_dropped_and_however_it_is_cased(tmp_path):
    """Where the operator put the file is not a registry fact."""
    loaded = load(tmp_path, feature(source_file="kelp_lajolla.csv"))

    assert loaded.for_file("KELP_LAJOLLA.CSV") is not None
    assert loaded.for_file("/somewhere/else/kelp_lajolla.csv") is not None
    assert loaded.for_file(str(Path("C:/drop/kelp_lajolla.csv"))) is not None


def test_a_polygon_with_no_source_file_is_refused(tmp_path):
    """It could never receive a row, so it would sit in the registry looking
    like coverage while contributing nothing."""
    assert "source_file" in refuses(tmp_path, feature(source_file=_ABSENT))
    assert "source_file" in refuses(tmp_path, feature(source_file="  "))


def test_a_source_file_given_as_a_path_is_refused(tmp_path):
    message = refuses(tmp_path, feature(source_file="incoming/kelp_lajolla.csv"))
    assert "name alone" in message


# --------------------------------------------------------------------------
# The pinned dataset revision
# --------------------------------------------------------------------------


def test_the_registry_pins_the_dataset_revision_and_its_doi(tmp_path):
    """The CSV carries no version of any kind, so this is the only place the
    chain from a figure back to a DOI can be closed (docs/02)."""
    loaded = load(
        tmp_path,
        feature(),
        kelp_watch={"revision": 23, "doi": "10.6073/pasta/2c1218b7ebe6967da52000adf02f6a8b"},
    )
    assert loaded.kelp_watch.revision == 23
    assert loaded.kelp_watch.doi.startswith("10.6073/")
    assert loaded.kelp_watch.label == "ver23"


def test_a_registry_that_pins_no_revision_loads_and_says_so(tmp_path):
    """Legitimately revision-less until the first export is recorded; it is the
    ingest that refuses, because that is the moment one would be invented."""
    assert load(tmp_path, feature()).kelp_watch is None


def test_a_revision_that_is_not_a_whole_number_is_refused(tmp_path):
    for bad in ({"revision": "23"}, {"revision": 23.5}, {"revision": 0}, {"revision": True}):
        assert "revision" in refuses(tmp_path, feature(), kelp_watch=bad)


def test_an_empty_or_unknown_kelp_watch_block_is_refused(tmp_path):
    assert "kelp_watch" in refuses(tmp_path, feature(), kelp_watch={})
    assert "version" in refuses(tmp_path, feature(), kelp_watch={"revision": 23, "version": 1})


def test_an_unknown_top_level_member_is_refused(tmp_path):
    """A `kelpwatch` typo would otherwise leave the registry silently unpinned."""
    assert "kelpwatch" in refuses(tmp_path, feature(), kelpwatch={"revision": 23})
