"""Reads `data/registry/polygons.geojson`: which water a kelp number describes (docs/03).

A fourth registry file beside `sites.json`, `parameters.json` and
`features.json`, separate from all three for the reason they are separate from
each other: it answers a different question. `sites.json` records where an
instrument was; this records the *areas* the analysis compares kelp over. A
polygon is not a site — it has no instrument, no timezone and no deployment —
and a canopy value belongs to one of these rather than to a `site_id`, which is
why the kelp half of the features zone is keyed on `polygon_id`.

The registry has two jobs. It records **which export belongs to which polygon**,
because a Kelp Watch CSV names the geometry it describes nowhere in the file
(docs/02) -- only in its filename. And it records **what that geometry was**, so
that which water a number describes is diffable line by line rather than living
only in the browser session where it was selected.

Geometry is optional, and that is a change of role rather than a relaxation. The
export arrives already summed over the selected geometry, so no number in this
project is computed from these outlines any more; what still needs them is the
docs/04 §4.5 distance-decay test, and the stage that runs it is the one that
should refuse a polygon without one. A polygon whose outline has not been
recorded yet declares `"geometry": null` and says so out loud. A polygon whose
outline is *malformed* is still refused -- "not drawn yet" and "drawn wrong" are
different facts and must not collapse into one.

**Declared in WGS84, and said so out loud.** GeoJSON is WGS84 by definition
(RFC 7946), so a file that names any other reference system is refused rather
than reprojected: a silent CRS mismatch shifts a polygon by hundreds of metres,
which at the 30 m Landsat resolution the kelp product publishes at is tens of
pixels of the wrong ocean. The loaded frame carries `EPSG:4326` explicitly, so
the aggregation joins against something that states its own units.

The posture is the parameter and feature registries', deliberately: **refuse
rather than ignore**. An unknown purpose, an unknown property, a missing or
repeated id, an empty `site_ids`, an absent `source_file`, and a geometry that
is present but empty, of the wrong kind, or self-intersecting — each raises
here, naming the file and the offending feature. None of them fails loudly
downstream: a wrong `source_file` attributes one bed's forty years to another
polygon, and neither table nor figure would look wrong.

What this module deliberately does **not** check: that each `site_ids` entry
exists in `sites.json`. That is a cross-file claim, and no registry loader here
makes one — `neighbor_refs` in the site registry has exactly the same property.
A typo there costs the comparison table its rows for that pair rather than
producing wrong ones, so the stage that builds those rows is where it belongs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

DEFAULT_POLYGONS_PATH = Path("data/registry/polygons.geojson")

#: The CRS every polygon is declared in. GeoJSON has no other option (RFC 7946);
#: this is written down so the frame the aggregation receives states its units.
WGS84 = "EPSG:4326"

#: What a polygon is *for*, as a controlled vocabulary. `near_site` sits on a
#: sensor, `control` is the comparison area away from one, `regional` is the
#: wider bed the site sits in. Refused if unknown, because a purpose nobody
#: implements reads in the output exactly like one that was applied.
POLYGON_PURPOSES = ("control", "near_site", "regional")

#: The geometry kinds an area can be. A point or a line has no interior and no
#: extent, so it is not an analysis polygon under another name -- it is a mistake.
POLYGON_GEOMETRIES = ("Polygon", "MultiPolygon")

#: Properties a feature may carry. `_`-prefixed keys are comments, as in
#: `features.json`, which is how the shipped file explains itself.
_PROPERTY_KEYS = frozenset({"polygon_id", "purpose", "site_ids", "source_file", "name", "notes"})

#: The top-level members the file may carry beside `type` and `features`. `bbox`
#: is RFC 7946's; `crs` is the superseded 2008 draft's and is only ever refused;
#: `kelp_watch` is this project's, and pins the dataset revision.
_COLLECTION_KEYS = frozenset({"type", "features", "bbox", "crs", "kelp_watch"})

#: What the `kelp_watch` member declares.
_KELP_WATCH_KEYS = frozenset({"revision", "doi"})

#: The GeoJSON member that would name a reference system other than WGS84. It is
#: from the superseded 2008 draft; RFC 7946 removed it. Present and naming
#: anything else, the file is refused.
_CRS_KEY = "crs"

_WGS84_NAMES = frozenset(
    {
        "epsg:4326",
        "urn:ogc:def:crs:ogc:1.3:crs84",
        "urn:ogc:def:crs:epsg::4326",
        "wgs84",
        "crs84",
    }
)


@dataclass(frozen=True)
class KelpWatch:
    """Which revision of the upstream dataset the exports in this registry are.

    The CSV export carries no version of any kind, so this is the only place the
    provenance chain from a figure back to a DOI can be closed, and an ingest
    refuses to run without it (docs/02).

    One revision for the whole registry rather than one per polygon, and that is
    deliberate rather than lazy. A newer revision may revise history as well as
    extend it -- the upstream product recalibrates between sensors and fills
    scan-line gaps -- so two revisions must never be mixed inside one analysis.
    Pinning it once means bumping it obsoletes every landing at the old revision
    at the same moment, which is loud: a bed not re-exported produces no rows
    rather than quietly contributing stale ones.
    """

    revision: int
    doi: str | None = None

    @property
    def label(self) -> str:
        """`ver23` -- the landing directory, so two revisions cannot interleave."""
        return f"ver{self.revision}"


@dataclass(frozen=True)
class Polygon:
    """One analysis polygon's metadata, without its geometry.

    The geometry is deliberately not here. It lives in `Polygons.frame`, where
    geopandas can do point-in-polygon against a whole grid of Landsat pixel
    centroids at once; a shapely object hanging off a dataclass would invite a
    per-polygon Python loop over hundreds of thousands of pixels.

    `site_ids` is a tuple rather than a frame column for the same reason in
    reverse: a list-valued column is a trap in every pandas operation that
    touches it, and the polygon-to-site association is looked up, not
    aggregated.
    """

    polygon_id: str
    purpose: str
    site_ids: tuple[str, ...]
    source_file: str
    has_geometry: bool = False
    name: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class Polygons:
    """The parsed file: the metadata records, the geometry, and where it came from.

    Both views are built from the same validated features in one pass, so they
    cannot come to disagree about which polygons exist or what order they are in.
    """

    path: Path
    polygons: tuple[Polygon, ...]
    frame: gpd.GeoDataFrame
    kelp_watch: KelpWatch | None = None

    def __len__(self) -> int:
        return len(self.polygons)

    def __iter__(self):
        return iter(self.polygons)

    def __contains__(self, polygon_id: object) -> bool:
        return any(p.polygon_id == polygon_id for p in self.polygons)

    @property
    def ids(self) -> tuple[str, ...]:
        """Every polygon id, in file order -- so a run over "every polygon" is
        reproducible for the reason `find_stations` preserves site order."""
        return tuple(p.polygon_id for p in self.polygons)

    def get(self, polygon_id: str) -> Polygon | None:
        for polygon in self.polygons:
            if polygon.polygon_id == polygon_id:
                return polygon
        return None

    def for_file(self, filename: str) -> Polygon | None:
        """The polygon whose export arrives under this filename, or None.

        The registry gate for this source (hard rule 5, docs/02): a Kelp Watch
        CSV carries no identifier for the geometry it describes, so an export
        the registry does not claim is quarantined rather than attributed to a
        polygon by guesswork. Matched case-insensitively on the name alone, so
        moving the file between directories does not unclaim it.
        """
        wanted = Path(filename).name.casefold()
        for polygon in self.polygons:
            if polygon.source_file.casefold() == wanted:
                return polygon
        return None

    def for_site(self, site_id: str) -> tuple[Polygon, ...]:
        """Every polygon that declares a relationship to one site.

        The one lookup the comparison stage needs, so that which polygon pairs
        with which station is read off the registry rather than string-matched
        out of a polygon's name (docs/03 integrity rules).
        """
        return tuple(p for p in self.polygons if site_id in p.site_ids)


def load_polygons(path: Path | str | None = None) -> Polygons:
    """Load the analysis polygons. Defaults to `data/registry/polygons.geojson`.

    An empty `FeatureCollection` loads cleanly and yields no polygons. That is
    the state the repository ships in and it is not an error: a project with
    environmental data and no polygons drawn yet is a project mid-way through,
    and refusing it would make `kelpcompare features` fail for having nothing
    kelp-shaped to do.
    """
    resolved = Path(path) if path is not None else DEFAULT_POLYGONS_PATH
    with resolved.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    _check_collection(payload, path=resolved)
    _check_crs(payload, path=resolved)

    features = payload.get("features") or []
    records = tuple(
        _polygon(feature, index=index, path=resolved) for index, feature in enumerate(features)
    )
    _check_unique(records, path=resolved)

    return Polygons(
        path=resolved,
        polygons=records,
        frame=_frame(features, path=resolved),
        kelp_watch=_kelp_watch(payload.get("kelp_watch"), path=resolved),
    )


# --------------------------------------------------------------------------
# The file as a whole
# --------------------------------------------------------------------------


def _check_collection(payload, *, path: Path) -> None:
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        kind = payload.get("type") if isinstance(payload, dict) else type(payload).__name__
        raise ValueError(
            f"{path} is {kind!r}, not a GeoJSON FeatureCollection; the polygon registry is "
            "one collection of Features, so a bare geometry or a list has no id to name it by"
        )
    features = payload.get("features", [])
    if features is not None and not isinstance(features, list):
        raise ValueError(f"{path} `features` is {type(features).__name__}, not a list of Features")

    _reject_unknown(
        {k: v for k, v in payload.items() if not k.startswith("_")},
        _COLLECTION_KEYS,
        where="the collection",
        path=path,
    )


def _check_crs(payload, *, path: Path) -> None:
    """Refuse a file that names a reference system other than WGS84.

    Never reproject. A polygon whose coordinates are not what they claim is
    displaced by hundreds of metres, and at 30 m resolution that is tens of
    pixels -- a wrong answer that looks exactly like a right one. Refusing puts
    it in front of a human while it is still a file rather than a correlation.
    """
    declared = payload.get(_CRS_KEY)
    if declared is None:
        return
    name = _crs_name(declared)
    if name is None or name.lower() not in _WGS84_NAMES:
        raise ValueError(
            f"{path} declares crs {name or declared!r}; polygons are WGS84 ({WGS84}) and are "
            "never reprojected here -- a shifted polygon aggregates the wrong pixels and "
            "reads exactly like a correct one. Re-export the file in WGS84."
        )


def _crs_name(declared) -> str | None:
    if isinstance(declared, str):
        return declared
    if isinstance(declared, dict):
        properties = declared.get("properties")
        if isinstance(properties, dict):
            name = properties.get("name")
            return str(name) if name is not None else None
    return None


def _kelp_watch(block, *, path: Path) -> KelpWatch | None:
    """The pinned dataset revision, or None if the file does not pin one.

    None rather than a raise, because the registry is legitimately revision-less
    until the first export is recorded, and a project with polygons drawn and no
    kelp landed yet is a project mid-way through. It is the *ingest* that
    refuses, which is the moment a revision would otherwise be invented.
    """
    if block is None:
        return None
    if not isinstance(block, dict) or not block:
        raise ValueError(
            f"{path} `kelp_watch` is {block!r}; declare {sorted(_KELP_WATCH_KEYS)} or omit it"
        )
    block = {key: value for key, value in block.items() if not key.startswith("_")}
    _reject_unknown(block, _KELP_WATCH_KEYS, where="`kelp_watch`", path=path)

    revision = block.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError(
            f"{path} `kelp_watch.revision` is {revision!r}; expected the whole revision "
            "number the export's recommended citation names, e.g. 23"
        )
    doi = block.get("doi")
    return KelpWatch(revision=revision, doi=None if doi is None else str(doi))


def _check_unique(records: tuple[Polygon, ...], *, path: Path) -> None:
    seen: set[str] = set()
    for record in records:
        if record.polygon_id in seen:
            raise ValueError(
                f"{path} declares polygon_id {record.polygon_id!r} twice; the id is the key "
                "every kelp row and every comparison row joins on, so a repeat would silently "
                "merge two areas into one series"
            )
        seen.add(record.polygon_id)


# --------------------------------------------------------------------------
# One feature
# --------------------------------------------------------------------------


def _polygon(feature, *, index: int, path: Path) -> Polygon:
    where = f"feature {index}"
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise ValueError(f"{path} {where} is not a GeoJSON Feature")

    properties = _uncommented(feature.get("properties"), where=where, path=path)
    _reject_unknown(properties, _PROPERTY_KEYS, where=where, path=path)

    polygon_id = _identifier(properties.get("polygon_id"), where=where, path=path)
    where = f"polygon {polygon_id!r}"

    if "geometry" not in feature:
        raise ValueError(
            f"{path} {where} has no `geometry` member; every GeoJSON Feature has one "
            '(RFC 7946). Declare `"geometry": null` to say the outline is not recorded yet'
        )
    drawn = _check_geometry(feature["geometry"], where=where, path=path)

    return Polygon(
        polygon_id=polygon_id,
        purpose=_purpose(properties.get("purpose"), where=where, path=path),
        site_ids=_site_ids(properties.get("site_ids"), where=where, path=path),
        source_file=_source_file(properties.get("source_file"), where=where, path=path),
        has_geometry=drawn,
        name=_optional_text(properties.get("name")),
        notes=_optional_text(properties.get("notes")),
    )


def _source_file(value, *, where: str, path: Path) -> str:
    """The filename this polygon's export arrives under. Required.

    Required for the reason `site_ids` is: a Kelp Watch CSV names the geometry
    it describes nowhere in the file (docs/02), so a polygon that claims no
    export can never receive a row -- it would sit in the registry looking like
    coverage while contributing nothing, which is a half-finished edit rather
    than a decision.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{path} {where} declares source_file {value!r}; name the export file this "
            'polygon\'s rows arrive in, e.g. "kelp_lajolla.csv" -- the CSV itself carries '
            "no identifier for the geometry it describes"
        )
    name = value.strip()
    if Path(name).name != name:
        raise ValueError(
            f"{path} {where} declares source_file {name!r}; give the file's name alone, "
            "not a path -- where the operator drops it is not a registry fact"
        )
    return name


