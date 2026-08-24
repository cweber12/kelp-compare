"""Reads `data/registry/parameters.json`: the controlled vocabulary (docs/03).

Separate from `registry.py` because it answers a different question. `sites.json`
records which instrument was where; `parameters.json` records what a measurement
*means* -- its canonical SI unit and the bounds a QARTOD gross-range test uses
(docs/04 s1, ADR-004). The normalizer needs the first; QC will need the second;
neither should have to load the other's file.

Adding a sensor type is an entry here plus a registry deployment, never a schema
change (docs/03 "Parameter vocabulary").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PARAMETERS_PATH = Path("data/registry/parameters.json")


@dataclass(frozen=True)
class Parameter:
    """One controlled parameter name and what the project stores it as."""

    name: str
    unit: str
    valid_range: tuple[float, float] | None = None
    datum: str | None = None


@dataclass(frozen=True)
class Parameters:
    """The parsed vocabulary, plus the path it came from (for error messages)."""

    path: Path
    entries: dict[str, Parameter]

    def __contains__(self, name: object) -> bool:
        return name in self.entries

    def __getitem__(self, name: str) -> Parameter:
        try:
            return self.entries[name]
        except KeyError:
            raise KeyError(
                f"{name!r} is not a controlled parameter in {self.path}; "
                f"known: {', '.join(sorted(self.entries))}"
            ) from None

    def get(self, name: str) -> Parameter | None:
        return self.entries.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.entries))


def load_parameters(path: Path | str | None = None) -> Parameters:
    """Load the vocabulary. Defaults to `data/registry/parameters.json` under cwd."""
    resolved = Path(path) if path is not None else DEFAULT_PARAMETERS_PATH
    with resolved.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    entries = {
        name: Parameter(
            name=name,
            unit=str(record["unit"]),
            valid_range=_range(record.get("valid_range")),
            datum=record.get("datum"),
        )
        for name, record in payload.get("parameters", {}).items()
    }
    return Parameters(path=resolved, entries=entries)


def _range(value) -> tuple[float, float] | None:
    if not value or len(value) != 2:
        return None
    return (float(value[0]), float(value[1]))
