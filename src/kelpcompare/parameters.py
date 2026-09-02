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
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PARAMETERS_PATH = Path("data/registry/parameters.json")

#: The tests `kelpcompare qc` runs, and therefore the only names this registry
#: accepts -- as a threshold block, or as a `by_source` removal. Declaring a
#: threshold for a deferred test (`flat_line`, `climatology` -- docs/04 s1)
#: raises rather than sitting unread in the registry looking like coverage.
IMPLEMENTED_TESTS = ("gross_range", "spike", "rate_of_change")

#: The key inside a `qc` block that holds the per-source exceptions (ADR-008).
BY_SOURCE = "by_source"

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
    "that test does not run for this parameter" -- never "use a default".

    `by_source` is the ADR-008 exception: the tests that do not run for one
    source's series of this parameter, keyed by source name. It removes tests
    and can never supply different numbers for them, so a reader of this block
    still learns every threshold the project applies to this parameter from
    the members above -- the map only ever subtracts.
    """

    spike: SpikeThresholds | None = None
    rate_of_change: RateOfChangeThresholds | None = None
    gross_range: GrossRangeThresholds | None = None
    by_source: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def suppressed_for(self, source: str | None) -> frozenset[str]:
        """Which tests do not run for `source`. Empty for a source with no entry."""
        return self.by_source.get(source, frozenset())


@dataclass(frozen=True)
class Parameter:
    """One controlled parameter name and what the project stores it as."""

    name: str
    unit: str
    valid_range: tuple[float, float] | None = None
    datum: str | None = None
    qc: QcThresholds = field(default_factory=QcThresholds)


@dataclass(frozen=True)
class SourceException:
    """One `by_source` entry, flattened for a caller that wants them all.

    The qc stage asks the other question -- "which tests are off for the source
    in front of me" -- and `QcThresholds.suppressed_for` answers that. This is
    for the run that has to notice an exception which matched nothing.
    """

    parameter: str
    source: str
    tests: tuple[str, ...]


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

    @property
    def source_exceptions(self) -> tuple[SourceException, ...]:
        """Every declared per-source removal, in a stable order (ADR-008)."""
        return tuple(
            SourceException(parameter=name, source=source, tests=tuple(sorted(tests)))
            for name in sorted(self.entries)
            for source, tests in sorted(self.entries[name].qc.by_source.items())
        )


def load_parameters(
    path: Path | str | None = None, *, sources: Collection[str] | None = None
) -> Parameters:
    """Load the vocabulary. Defaults to `data/registry/parameters.json` under cwd.

    `sources` is the caller's known-source vocabulary, and the only thing a
    `qc.by_source` exception can be checked against (ADR-008). It is passed in
    rather than imported on purpose: this module answers what a measurement
    means, and where the project's sources are enumerated is not its business.

    Omitting it is fine for a caller that only wants units and ranges. A file
    that declares exceptions is refused rather than loaded unchecked, because
    an unverified source name is exactly the typo that leaves an exception
    silently unapplied.
    """
    resolved = Path(path) if path is not None else DEFAULT_PARAMETERS_PATH
    with resolved.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    entries = {
        name: Parameter(
            name=name,
            unit=str(record["unit"]),
            valid_range=_range(record.get("valid_range")),
            datum=record.get("datum"),
            qc=_thresholds(
                record.get("qc"),
                name=name,
                valid_range=_range(record.get("valid_range")),
                sources=sources,
                path=resolved,
            ),
        )
        for name, record in payload.get("parameters", {}).items()
    }
    return Parameters(path=resolved, entries=entries)


def _range(value) -> tuple[float, float] | None:
    if not value or len(value) != 2:
        return None
    return (float(value[0]), float(value[1]))


def _thresholds(
    block,
    *,
    name: str,
    valid_range: tuple[float, float] | None,
    sources: Collection[str] | None,
    path: Path,
) -> QcThresholds:
    """Parse one `qc` block, refusing anything it does not recognize.

    The tests named here are the ones `kelpcompare qc` actually runs. Declaring a
    threshold for a deferred test (`flat_line`, `climatology` -- docs/04 s1)
    raises rather than sitting unread in the registry looking like coverage.

    `valid_range` comes in because it, and not this block, is what makes the
    gross-range test run -- so it is what says whether excepting a source from
    that test would except it from anything (ADR-008).
    """
    if not block:
        return QcThresholds()

    unknown = set(block) - {*IMPLEMENTED_TESTS, BY_SOURCE}
    if unknown:
        raise ValueError(
            f"{name!r} in {path} declares thresholds for {sorted(unknown)}, which "
            "kelpcompare qc does not run; see docs/04 s1 for the implemented tests"
        )

    spike = _spike(block.get("spike"), name=name, path=path)
    rate_of_change = _rate_of_change(block.get("rate_of_change"), name=name, path=path)
    gross_range = _gross_range(block.get("gross_range"), name=name, path=path)

    declared = {
        test
        for test, runs in (
            ("gross_range", valid_range is not None),
            ("spike", spike is not None),
            ("rate_of_change", rate_of_change is not None),
        )
        if runs
    }

    return QcThresholds(
        spike=spike,
        rate_of_change=rate_of_change,
        gross_range=gross_range,
        by_source=_by_source(
            block.get(BY_SOURCE), name=name, declared=declared, sources=sources, path=path
        ),
    )


def _by_source(
    block,
    *,
    name: str,
    declared: set[str],
    sources: Collection[str] | None,
    path: Path,
) -> dict[str, frozenset[str]]:
    """Parse the per-source exceptions -- `{source: {test: null}}` (ADR-008).

    Four refusals, each guarding the same failure: an exception that looks
    declared and is not applied, which is indistinguishable in the stored flags
    from the bug it was written to fix. An unknown source name, a test the qc
    stage does not run, a test this parameter would not have run anyway, and a
    threshold value where only `null` belongs.
    """
    if block is None:
        return {}
    if not isinstance(block, dict) or not block:
        raise ValueError(
            f"{name!r} in {path} has an empty or malformed {BY_SOURCE} block; it maps "
            "a source name to the tests that do not run for it, or is absent"
        )
    if sources is None:
        raise ValueError(
            f"{name!r} in {path} declares {BY_SOURCE} exceptions, which cannot be "
            "checked without the caller's known-source vocabulary; pass sources= "
            "to load_parameters (ADR-008)"
        )

    known = set(sources)
    exceptions: dict[str, frozenset[str]] = {}
    for source, tests in block.items():
        if source not in known:
            raise ValueError(
                f"{name!r} in {path} excepts {source!r} from a QC test, and {source!r} "
                f"is not a known source; known: {', '.join(sorted(known))}"
            )
        exceptions[source] = _excepted_tests(
            tests, name=name, source=source, declared=declared, path=path
        )
    return exceptions


def _excepted_tests(
    block, *, name: str, source: str, declared: set[str], path: Path
) -> frozenset[str]:
    """One source's `{test: null}` map, as the set of tests it switches off."""
    if not isinstance(block, dict) or not block:
        raise ValueError(
            f"{name!r} in {path} excepts {source!r} from nothing; name at least one of "
            f"{sorted(IMPLEMENTED_TESTS)}, or remove the entry"
        )

    unknown = set(block) - set(IMPLEMENTED_TESTS)
    if unknown:
        raise ValueError(
            f"{name!r} in {path} excepts {source!r} from {sorted(unknown)}, which "
            f"kelpcompare qc does not run; known: {sorted(IMPLEMENTED_TESTS)}"
        )

    for test, value in block.items():
        if value is not None:
            raise ValueError(
                f"{name!r} in {path} gives {source!r} a {test} threshold of {value!r} "
                f"under {BY_SOURCE}; an exception removes a test (null) and never "
                "retunes one for a source (ADR-008)"
            )
        if test not in declared:
            raise ValueError(
                f"{name!r} in {path} excepts {source!r} from {test!r}, which does not "
                f"run for {name!r} at all; the exception would remove nothing"
            )

    return frozenset(block)


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