def _identifier(value, *, where: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{path} {where} declares polygon_id {value!r}; every polygon needs a non-empty "
            "id, because it is what a kelp row is keyed on and what an error can name it by"
        )
    return value.strip()


def _purpose(value, *, where: str, path: Path) -> str:
    """A polygon says what it is for, from a closed vocabulary.

    Closed for the reason `features.json` closes its feature sets: a purpose
    nothing implements sits in the registry looking like coverage, and in the
    output table a polygon with an invented purpose is indistinguishable from
    one whose purpose was honoured.
    """
    if value not in POLYGON_PURPOSES:
        raise ValueError(
            f"{path} {where} declares purpose {value!r}; known purposes are "
            f"{list(POLYGON_PURPOSES)}"
        )
    return value


def _site_ids(value, *, where: str, path: Path) -> tuple[str, ...]:
    """The sites this polygon is compared against. Required, and non-empty.

    Non-empty because a polygon paired with no site produces no comparison row
    at all: it is a half-finished edit rather than a decision, and it would
    disappear from the analysis silently. A `control` polygon is still declared
    against the sites it controls *for* -- that is what makes it a control.
    """
    if not isinstance(value, list) or not value:
        raise ValueError(
            f"{path} {where} declares site_ids {value!r}; expected a non-empty list of "
            "site_ids, since a polygon paired with no site yields no comparison row and "
            "would vanish from the analysis without saying so"
        )
    ids = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"{path} {where} declares site_id {entry!r}, which is not a name")
        ids.append(entry.strip())
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path} {where} declares a site_id twice: {ids}")
    return tuple(ids)


