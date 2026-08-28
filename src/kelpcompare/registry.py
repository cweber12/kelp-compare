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
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REGISTRY_PATH = Path("data/registry/sites.json")


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
    """

    site_id: str
    station_code: str
    operator: str
    name: str | None = None
    sensor_depths_m: dict[str, float] = field(default_factory=dict)
    measured_parameters: tuple[str, ...] = ()
    same_platform_as: tuple[str, ...] = ()

    def depth_for(self, parameter: str) -> float | None:
        """The declared depth for one parameter, or None.

        None is the right answer for a met parameter -- docs/03 says `depth_m` is
        null for those -- and equally the right answer for a water parameter
        whose depth the provider has not published. Neither is guessed.
        """
        return self.sensor_depths_m.get(parameter)

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


def _station(site: dict) -> Station:
    depths = site.get("sensor_depths_m") or {}
    platform = site.get("same_platform_as") or ()
    measured = site.get("measured_parameters") or ()
    return Station(
        site_id=site.get("site_id", ""),
        station_code=str(site.get("station_code", "")),
        operator=str(site.get("operator", "")),
        name=site.get("name"),
        sensor_depths_m={str(k): float(v) for k, v in depths.items()},
        measured_parameters=tuple(str(p) for p in measured),
        same_platform_as=tuple(str(s) for s in platform),
    )


def _deployment(site: dict, record: dict) -> Deployment:
    window = record.get("window_local")
    series_map = record.get("series_map")
    return Deployment(
        site_id=site.get("site_id", ""),
        serial=_normalize_serial(record.get("serial")),
        instrument=record.get("instrument"),
        deployment_number=record.get("deployment_number"),
        tz=record.get("tz"),
        window_local=(str(window[0]), str(window[1])) if window and len(window) == 2 else None,
        series_map={str(k): str(v) for k, v in series_map.items()} if series_map else None,
        depth_m=record.get("depth_m"),
        notes=record.get("notes"),
    )
