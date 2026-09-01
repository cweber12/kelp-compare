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

#: What `predecessor_datasets[].covers_until` has to look like. A UTC instant,
#: spelled the way the providers spell `time_coverage_start` -- not a date,
#: because the boundary between two datasets of one station falls where one
#: record starts, which is a time of day and not a midnight. Fixed width and
#: fixed offset, which is what lets `Dataset` compare two of them as strings.
_ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


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
class Derivation:
    """What a *derived* site is derived from: one analysis polygon.

    A site record normally answers "where is the instrument". A handful of sites
    have no instrument at all: they carry a series computed from a gridded
    product over the area an analysis polygon covers, which is a number about
    that polygon rather than about a point (docs/03 "A site may be derived from
    a polygon"). The satellite leg of docs/04 s4.5 is the first of them.

    Recorded as a block on the site rather than inferred from the identifier,
    because `SST:LA-JOLLA` looking like `KELP:LA-JOLLA` is exactly the
    string-match between a station name and a polygon name that docs/03's
    integrity rules forbid. A derived site says which polygon it reduces, and a
    reader that needs to know joins on that.

    Only `polygon_id` is carried. *Which* reduction is applied is a property of
    the fetcher that computes it, not a registry fact -- there is one
    implementation, and a registry field naming it would imply an alternative a
    caller could select and get.
    """

    polygon_id: str