def _check_geometry(geometry, *, where: str, path: Path) -> bool:
    """Whether an outline was recorded, refusing anything that is not an area.

    A null geometry is accepted and reported as "not recorded yet": the export
    arrives already summed over the selected geometry, so no number depends on
    the outline, and forcing an invented one into the registry to satisfy a
    loader would be worse than recording its absence. What is still refused is a
    geometry that is *present and wrong* -- a point, a line, an empty ring, a
    ring that crosses itself. "Not drawn yet" and "drawn wrong" are different
    facts, and only one of them is a mistake.

    Checked before the frame is built so the message names the polygon rather
    than an index into a batch construction.
    """
    if geometry is None:
        return False
    if not isinstance(geometry, dict) or geometry.get("type") not in POLYGON_GEOMETRIES:
        kind = geometry.get("type") if isinstance(geometry, dict) else type(geometry).__name__
        raise ValueError(
            f"{path} {where} has geometry type {kind!r}; an analysis polygon is one of "
            f"{list(POLYGON_GEOMETRIES)} -- a point or a line has no extent. Use null if the "
            "outline has simply not been recorded yet"
        )
    return True


def _reject_unknown(block: dict, allowed: frozenset[str], *, where: str, path: Path) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ValueError(
            f"{path} {where} declares unknown propert(ies) {unknown}; known: {sorted(allowed)}"
        )


