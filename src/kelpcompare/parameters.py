"""Reads `data/registry/parameters.json`: the controlled vocabulary (docs/03).

Separate from `registry.py` because it answers a different question. `sites.json`
records which instrument was where; `parameters.json` records what a measurement
*means* -- its canonical SI unit, the bounds a QARTOD gross-range test uses, and
the thresholds the other QARTOD tests need (docs/04 s1, ADR-004). The normalizer
needs the first; `kelpcompare qc` needs the rest; neither should have to load the
other's file.

ADR-004 puts threshold tuning here rather than in code, which makes this module
the last place a mis-declared threshold can be caught. It refuses rather than
ignores -- an unknown key, an empty block, a test the qc stage does not run --
because a silently disabled test is indistinguishable, in the stored flags, from
a test that ran and passed.

Adding a sensor type is an entry here plus a registry deployment, never a schema
change (docs/03 "Parameter vocabulary").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PARAMETERS_PATH = Path("data/registry/parameters.json")

_SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class SpikeThresholds:
    """QARTOD spike test bounds, in the parameter's own canonical unit."""

    suspect: float | None = None
    fail: float | None = None


@dataclass(frozen=True)
class RateOfChangeThresholds:
    """QARTOD rate-of-change bounds, declared per hour and used per second.

    The registry speaks per hour because that is the rate an operator can reason
    about; `ioos_qc` takes a rate per second. Converting here means the two units
    never meet at the call site, where a factor of 3600 in the wrong direction
    would flag nothing at all (docs/03).
    """

    suspect_per_hour: float | None = None
    fail_per_hour: float | None = None

    @property
    def suspect_per_second(self) -> float | None:
        return _per_second(self.suspect_per_hour)

    @property
    def fail_per_second(self) -> float | None:
        return _per_second(self.fail_per_hour)


@dataclass(frozen=True)
class GrossRangeThresholds:
    """The span inside `valid_range` that is merely suspect rather than a fail.

    Only the suspect span lives here: the fail span *is* `valid_range`, so the
    hard bounds are declared once (docs/03).
    """

    suspect_span: tuple[float, float]


@dataclass(frozen=True)
class QcThresholds:
    """One parameter's `qc` block. Every member is optional and None means
    "that test does not run for this parameter" -- never "use a default"."""

    spike: SpikeThresholds | None = None
    rate_of_change: RateOfChangeThresholds | None = None
    gross_range: GrossRangeThresholds | None = None


@dataclass(frozen=True)
class Parameter:
    """One controlled parameter name and what the project stores it as."""

    name: str
    unit: str
    valid_range: tuple[float, float] | None = None
    datum: str | None = None
    qc: QcThresholds = field(default_factory=QcThresholds)


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
            qc=_thresholds(record.get("qc"), name=name, path=resolved),
        )
        for name, record in payload.get("parameters", {}).items()
    }
    return Parameters(path=resolved, entries=entries)


def _range(value) -> tuple[float, float] | None:
    if not value or len(value) != 2:
        return None
    return (float(value[0]), float(value[1]))


def _thresholds(block, *, name: str, path: Path) -> QcThresholds:
    """Parse one `qc` block, refusing anything it does not recognize.

    The tests named here are the ones `kelpcompare qc` actually runs. Declaring a
    threshold for a deferred test (`flat_line`, `climatology` -- docs/04 s1)
    raises rather than sitting unread in the registry looking like coverage.
    """
    if not block:
        return QcThresholds()

    unknown = set(block) - {"spike", "rate_of_change", "gross_range"}
    if unknown:
        raise ValueError(
            f"{name!r} in {path} declares thresholds for {sorted(unknown)}, which "
            "kelpcompare qc does not run; see docs/04 s1 for the implemented tests"
        )

    return QcThresholds(
        spike=_spike(block.get("spike"), name=name, path=path),
        rate_of_change=_rate_of_change(block.get("rate_of_change"), name=name, path=path),
        gross_range=_gross_range(block.get("gross_range"), name=name, path=path),
    )


def _spike(block, *, name: str, path: Path) -> SpikeThresholds | None:
    if block is None:
        return None
    _check_keys(block, {"suspect", "fail"}, test="spike", name=name, path=path)
    return SpikeThresholds(suspect=_number(block.get("suspect")), fail=_number(block.get("fail")))


def _rate_of_change(block, *, name: str, path: Path) -> RateOfChangeThresholds | None:
    if block is None:
        return None
    _check_keys(
        block, {"suspect_per_hour", "fail_per_hour"}, test="rate_of_change", name=name, path=path
    )
    return RateOfChangeThresholds(
        suspect_per_hour=_number(block.get("suspect_per_hour")),
        fail_per_hour=_number(block.get("fail_per_hour")),
    )


def _gross_range(block, *, name: str, path: Path) -> GrossRangeThresholds | None:
    if block is None:
        return None
    _check_keys(block, {"suspect_span"}, test="gross_range", name=name, path=path)
    span = _range(block.get("suspect_span"))
    if span is None:
        raise ValueError(
            f"{name!r} in {path} declares a gross_range.suspect_span that is not two "
            f"bounds: {block.get('suspect_span')!r}"
        )
    return GrossRangeThresholds(suspect_span=span)


def _check_keys(block: dict, allowed: set[str], *, test: str, name: str, path: Path) -> None:
    """Every key recognized, and at least one of them present.

    Both halves matter. An unrecognized key is usually a typo, which would leave
    the test running on a threshold nobody declared; an empty block is a
    half-finished edit, which would leave it not running at all.
    """
    unknown = set(block) - allowed
    if unknown:
        raise ValueError(
            f"{name!r} in {path} declares unknown {test} threshold(s) {sorted(unknown)}; "
            f"known: {sorted(allowed)}"
        )
    if not block:
        raise ValueError(
            f"{name!r} in {path} has an empty {test} block; declare at least one of "
            f"{sorted(allowed)}, or remove the block to leave the test unrun"
        )


def _number(value) -> float | None:
    return None if value is None else float(value)


def _per_second(per_hour: float | None) -> float | None:
    return None if per_hour is None else per_hour / _SECONDS_PER_HOUR