@dataclass(frozen=True)
class Dataset:
    """One of the datasets a station's record is spread across, and the window it owns.

    Most stations are one dataset: the provider serves the whole record under one
    identifier and `station_code` is the whole answer. A provider that has
    re-platformed splits the record instead -- City of San Diego RTOMS publishes
    Point Loma as a "real time" dataset back to 2021-11-04 and a separate
    "historic" one before it -- and then the site record has to name both, in
    order, with the instant where authority passes from one to the next
    (docs/03 "A station's record may span more than one dataset").

    **The window is not a convenience, it is the thing that keeps the record
    honest.** Two datasets of one mooring overlap, and where they overlap they
    can disagree about the *label* on a reading while agreeing exactly on the
    reading: RTOMS Point Loma reports the same deep sensor at 74 m in one
    dataset and 75 m in the other, same timestamp, same value to the millidegree
    (docs/02). `depth_m` is part of `OBSERVATION_KEY`, so landing both stores one
    reading twice under two permanent names, and nothing downstream can tell that
    from a mooring that really carried two sensors a metre apart. Giving each
    dataset a window and asking it for nothing outside makes that impossible
    rather than merely discouraged.

    `starts_at` and `ends_at` are half-open -- `[starts_at, ends_at)` -- so two
    consecutive datasets cannot both claim the boundary instant. `None` at either
    end means unbounded, which is what the first and last dataset carry.
    """

    station_code: str
    starts_at: str | None = None
    ends_at: str | None = None

    @property
    def is_current(self) -> bool:
        """Whether this is the dataset the provider is still adding to.

        The realtime feed belongs to it alone. A superseded dataset has a fixed
        end, so asking it for "the last 45 days" would either answer nothing or,
        worse, answer with the last 45 days it happens to hold and label them as
        current.
        """
        return self.ends_at is None

    def covers_year(self, year: int) -> bool:
        """Whether this dataset owns any part of one calendar year, UTC.

        Compared as strings, which is exact rather than lax: `_ISO_INSTANT` pins
        every boundary to one fixed-width UTC spelling, and those sort
        lexicographically in chronological order. A boundary that did not match
        never became a `Dataset` at all.
        """
        opens = f"{year:04d}-01-01T00:00:00Z"
        closes = f"{year + 1:04d}-01-01T00:00:00Z"
        return (self.ends_at is None or self.ends_at > opens) and (
            self.starts_at is None or self.starts_at < closes
        )


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

    `predecessors` records the datasets this station's record used to live in,
    where a provider has split it across more than one identifier. Read through
    `datasets`, never on its own: what a fetcher needs is the whole ordered
    chain with a window on each, and the current dataset is not in this tuple.

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
    predecessors: tuple[Dataset, ...] = ()
    archive: Archive | None = None
    derived_from: Derivation | None = None

    @property
    def datasets(self) -> tuple[Dataset, ...]:
        """Every dataset this station's record spans, oldest first, current last.

        One entry for almost every station, and callers are written against the
        tuple rather than against `station_code` so that stays true of the code
        when it stops being true of a station. The current dataset is derived
        here rather than stored, so it cannot drift from `station_code` -- which
        is still the identifier the rest of the project addresses this station
        by, and still what `--station` matches.

        Oldest first because that is the order the windows have to be ingested
        in: `storage._write_partition` lets the rows written last win a key they
        share, so the dataset the provider is still maintaining settles any tie
        with the one it superseded.

        Empty for a site with no `station_code` -- a project sensor, which has no
        dataset anywhere and is not something a fetcher can ask for.
        """
        if not self.station_code:
            return ()
        opens = self.predecessors[-1].ends_at if self.predecessors else None
        return (*self.predecessors, Dataset(self.station_code, starts_at=opens))

    @property
    def is_derived(self) -> bool:
        """Whether this site is a reduction over a polygon rather than an instrument.

        Asked rather than inferred from the namespace. A caller that branched on
        `site_id.startswith("SST:")` would be deciding what a site *is* from
        what it is *called*, which is the failure docs/03 keeps naming: an
        identifier is a label, and the registry is where a fact is declared.
        """
        return self.derived_from is not None

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

    A site that declares `predecessor_datasets` and no `station_code` is *not*
    skipped, and that is the point: it names datasets to fetch, so it is plainly
    meant to be a public station, and dropping it would answer a half-finished
    record by pretending the site is not there. `_station` refuses it by name
    instead.
    """
    return tuple(
        _station(site)
        for site in registry.sites
        if site.get("operator") == operator and _is_public(site)
    )


def find_deployment(registry: Registry, serial: str) -> Deployment | None:
    """The first deployment record matching `serial`, or None."""
    matches = find_deployments(registry, serial)
    return matches[0] if matches else None


def find_station(registry: Registry, site_id: str) -> Station | None:
    """The public station registered under one `site_id`, or None.

    The by-identifier counterpart to `find_stations`, which answers by operator.
    docs/04 s1's neighbor validation resolves `neighbor_refs` -- a list of
    `site_id`s -- against the stations they name, and asking for every station of
    an unknown operator to find one of them would be the wrong question.

    None for a `site_id` that is a project sensor, or that no site declares.
    Neither is an error here: a `neighbor_refs` entry naming a station nobody has
    registered yet is a gap the caller reports, not a reason to refuse the
    registry.
    """
    for site in registry.sites:
        if _site_id(site) == site_id and _is_public(site):
            return _station(site)
    return None


def neighbor_refs(registry: Registry, site_id: str) -> tuple[str, ...]:
    """The public stations declared for validating one site, in registry order.

    Ordered because docs/03 calls them ordered: the first entry is the reference
    a reader should reach for first, and `find_station` resolving them in turn is
    what makes "the nearest one" a registry fact rather than a distance this code
    recomputes.

    **Not a field on `Deployment`, and for the same reason `lat`/`lon` are not.**
    Which stations validate this place is a property of the place, not of the
    instrument that happened to be in it -- a second deployment of the same
    logger at the same site validates against the same references, and a record
    that carried its own copy could disagree with its sibling. Reading it from
    the site record means there is one answer and nowhere to put a second.

    Empty means undeclared, never "validate against nothing": a site whose
    references nobody has recorded produces no validation rows and says so,
    rather than silently producing a table that looks complete.
    """
    site = registry.site(site_id)
    if not site:
        return ()
    return tuple(str(ref) for ref in site.get("neighbor_refs") or ())


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
        predecessors=_predecessors(site),
        archive=_archive(site),
        derived_from=_derivation(site),
    )


def _is_public(site: dict) -> bool:
    """Whether this record is something a fetcher could be pointed at.

    A `station_code`, or the `predecessor_datasets` block that only a public
    station has. The second is not redundant: a record naming datasets and no
    current identifier is a public station someone stopped editing halfway, and
    it has to reach `_station` to be refused rather than be filtered out of the
    run as though it were a project sensor.
    """
    return bool(site.get("station_code") or site.get("predecessor_datasets"))


def _predecessors(site: dict) -> tuple[Dataset, ...]:
    """The superseded datasets on a station record, oldest first (docs/03).

    Empty for almost every station, which is what an absent block means: the
    provider serves the whole record under one identifier.

    Every rule here refuses rather than repairs, and each of them is a way a
    hand-edited registry could silently halve or double a record:

    - **A boundary is mandatory.** A predecessor with no `covers_until` is a
      second dataset asked for every window its successor is also asked for,
      which is the overlap this block exists to bound (see `Dataset`).
    - **Boundaries increase.** They are read as a chain -- each dataset starts
      where the one before it ended -- so a list out of order would hand a
      dataset a window that runs backwards and fetch nothing, silently.
    - **No identifier twice.** The same dataset named twice would be asked for
      two adjacent windows and land both, which is not wrong so much as
      unreadable: two manifest entries, two landings, one dataset.
    - **A predecessor needs a successor.** Without `station_code` there is no
      current dataset for the chain to end at, and the last boundary would name
      an instant after which nothing is declared to hold the record.
    """
    block = site.get("predecessor_datasets")
    if block is None:
        return ()

    site_id = _site_id(site) or "<unnamed site>"
    if not isinstance(block, list) or not block:
        raise ValueError(
            f"`predecessor_datasets` on {site_id} is {block!r}; declare a non-empty list of "
            "datasets this station's record continues from, or omit it, which is how the "
            "registry says the provider serves the whole record under one identifier"
        )
    if not site.get("station_code"):
        raise ValueError(
            f"{site_id} declares `predecessor_datasets` but no `station_code`; a superseded "
            "dataset is only superseded by a current one, and there is nothing here to hold "
            "the record after the last boundary"
        )

    datasets: list[Dataset] = []
    seen = {str(site.get("station_code"))}
    for entry in block:
        if not isinstance(entry, dict) or not entry:
            raise ValueError(
                f"`predecessor_datasets` on {site_id} carries {entry!r}; each entry is an "
                "object with a `station_code` and the `covers_until` instant its record ends at"
            )

        code = entry.get("station_code")
        if not isinstance(code, str) or not code:
            raise ValueError(
                f"`predecessor_datasets[].station_code` on {site_id} is {code!r}; it is the "
                "identifier the provider knows the superseded dataset by, and there is nothing "
                "to fetch without it"
            )
        if code in seen:
            raise ValueError(
                f"{site_id} names {code!r} twice; one dataset holds one window, so a repeated "
                "identifier would be fetched and landed once per window it appears in"
            )
        seen.add(code)

        until = entry.get("covers_until")
        if not isinstance(until, str) or not _ISO_INSTANT.fullmatch(until):
            raise ValueError(
                f"`covers_until` on {site_id}'s {code!r} is {until!r}; it must be a UTC instant "
                "spelled YYYY-MM-DDTHH:MM:SSZ. It is where this dataset stops being the record "
                "and cannot be omitted -- an unbounded predecessor is fetched over its "
                "successor's window too, and the two can disagree about a reading's depth "
                "while agreeing about the reading (docs/02)"
            )
        opens = datasets[-1].ends_at if datasets else None
        if opens is not None and until <= opens:
            raise ValueError(
                f"{site_id} declares {code!r} covering until {until}, which is not after the "
                f"{opens} the entry before it ends at; `predecessor_datasets` is read as a "
                "chain, oldest first, and each dataset starts where the last one ended"
            )
        datasets.append(Dataset(code, starts_at=opens, ends_at=until))

    return tuple(datasets)


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


def _derivation(site: dict) -> Derivation | None:
    """The polygon a derived site reduces, or None for an ordinary station.

    None rather than a raise for the same reason `_archive` returns one: almost
    every site is an instrument somewhere and has nothing to derive from. It is
    the fetcher for a derived source that refuses when the block is absent,
    which is the moment a polygon would otherwise be guessed from a name.

    `polygon_id` is validated because everything downstream hangs off it: it is
    what the geometry is looked up by, and a blank or non-string one would reach
    the polygon registry as a lookup that matches nothing and reads there as "no
    such polygon" -- a registry typo wearing the costume of a missing outline.
    Unknown keys are refused rather than ignored, as `polygons._reject_unknown`
    refuses them, because a misspelt key in a block this small is silently the
    whole block being absent.
    """
    block = site.get("derived_from")
    if block is None:
        return None

    site_id = _site_id(site) or "<unnamed site>"
    if not isinstance(block, dict) or not block:
        raise ValueError(
            f"`derived_from` on {site_id} is {block!r}; declare a block naming a `polygon_id` "
            "or omit it, which is how the registry says a site is an instrument somewhere"
        )

    unknown = sorted(set(block) - {"polygon_id"})
    if unknown:
        raise ValueError(
            f"`derived_from` on {site_id} carries {unknown}, which this registry does not "
            "define; the block holds `polygon_id` and nothing else"
        )

    polygon_id = block.get("polygon_id")
    if not isinstance(polygon_id, str) or not polygon_id.strip():
        raise ValueError(
            f"`derived_from.polygon_id` on {site_id} is {polygon_id!r}; it must name a polygon "
            "in polygons.geojson. It is what the geometry this series is reduced over is "
            "looked up by, so it cannot be blank or inferred from the site_id"
        )

    return Derivation(polygon_id=polygon_id.strip())


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