def _uncommented(properties, *, where: str, path: Path) -> dict:
    if properties is None or not isinstance(properties, dict) or not properties:
        raise ValueError(
            f"{path} {where} carries no usable properties (got {properties!r}); a polygon "
            "needs at least an id, a purpose and the sites it relates to"
        )
    return {key: value for key, value in properties.items() if not key.startswith("_")}


def _optional_text(value) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------
# The geometry frame
# --------------------------------------------------------------------------


def _frame(features: list, *, path: Path) -> gpd.GeoDataFrame:
    """The polygons as a WGS84 GeoDataFrame, id and purpose beside the geometry.

    Built in one call on the happy path; on a construction failure the features
    are walked one at a time so the error names the polygon that would not
    build. Both matter: the fast path is what the aggregation runs, and a
    coordinate ring one vertex short is a hand-edit whose message should say
    which polygon to look at.
    """
    if not features:
        return gpd.GeoDataFrame(
            {"polygon_id": [], "purpose": [], "geometry": []}, geometry="geometry", crs=WGS84
        )
    try:
        frame = gpd.GeoDataFrame.from_features(features, crs=WGS84)
    except Exception as error:  # re-raised below as a ValueError naming the culprit
        raise ValueError(_locate(features, error, path=path)) from error

    _check_built(frame, path=path)
    return frame[["polygon_id", "purpose", "geometry"]]


