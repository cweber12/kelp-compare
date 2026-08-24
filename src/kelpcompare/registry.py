"""Reads `data/registry/sites.json`. Knows nothing about any vendor file format.

The registry is the project's record of which instrument was where, when, and in
what timezone (docs/03 "Site registry"). Two callers need it and neither should
own it: the adapters, for the docs/06 s5 check-4 registry gate, and the
normalizer, which needs `tz` and `window_local` to convert to UTC and flag the
deployment window, and `series_map` to name the parameter each series carries.

Note what `Deployment` deliberately does NOT carry: `lat`/`lon`. Position lives
on the site record and is nullable by design -- serial 22506632's position is
unverified pending a GPS fix (see its `notes` in sites.json). Leaving the field
off this dataclass means ingest code has nowhere to put a coordinate, so it
cannot come to depend on one or quietly invent one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
