"""Reads `data/registry/sites.json`. Knows nothing about any vendor file format.

The registry is the project's record of which instrument was where, when, and in
what timezone (docs/03 "Site registry"). Two callers need it and neither should
own it: the adapters, for the docs/06 s5 check-4 registry gate, and the
normalizer, which needs `tz` and `window_local` to convert to UTC and flag the
deployment window, and `series_map` to name the parameter each series carries.

Note what `Deployment` deliberately does NOT carry: `lat`/`lon`. Position lives
on the site record and is nullable by design: a logger can be in the water and
recording before anyone has surveyed where it is, and the docs/06 s5 check-4 gate
lets that file ingest. Leaving the field off this dataclass means ingest code has
nowhere to put a coordinate, so it cannot come to depend on one or quietly invent
one -- and it is why two loggers at different positions are two site records
rather than two deployments of one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REGISTRY_PATH = Path("data/registry/sites.json")

#: What `archive.archived` has to look like. It is a directory name in `raw/`
#: and the value a file's own header is checked against, so it is pinned to one
#: spelling rather than parsed leniently.
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Deployment:
    """One `deployments[]` entry from a site record, plus its owning `site_id`."""

    site_id: str
    serial: str
    instrument: str | None = None
    deployment_number: int | None = None
    tz: str | None = None
    window_local: tuple[str, str] | None = None
    series_map: dict[str, str] | None = None
    depth_m: float | None = None
    notes: str | None = None

    @property
    def is_complete(self) -> bool:
        """True when this record satisfies the docs/06 s5 check-4 requirements.

        A serial match alone is not enough: the normalizer cannot place the data
        in time without a timezone and an in-water window, and it cannot name the
        parameter a series carries without a series map.
        """
        return bool(self.tz) and self.window_local is not None and bool(self.series_map)

    def parameter_for(self, series_name: str) -> str | None:
        """The controlled parameter this deployment declares for a series.

        None means unmapped, which the normalizer reports and skips. Guessing a
        parameter from a unit is not possible in general -- degC is equally
        `sea_water_temperature` and `air_temperature` -- so an unmapped series is
        a registry gap for a human, never an inference.
        """
        return (self.series_map or {}).get(series_name)


@dataclass(frozen=True)
class Archive:
    """Which hand-downloaded snapshot a site's landings came from (docs/03).

    A pulled station needs no such record: the identifier *is* the version,
    because the provider serves whatever is current and a re-run gets the same
    thing. A station that can only be downloaded by hand has no such guarantee
    -- what arrives is one cumulative snapshot of the whole record, and the next
    one may revise it -- so the snapshot is pinned here, and an ingest without a
    pin is refused rather than traced to "whatever was on the site that day".

    This is the Kelp Watch revision pin (`polygons.KelpWatch`) applied to a site
    rather than a polygon, and it does one thing that one cannot: the SIO Shore
    Stations file declares its own archive date in its header, so the pin is
    *checked* at ingest and a file from another snapshot is quarantined.

    `citation` is pinned beside the date rather than left to a document because
    it is a property of the archive, not of the program: the funding award in
    the SIO citation text differs between snapshots.
    """

    archived: str
    source_file: str | None = None
    doi: str | None = None
    citation: str | None = None

    @property
    def label(self) -> str:
        """The landing directory, so two archives cannot interleave in `raw/`.

        The date itself rather than a prefixed form of it. `polygons.KelpWatch`
        needs `ver23` because a bare `23` names nothing; an ISO date already
        says what it is.
        """
        return self.archived


@dataclass(frozen=True)
class Station:
    """A public-station site record: what a fetcher needs in order to ask for it.

    The counterpart to `Deployment`. A project sensor is described by when it was
    where; a public station is described by the identifier its provider knows it
    by, and by the geometry of its sensors -- which is not decoration. docs/02
    requires the water-temperature depth to be recorded because comparing a
    3.4 m shore-station intake against a logger at another depth is a real
    analysis error, and `depth_m` on every observation row is where that is
    prevented structurally rather than remembered.

    `same_platform_as` records that two site records describe one instrument
    package. NDBC redistributes NOS observations, so `NDBC:LJAC1` and
    `COOPS:9410230` are the same hardware under two identifiers -- and the
    docs/04 neighbor validation must never count them as two independent
    references for the same sensor.

    `measured_parameters` records what the station carries an instrument for.
    It is not derivable from `sensor_depths_m`: a met parameter has no depth and
    is measured anyway, so an absence there means "no depth published", never
    "no sensor" (docs/03).

    **`lat`/`lon` are carried here although `Deployment` deliberately refuses
    them**, and the asymmetry is the same fact twice. A project logger can be in
    the water and recording before anyone has surveyed it, so ingest code must
    have nowhere to put a coordinate; a public station's position is something
    its operator published, and for a hand-downloaded archive it is how a
    dropped file is matched to a site at all -- the SIO Shore Stations file
    declares its own position, and the match is against this. Still optional,
    because a station can be registered before anyone has copied its position
    down, and a caller that needs one has to cope with `None` rather than
    assume.
    """

    site_id: str
    station_code: str
    operator: str
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    sensor_depths_m: dict[str, float] = field(default_factory=dict)
    depth_set_m: dict[str, tuple[float, ...]] = field(default_factory=dict)
    measured_parameters: tuple[str, ...] = ()
    same_platform_as: tuple[str, ...] = ()
    archive: Archive | None = None

    def depth_for(self, parameter: str) -> float | None:
        """The depth the registry supplies for one parameter, or None.

        None is the right answer for a met parameter -- docs/03 says `depth_m` is
        null for those -- and equally the right answer for a water parameter
        whose depth the provider has not published. Neither is guessed.

        None is also the answer for a parameter measured on a moored string,
        whose depth is on the payload rather than here (`describes_own_depth`).
        That is deliberate: this method's contract is "what the fetcher should
        write", and for a self-describing source the answer is "not mine to
        say". A fetcher falling back to this value would write one depth for
        every sensor on the string and collapse them all into one series.
        """
        return self.sensor_depths_m.get(parameter)

    def describes_own_depth(self, parameter: str) -> bool:
        """Whether the payload carries this parameter's depth rather than the registry.

        True when the registry declared a *set* of depths for it. A moored string
        measures one parameter at many depths, so there is no single value for
        `sensor_depths_m` to hold and the depth has to be read per row. What the
        registry can still do is record which depths anyone has actually looked
        at, which is what `declared_depths` is for.
        """
        return parameter in self.depth_set_m

    def declared_depths(self, parameter: str) -> tuple[float, ...]:
        """Every depth recorded for one parameter, however it was declared.

        A scalar declaration answers with a one-tuple, a set declaration with the
        set, and an undeclared parameter with `()`. A caller checking a payload
        depth against the registry wants those three to look the same, so the two
        declaration forms are flattened here rather than at each call site.

        Empty means undeclared, never "measured nowhere" -- the same distinction
        `declares_parameters` draws, and for the same reason.
        """
        if parameter in self.depth_set_m:
            return self.depth_set_m[parameter]
        depth = self.sensor_depths_m.get(parameter)
        return () if depth is None else (depth,)

    @property
    def declares_parameters(self) -> bool:
        """Whether anyone has recorded what this station carries.

        Empty means undeclared, not "measures nothing": a station that measured
        nothing would not be in the registry as something to fetch. Callers need
        the distinction, because "we have not checked" and "it does not have one"
        must not produce the same silence.
        """
        return bool(self.measured_parameters)

    def measures(self, parameter: str) -> bool:
        """Whether this station carries an instrument for one parameter.

        True for everything while the station is undeclared. Fetchers store what
        they recognise in that case and report the gap -- refusing instead would
        turn an unrecorded fact into missing data, which is the worse of the two
        (https://github.com/cweber12/kelp-compare/issues/21).
        """
        return parameter in self.measured_parameters if self.declares_parameters else True


@dataclass(frozen=True)
class Registry:
    """The parsed contents of a sites.json, plus the path it came from."""

    path: Path
    sites: tuple[dict, ...]

    @property
    def deployments(self) -> tuple[Deployment, ...]:
        return tuple(
            _deployment(site, record)
            for site in self.sites
            for record in site.get("deployments", [])
        )

    def site(self, site_id: str) -> dict | None:
        for site in self.sites:
            if site.get("site_id") == site_id:
                return site
        return None


def load_registry(path: Path | str | None = None) -> Registry:
    """Load a site registry. Defaults to `data/registry/sites.json` under the cwd."""
    resolved = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    with resolved.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return Registry(path=resolved, sites=tuple(payload.get("sites", [])))


def find_deployments(registry: Registry, serial: str) -> tuple[Deployment, ...]:
    """Every deployment record matching `serial`.

    Plural because one logger is redeployed: the reviewed TidbiT is on its third
    deployment, and each is a separate record with its own window.
    """
    wanted = _normalize_serial(serial)
    return tuple(d for d in registry.deployments if _normalize_serial(d.serial) == wanted)


def find_stations(registry: Registry, operator: str) -> tuple[Station, ...]:
    """Every public station this registry declares for one operator.

    A site with no `station_code` is skipped: without the identifier its provider
    knows it by, there is nothing a fetcher could ask for. Site order is
    preserved, so a run over "every NDBC station" is reproducible.
    """
    return tuple(
        _station(site)
        for site in registry.sites
        if site.get("operator") == operator and site.get("station_code")
    )


def find_deployment(registry: Registry, serial: str) -> Deployment | None:
    """The first deployment record matching `serial`, or None."""
    matches = find_deployments(registry, serial)
    return matches[0] if matches else None


def _normalize_serial(serial: object) -> str:
    """Serials are strings, but a hand-edited registry may hold a JSON number."""
    if serial is None:
        return ""
    if isinstance(serial, float) and serial.is_integer():
        return str(int(serial))
    return str(serial).strip()


def _site_id(site: dict) -> str:
    """The site's identifier as a string, whatever the JSON holds.

    The same reason as `_depth`: `site_id` is the other `OBSERVATION_KEY`
    component read from this file rather than from the instrument, and the
    partition write dedupes before it casts dtypes, so a JSON number keys
    differently from the `"1234"` already on disk and the reading survives twice.
    docs/03 names the two fields together for that reason.

    An explicit `null` becomes `""` -- the same "no site declared" the absent key
    produces -- rather than the string `"None"` that a bare `str()` would invent.
    """
    value = site.get("site_id")
    return "" if value is None else str(value)


def _depth(value: object, *, site_id: str, serial: str) -> float | None:
    """Depths are floats, but a hand-edited registry may hold a JSON string.

    Coerced rather than passed through because `depth_m` is one of the four
    `storage.OBSERVATION_KEY` components, and `_dedupe` runs before the write
    casts dtypes. A depth that arrives as `"8.23"` does not compare equal to the
    `8.23` already in the partition, so the same reading survives twice and
    nothing raises -- the silent split docs/03 "Partition files and idempotence"
    describes for a depth *change*, reached here by an edit that changed nothing.

    Refusing a non-number is the other half. Left alone it still fails, but at
    the `astype` inside the partition write: after the fetch, the parse and the
    QC are done, and naming storage rather than the registry field at fault.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"depth_m on {site_id or '<unnamed site>'} deployment of serial "
            f"{serial or '<no serial>'} is {value!r}, which is not a number; it is part "
            "of the storage dedupe key and must be a float or null"
        ) from None


