"""Reads `data/registry/polygons.geojson`: which water a kelp number describes (docs/03).

A fourth registry file beside `sites.json`, `parameters.json` and
`features.json`, separate from all three for the reason they are separate from
each other: it answers a different question. `sites.json` records where an
instrument was; this records the *areas* the analysis compares kelp over. A
polygon is not a site — it has no instrument, no timezone and no deployment —
and a canopy value belongs to one of these rather than to a `site_id`, which is
why the kelp half of the features zone is keyed on `polygon_id`.

The geometry lives in the repository so that which water a number describes is a
reviewable data change, diffable line by line, rather than a drawing made in a
browser session nobody can reproduce (docs/01 §2 requires regenerating
everything from raw with one command).

**Declared in WGS84, and said so out loud.** GeoJSON is WGS84 by definition
(RFC 7946), so a file that names any other reference system is refused rather
than reprojected: a silent CRS mismatch shifts a polygon by hundreds of metres,
which at the 30 m Landsat resolution the kelp product publishes at is tens of
pixels of the wrong ocean. The loaded frame carries `EPSG:4326` explicitly, so
the aggregation joins against something that states its own units.

The posture is the parameter and feature registries', deliberately: **refuse
rather than ignore**. An unknown purpose, a missing id, a repeated id, a
geometry that is null, empty, of the wrong kind, or self-intersecting — each
raises here, naming the file and the offending feature, because a malformed
polygon does not fail loudly downstream. It quietly aggregates the wrong pixels
and produces a plausible series, which is the one failure mode a reviewer
cannot catch by looking at the output.

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

#: The geometry kinds an area can be. A point or a line has no interior for a
#: pixel centroid to fall inside, so it is not an analysis polygon under another
#: name -- it is a mistake.
POLYGON_GEOMETRIES = ("Polygon", "MultiPolygon")

#: Properties a feature may carry. `_`-prefixed keys are comments, as in
#: `features.json`, which is how the shipped file explains itself.
_PROPERTY_KEYS = frozenset({"polygon_id", "purpose", "site_ids", "name", "notes"})

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

    return Polygons(path=resolved, polygons=records, frame=_frame(features, path=resolved))


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

    _check_geometry(feature.get("geometry"), where=where, path=path)
    return Polygon(
        polygon_id=polygon_id,
        purpose=_purpose(properties.get("purpose"), where=where, path=path),
        site_ids=_site_ids(properties.get("site_ids"), where=where, path=path),
        name=_optional_text(properties.get("name")),
        notes=_optional_text(properties.get("notes")),
    )


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


def _check_geometry(geometry, *, where: str, path: Path) -> None:
    """Refuse anything a pixel centroid cannot fall inside.

    Checked before the frame is built so the message names the polygon rather
    than an index into a batch construction, and checked at all because every
    one of these failures produces a *series* downstream rather than an error:
    a null or empty geometry contains no pixel and yields an all-null column,
    and a self-intersecting one contains an area nobody drew.
    """
    if geometry is None:
        raise ValueError(
            f"{path} {where} declares no geometry; a polygon with no area contains no pixel "
            "and would produce an all-null kelp series rather than an error"
        )
    if not isinstance(geometry, dict) or geometry.get("type") not in POLYGON_GEOMETRIES:
        kind = geometry.get("type") if isinstance(geometry, dict) else type(geometry).__name__
        raise ValueError(
            f"{path} {where} has geometry type {kind!r}; an analysis polygon is one of "
            f"{list(POLYGON_GEOMETRIES)} -- a point or a line has no interior for a pixel "
            "centroid to fall inside"
        )


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

    Vectorised rather than per-feature because that is how the aggregation will
    use the same frame, and because an invalid ring is a property of the built
    polygon rather than of the JSON it came from.
    """
    for polygon_id, geometry in zip(frame["polygon_id"], frame.geometry, strict=True):
        if geometry is None:
            raise ValueError(f"{path} polygon {polygon_id!r} built no geometry")

    empty = frame.loc[frame.geometry.is_empty, "polygon_id"].tolist()
    if empty:
        raise ValueError(
            f"{path} polygon(s) {empty} are empty; an empty polygon contains no pixel and "
            "would produce an all-null kelp series rather than an error"
        )

    invalid = frame.loc[~frame.geometry.is_valid, "polygon_id"].tolist()
    if invalid:
        raise ValueError(
            f"{path} polygon(s) {invalid} are not valid geometry -- a ring that crosses "
            "itself encloses an area nobody drew, and every pixel test against it is a "
            "guess. Fix the coordinates rather than letting the aggregation resolve it."
        )


__all__ = [
    "DEFAULT_POLYGONS_PATH",
    "POLYGON_GEOMETRIES",
    "POLYGON_PURPOSES",
    "WGS84",
    "Polygon",
    "Polygons",
    "load_polygons",
]
