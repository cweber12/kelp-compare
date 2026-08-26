"""Reads `data/registry/features.json`: what the quarterly builder computes (docs/04 s2-s3).

A third registry file beside `sites.json` and `parameters.json`, and separate
from both for the same reason they are separate from each other: it answers a
different question. `sites.json` records which instrument was where;
`parameters.json` records what a measurement *means* -- its canonical SI unit
and the bounds its QARTOD tests need. A kelp stress temperature is neither. It
is not a property of temperature at all; it is an ecological decision about what
this analysis does with temperature, and it belongs to the analysis (ADR-006).

The posture is the parameter registry's, deliberately: refuse rather than
ignore. An unknown key, an empty block, a feature set the builder does not
implement -- each raises here, naming the file and the offending key, because a
feature that silently did not get built is indistinguishable in the output table
from a feature that was built and came out null. Keys beginning with `_` are
comments and are ignored at every level, which is how the shipped file explains
itself.

Threshold values are load-bearing beyond their arithmetic: the builder derives
its column names from them (docs/03), so retuning a threshold renames its column
rather than silently changing what an existing column means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FEATURES_PATH = Path("data/registry/features.json")

#: Feature set -> the threshold kinds it takes. A set with no kinds must not be
#: given a `thresholds` block; a set with kinds must be given a non-empty one.
#: `statistics` is universal -- distribution only, applicable to any parameter --
#: and `temperature` is it plus the docs/04 s2 ecological features.
IMPLEMENTED_FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "statistics": (),
    "temperature": ("days_above", "max_spell_above", "degree_days_above", "days_below"),
}

#: Named by docs/04 s2 and refused until the fetchers that would feed them exist
#: (docs/02). Listed so the refusal can say "not yet" rather than "no such thing".
DEFERRED_FEATURE_SETS = ("waves", "water_level")

_POLICY_KEYS = frozenset({"coverage_floor", "baseline"})
_BASELINE_KEYS = frozenset({"start_year", "end_year", "min_years"})
_PARAMETER_KEYS = frozenset({"feature_set", "thresholds"})


@dataclass(frozen=True)
class Baseline:
    """The fixed climatology window, inclusive of both years (docs/04 s3).

    Fixed, and recorded on the climatology table, so that an anomaly computed
    today still means the same thing after next year's backfill. `min_years` is
    what stops a difference against a one-year mean being presented as an
    anomaly.
    """

    start_year: int
    end_year: int
    min_years: int

    def contains(self, year: int) -> bool:
        return self.start_year <= year <= self.end_year

    @property
    def span(self) -> int:
        return self.end_year - self.start_year + 1

    @property
    def label(self) -> str:
        return f"{self.start_year}-{self.end_year}"


@dataclass(frozen=True)
class ParameterFeatures:
    """One parameter's entry: which feature set applies, and its thresholds.

    Declared, never inferred. `degC` is equally `sea_water_temperature` and
    `air_temperature`, and only one of them gets kelp stress thresholds -- the
    same reason docs/03 forbids inferring a parameter from its unit.
    """

    parameter: str
    feature_set: str
    thresholds: dict[str, tuple[float, ...]] = field(default_factory=dict)

    def of(self, kind: str) -> tuple[float, ...]:
        """The thresholds declared for one kind, or none. Never a default."""
        return self.thresholds.get(kind, ())


@dataclass(frozen=True)
class FeatureConfig:
    """The parsed configuration, plus the path it came from (for error messages)."""

    path: Path
    coverage_floor: float
    baseline: Baseline
    parameters: dict[str, ParameterFeatures]

    def __contains__(self, name: object) -> bool:
        return name in self.parameters

    def get(self, name: str) -> ParameterFeatures | None:
        return self.parameters.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.parameters))


def load_feature_config(path: Path | str | None = None) -> FeatureConfig:
    """Load the configuration. Defaults to `data/registry/features.json` under cwd."""
    resolved = Path(path) if path is not None else DEFAULT_FEATURES_PATH
    with resolved.open(encoding="utf-8") as handle:
        payload = _uncommented(json.load(handle))

    _reject_unknown(payload, {"policy", "parameters"}, what="top-level key", path=resolved)
    floor, baseline = _policy(payload.get("policy"), path=resolved)
    return FeatureConfig(
        path=resolved,
        coverage_floor=floor,
        baseline=baseline,
        parameters=_parameters(payload.get("parameters"), path=resolved),
    )


def _policy(block, *, path: Path) -> tuple[float, Baseline]:
    if not block:
        raise ValueError(
            f"{path} declares no `policy`; the coverage floor and the baseline are required"
        )
    _reject_unknown(block, _POLICY_KEYS, what="policy key", path=path)

    missing = sorted(_POLICY_KEYS - set(block))
    if missing:
        raise ValueError(f"{path} `policy` is missing {missing}")

    floor = _number(block["coverage_floor"], where="policy.coverage_floor", path=path)
    if not 0.0 <= floor <= 1.0:
        raise ValueError(
            f"{path} `policy.coverage_floor` is {floor}, which is not a fraction between 0 and 1"
        )
    return floor, _baseline(block["baseline"], path=path)


def _baseline(block, *, path: Path) -> Baseline:
    if not isinstance(block, dict) or not block:
        raise ValueError(f"{path} `policy.baseline` is empty; declare {sorted(_BASELINE_KEYS)}")
    _reject_unknown(block, _BASELINE_KEYS, what="policy.baseline key", path=path)

    missing = sorted(_BASELINE_KEYS - set(block))
    if missing:
        raise ValueError(f"{path} `policy.baseline` is missing {missing}")

    baseline = Baseline(
        start_year=_integer(block["start_year"], where="policy.baseline.start_year", path=path),
        end_year=_integer(block["end_year"], where="policy.baseline.end_year", path=path),
        min_years=_integer(block["min_years"], where="policy.baseline.min_years", path=path),
    )
    if baseline.end_year < baseline.start_year:
        raise ValueError(
            f"{path} `policy.baseline` ends in {baseline.end_year}, before it starts in "
            f"{baseline.start_year}"
        )
    if baseline.min_years < 1:
        raise ValueError(
            f"{path} `policy.baseline.min_years` is {baseline.min_years}; a climatology needs "
            "at least one contributing year"
        )
    # A minimum the window cannot reach is a disabled feature wearing a threshold's
    # clothes: every anomaly would be null, for a reason nothing in the output says.
    if baseline.min_years > baseline.span:
        raise ValueError(
            f"{path} `policy.baseline` asks for {baseline.min_years} years but spans only "
            f"{baseline.span} ({baseline.label}); no anomaly could ever be computed"
        )
    return baseline


def _parameters(block, *, path: Path) -> dict[str, ParameterFeatures]:
    if not block:
        raise ValueError(
            f"{path} declares no `parameters`; a configuration that builds nothing is a "
            "half-finished edit, not a decision"
        )
    return {name: _parameter(entry, name=name, path=path) for name, entry in block.items()}


def _parameter(entry, *, name: str, path: Path) -> ParameterFeatures:
    if not isinstance(entry, dict) or not entry:
        raise ValueError(f"{name!r} in {path} has an empty entry; declare a `feature_set`")
    _reject_unknown(entry, _PARAMETER_KEYS, what=f"{name!r} key", path=path)

    feature_set = entry.get("feature_set")
    if not feature_set:
        raise ValueError(
            f"{name!r} in {path} declares no `feature_set`; it is declared, never inferred "
            "from the parameter's unit (docs/03)"
        )
    kinds = _kinds(feature_set, name=name, path=path)
    return ParameterFeatures(
        parameter=name,
        feature_set=feature_set,
        thresholds=_thresholds(entry.get("thresholds"), kinds, name=name, path=path),
    )


def _kinds(feature_set: str, *, name: str, path: Path) -> tuple[str, ...]:
    """The threshold kinds this set takes, refusing a set the builder cannot build.

    Refusing rather than skipping, and at load time rather than at build time: a
    declared-but-unbuilt feature set would sit in the registry looking like
    coverage, which is the same failure the parameter registry already refuses
    for thresholds belonging to deferred QC tests.
    """
    if feature_set in IMPLEMENTED_FEATURE_SETS:
        return IMPLEMENTED_FEATURE_SETS[feature_set]

    deferred = (
        " (docs/04 s2 names it, but the fetcher that would feed it does not exist yet)"
        if feature_set in DEFERRED_FEATURE_SETS
        else ""
    )
    raise ValueError(
        f"{name!r} in {path} declares feature_set {feature_set!r}, which kelpcompare features "
        f"does not build{deferred}; implemented: {sorted(IMPLEMENTED_FEATURE_SETS)}"
    )


def _thresholds(
    block, kinds: tuple[str, ...], *, name: str, path: Path
) -> dict[str, tuple[float, ...]]:
    if not kinds:
        if block:
            raise ValueError(
                f"{name!r} in {path} declares thresholds, but its feature_set takes none; "
                "remove the block or declare a feature_set that has thresholds"
            )
        return {}

    if not block:
        raise ValueError(
            f"{name!r} in {path} declares no thresholds; its feature_set needs at least one of "
            f"{sorted(kinds)}, and a set with none would be `statistics` under another name"
        )
    _reject_unknown(block, set(kinds), what=f"{name!r} threshold kind", path=path)
    return {kind: _values(block[kind], kind=kind, name=name, path=path) for kind in block}


def _values(values, *, kind: str, name: str, path: Path) -> tuple[float, ...]:
    if not isinstance(values, list) or not values:
        raise ValueError(
            f"{name!r} in {path} declares {kind} as {values!r}; expected a non-empty list of "
            "threshold values"
        )
    numbers = tuple(_number(v, where=f"{name}.thresholds.{kind}", path=path) for v in values)
    if len(set(numbers)) != len(numbers):
        raise ValueError(f"{name!r} in {path} declares a {kind} threshold twice: {list(numbers)}")
    return numbers


def _reject_unknown(block: dict, allowed: set[str], *, what: str, path: Path) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ValueError(f"{path} declares unknown {what}(s) {unknown}; known: {sorted(allowed)}")


def _uncommented(payload: dict) -> dict:
    """Drop `_`-prefixed keys, recursively. The shipped file explains itself in them."""
    return {
        key: _uncommented(value) if isinstance(value, dict) else value
        for key, value in payload.items()
        if not key.startswith("_")
    }


def _number(value, *, where: str, path: Path) -> float:
    """A JSON number, and not a JSON `true` -- which `float` would take as 1.0."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"{path} `{where}` is {value!r}, which is not a number")


def _integer(value, *, where: str, path: Path) -> int:
    """A whole year. `2007.5` is refused rather than truncated to a year it is not."""
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    raise ValueError(f"{path} `{where}` is {value!r}, which is not a whole year")