def _sensor_depths(site: dict) -> tuple[dict[str, float], dict[str, tuple[float, ...]]]:
    """Split `sensor_depths_m` into its scalar and its set declarations (docs/03).

    A number means the registry supplies the depth and the fetcher writes it. A
    list means the source is self-describing -- a moored string measuring one
    parameter at many depths -- and the registry is instead recording which
    depths have been seen, so a new one is caught rather than landed silently.

    Both arrive as floats, for the reason `_depth` gives: `depth_m` is part of
    `storage.OBSERVATION_KEY`, and a depth carried as a string does not compare
    equal to the float already in the partition, so the same reading survives
    twice and nothing raises.

    An empty list is refused rather than read as "no depths". It would flow on as
    a station that declares a set and matches nothing, which reads at the fetcher
    exactly like a source that changed every depth at once -- a confusing way to
    say something the absent key already says plainly.
    """
    declared = site.get("sensor_depths_m") or {}
    site_id = _site_id(site) or "<unnamed site>"
    scalars: dict[str, float] = {}
    sets: dict[str, tuple[float, ...]] = {}
    for key, value in declared.items():
        parameter = str(key)
        if isinstance(value, (list, tuple)):
            if not value:
                raise ValueError(
                    f"sensor_depths_m[{parameter!r}] on {site_id} is an empty list; omit the "
                    "parameter instead, which is how the registry says a depth is undeclared"
                )
            sets[parameter] = tuple(
                _sensor_depth(item, site_id=site_id, parameter=parameter) for item in value
            )
        else:
            scalars[parameter] = _sensor_depth(value, site_id=site_id, parameter=parameter)
    return scalars, sets