def _locate(features: list, error: Exception, *, path: Path) -> str:
    for feature in features:
        try:
            gpd.GeoDataFrame.from_features([feature], crs=WGS84)
        except Exception:  # noqa: BLE001 -- looking for which one, not what
            polygon_id = (feature.get("properties") or {}).get("polygon_id", "<no id>")
            return (
                f"{path} polygon {polygon_id!r} has a geometry that will not build: "
                f"{type(error).__name__}: {error}"
            )
    return f"{path}: geometry would not build: {type(error).__name__}: {error}"


def _check_built(frame: gpd.GeoDataFrame, *, path: Path) -> None:
    """The checks that need the geometry constructed: empty, and self-intersecting.

    Vectorised rather than per-feature because that is how a spatial join will
    use the same frame, and because an invalid ring is a property of the built
    polygon rather than of the JSON it came from.

    A polygon that declared no outline is skipped rather than judged. Both
    predicates would condemn it -- geopandas reports a missing geometry as
    invalid -- and "not recorded yet" is not the mistake these checks exist to
    catch.
    """
    # An explicit `is not None` rather than `notna()`: geopandas has changed its
    # mind about whether an *empty* geometry counts as missing, and this needs
    # empties kept in -- they are the mistake the next check is looking for.
    drawn = frame.loc[[geometry is not None for geometry in frame.geometry]]
    if drawn.empty:
        return

    empty = drawn.loc[drawn.geometry.is_empty, "polygon_id"].tolist()
    if empty:
        raise ValueError(
            f"{path} polygon(s) {empty} have an empty geometry; that is an outline of no "
            "extent rather than an absent one -- use null to say it is not recorded yet"
        )

    invalid = drawn.loc[~drawn.geometry.is_valid, "polygon_id"].tolist()
    if invalid:
        raise ValueError(
            f"{path} polygon(s) {invalid} are not valid geometry -- a ring that crosses "
            "itself encloses an area nobody drew. Fix the coordinates rather than leaving "
            "a later spatial join to resolve it."
        )


__all__ = [
    "DEFAULT_POLYGONS_PATH",
    "POLYGON_GEOMETRIES",
    "POLYGON_PURPOSES",
    "WGS84",
    "KelpWatch",
    "Polygon",
    "Polygons",
    "load_polygons",
]