def _sensor_depth(value: object, *, site_id: str, parameter: str) -> float:
    """One `sensor_depths_m` entry as a float, or a refusal naming the registry.

    The station-side counterpart to `_depth`, refused for the same reason: a
    depth reaching storage as a string splits a series in two without raising
    (docs/03 "Partition files and idempotence"). Left alone it would still fail,
    but inside a partition write that can only name storage as the culprit.
    """
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(
            f"sensor_depths_m[{parameter!r}] on {site_id} is {value!r}, which is not a number; "
            "it is part of the storage dedupe key and must be a float"
        ) from None


def _station(site: dict) -> Station:
    platform = site.get("same_platform_as") or ()
    measured = site.get("measured_parameters") or ()
    scalars, sets = _sensor_depths(site)
    return Station(
        site_id=_site_id(site),
        station_code=str(site.get("station_code", "")),
        operator=str(site.get("operator", "")),
        name=site.get("name"),
        lat=_coordinate(site.get("lat"), site, "lat"),
        lon=_coordinate(site.get("lon"), site, "lon"),
        sensor_depths_m=scalars,
        depth_set_m=sets,
        measured_parameters=tuple(str(p) for p in measured),
        same_platform_as=tuple(str(s) for s in platform),
        archive=_archive(site),
    )


def _archive(site: dict) -> Archive | None:
    """The pinned snapshot on a hand-downloaded station's record, or None.

    None rather than a raise, because most sites legitimately have no archive
    block -- a pulled station has nothing to pin. It is the ingest of a
    file-drop station that refuses when the pin is absent, which is the moment
    an archive date would otherwise be invented (the same posture
    `polygons._kelp_watch` takes).

    `archived` is validated because it is load-bearing twice over: it is the
    landing directory name, and it is what the file's own header is checked
    against. A malformed one would make a directory nobody can find again and a
    comparison nothing can match. The rest of the block is provenance text and
    is taken as written.
    """
    block = site.get("archive")
    if block is None:
        return None

    site_id = _site_id(site) or "<unnamed site>"
    if not isinstance(block, dict) or not block:
        raise ValueError(
            f"`archive` on {site_id} is {block!r}; declare a block with an `archived` date "
            "or omit it, which is how the registry says a site is not a hand-downloaded one"
        )

    archived = block.get("archived")
    if not isinstance(archived, str) or not _ISO_DATE.fullmatch(archived):
        raise ValueError(
            f"`archive.archived` on {site_id} is {archived!r}; it must be a YYYY-MM-DD date. "
            "It names the raw landing directory and is checked against the archive date the "
            "file declares in its own header, so it cannot be free text"
        )

    return Archive(
        archived=archived,
        source_file=_optional_text(block.get("source_file")),
        doi=_optional_text(block.get("doi")),
        citation=_optional_text(block.get("citation")),
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _coordinate(value: object, site: dict, field_name: str) -> float | None:
    """One WGS84 coordinate as a float, or None if the site declares none.

    Refused rather than coerced-or-ignored when it is present and unreadable.
    Position is a reviewed fact in this project -- docs/02 leaves a whole RTOMS
    window out because its provider gave three different answers for it -- and
    a coordinate silently read as absent would turn a station that *is* placed
    into one that matches nothing.
    """
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(
            f"{field_name} on {_site_id(site) or '<unnamed site>'} is {value!r}, which is "
            "not a number; a position is either declared or absent, never unreadable"
        ) from None


def _deployment(site: dict, record: dict) -> Deployment:
    window = record.get("window_local")
    series_map = record.get("series_map")
    site_id = _site_id(site)
    serial = _normalize_serial(record.get("serial"))
    return Deployment(
        site_id=site_id,
        serial=serial,
        instrument=record.get("instrument"),
        deployment_number=record.get("deployment_number"),
        tz=record.get("tz"),
        window_local=(str(window[0]), str(window[1])) if window and len(window) == 2 else None,
        series_map={str(k): str(v) for k, v in series_map.items()} if series_map else None,
        depth_m=_depth(record.get("depth_m"), site_id=site_id, serial=serial),
        notes=record.get("notes"),
    )
