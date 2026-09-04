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

One entry here answers a question the builder never asks: `role` says whether a
parameter may be *pre-registered* (docs/04 s5), not what is computed off it. It
lives beside `feature_set` because both are decisions the analysis makes about a
measurement rather than properties of the measurement, which is the line ADR-006
draws between this file and `parameters.json`. Nothing in the pipeline reads it
-- a control is fetched, normalized, flagged, aggregated and stored exactly as a
predictor is -- so demoting a parameter withholds it from a claim without ever
withholding a row, which is hard rule 4's shape applied one layer up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FEATURES_PATH = Path("data/registry/features.json")

#: A declared threshold is one number -- or, for a band, the pair that bounds it.
Threshold = float | tuple[float, float]

#: Feature set -> the threshold kinds it takes. A set with no kinds must not be
#: given a `thresholds` block; a set with kinds must be given a non-empty one.
#: `statistics` is universal -- distribution only, applicable to any parameter --
#: and `temperature` is it plus the docs/04 s2 ecological features.
IMPLEMENTED_FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "statistics": (),
    "temperature": (
        "days_above",
        "max_spell_above",
        "degree_days_above",
        "days_below",
        "time_in_band",
    ),
}

#: The kinds declared as `[low, high]` pairs rather than as single numbers. A
#: band needs both edges to mean anything and derives its column name from both,
#: so the parser is told which shape a kind takes rather than inferring it from
#: whatever the first entry happens to look like -- which would read `[14, 20]`
#: as a band under one kind and as two thresholds under another.
_PAIR_VALUED_KINDS = frozenset({"time_in_band"})

#: Named by docs/04 s2 and refused until the fetchers that would feed them exist
#: (docs/02). Listed so the refusal can say "not yet" rather than "no such thing".
DEFERRED_FEATURE_SETS = ("waves", "water_level")

#: What a parameter is *for* in the docs/04 s5 screen, which is a different
#: question from what gets computed off it. A `predictor` may be pre-registered
#: and carried into docs/04 s4.3; a `control` is screened and reported but never
#: registered, because its coefficient is evidence about the screen rather than
#: about kelp. Both are built, stored and flagged identically -- the role governs
#: the analysis, never the pipeline, so demoting a parameter cannot delete a row.
ANALYSIS_ROLES = ("predictor", "control")

#: What a parameter is when it says nothing. Predictor rather than control so
#: that the pool is opt-out: a parameter nobody has thought about yet is one the
#: screen will surface and the operator will have to argue with, which fails
#: louder than one silently withheld.
DEFAULT_ANALYSIS_ROLE = "predictor"

#: The gap, in metres, within which docs/04 s1 lets a neighbor validation report
#: bias and RMSE. Provisional, and thin: set from the only two pairs measured so
#: far -- NDBC:LJAC1 sits 4.83 m above PROJ:TIDBIT-1 and runs about 1 degC
#: warmer, and 13.36 m above PROJ:TIDBIT-2 and runs about 5 degC warmer -- both
#: in one stratified summer. Retune it against a winter record.
DEFAULT_NEIGHBOR_DEPTH_TOLERANCE_M = 5.0

#: Required in every `policy` block: nothing can be built without them.
_REQUIRED_POLICY_KEYS = frozenset({"coverage_floor", "baseline"})

#: Accepted, defaulted when absent. Optional rather than required because the
#: default is a documented number rather than a guess a run has to make, and
#: requiring it would invalidate every features.json written before it existed.
_OPTIONAL_POLICY_KEYS = frozenset({"neighbor_depth_tolerance_m", "baseline_overrides"})

_POLICY_KEYS = _REQUIRED_POLICY_KEYS | _OPTIONAL_POLICY_KEYS
_BASELINE_KEYS = frozenset({"start_year", "end_year", "min_years"})
_OVERRIDE_KEYS = frozenset({"start_year", "end_year"})
_PARAMETER_KEYS = frozenset({"feature_set", "thresholds", "role"})


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
    thresholds: dict[str, tuple[Threshold, ...]] = field(default_factory=dict)
    role: str = DEFAULT_ANALYSIS_ROLE

    def of(self, kind: str) -> tuple[Threshold, ...]:
        """The thresholds declared for one kind, or none. Never a default."""
        return self.thresholds.get(kind, ())

    @property
    def is_control(self) -> bool:
        """Screened and reported, never pre-registered (docs/04 s5)."""
        return self.role == "control"


@dataclass(frozen=True)
class FeatureConfig:
    """The parsed configuration, plus the path it came from (for error messages)."""

    path: Path
    coverage_floor: float
    baseline: Baseline
    parameters: dict[str, ParameterFeatures]
    neighbor_depth_tolerance_m: float = DEFAULT_NEIGHBOR_DEPTH_TOLERANCE_M
    baseline_overrides: dict[str, Baseline] = field(default_factory=dict)

    def __contains__(self, name: object) -> bool:
        return name in self.parameters

    def get(self, name: str) -> ParameterFeatures | None:
        return self.parameters.get(name)

    def baseline_for(self, site_id: str | None = None) -> Baseline:
        """The window one series takes its climatology against (docs/04 s3).

        The canonical window unless an operator declared otherwise for this
        site. Passing nothing -- or a site nobody has declared -- is the ordinary
        case and gets the canonical window, which is what keeps the kelp half,
        keyed on `polygon_id` and carrying no `site_id` at all, out of this
        entirely.
        """
        if site_id is None:
            return self.baseline
        return self.baseline_overrides.get(site_id, self.baseline)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.parameters))

    def roles(self) -> dict[str, str]:
        """Every declared parameter against its analysis role, defaults included.

        The mapping rather than the two lists, because a reader that wants to
        *label* a screened row needs the role of whatever the table happens to
        carry, and a parameter absent from this file has no role to report.
        """
        return {name: entry.role for name, entry in self.parameters.items()}

    @property
    def controls(self) -> tuple[str, ...]:
        """The parameters docs/04 s5 keeps out of the pre-registration pool."""
        return tuple(sorted(name for name, e in self.parameters.items() if e.is_control))

    @property
    def predictors(self) -> tuple[str, ...]:
        """The pool itself: everything not demoted."""
        return tuple(sorted(name for name, e in self.parameters.items() if not e.is_control))


def load_feature_config(path: Path | str | None = None) -> FeatureConfig:
    """Load the configuration. Defaults to `data/registry/features.json` under cwd."""
    resolved = Path(path) if path is not None else DEFAULT_FEATURES_PATH
    with resolved.open(encoding="utf-8") as handle:
        payload = _uncommented(json.load(handle))

    _reject_unknown(payload, {"policy", "parameters"}, what="top-level key", path=resolved)
    floor, baseline, tolerance, overrides = _policy(payload.get("policy"), path=resolved)
    return FeatureConfig(
        path=resolved,
        coverage_floor=floor,
        baseline=baseline,
        parameters=_parameters(payload.get("parameters"), path=resolved),
        neighbor_depth_tolerance_m=tolerance,
        baseline_overrides=overrides,
    )


def _policy(block, *, path: Path) -> tuple[float, Baseline, float, dict[str, Baseline]]:
    if not block:
        raise ValueError(
            f"{path} declares no `policy`; the coverage floor and the baseline are required"
        )
    _reject_unknown(block, _POLICY_KEYS, what="policy key", path=path)

    missing = sorted(_REQUIRED_POLICY_KEYS - set(block))
    if missing:
        raise ValueError(f"{path} `policy` is missing {missing}")

    floor = _number(block["coverage_floor"], where="policy.coverage_floor", path=path)
    if not 0.0 <= floor <= 1.0:
        raise ValueError(
            f"{path} `policy.coverage_floor` is {floor}, which is not a fraction between 0 and 1"
        )
    baseline = _baseline(block["baseline"], path=path)
    return (
        floor,
        baseline,
        _tolerance(block, path=path),
        _baseline_overrides(block.get("baseline_overrides"), baseline, path=path),
    )


def _tolerance(block, *, path: Path) -> float:
    """The docs/04 s1 depth tolerance, or the documented default.

    Negative is refused rather than clamped: a negative tolerance would make
    every pair incomparable and report a table of nulls that looks like a record
    of disagreement rather than of a misconfiguration. Zero is allowed and means
    exactly what it says -- report bias and RMSE only against a reference at the
    same depth -- which is a defensible position, not a mistake.
    """
    if "neighbor_depth_tolerance_m" not in block:
        return DEFAULT_NEIGHBOR_DEPTH_TOLERANCE_M
    value = _number(
        block["neighbor_depth_tolerance_m"], where="policy.neighbor_depth_tolerance_m", path=path
    )
    if value < 0.0:
        raise ValueError(
            f"{path} `policy.neighbor_depth_tolerance_m` is {value}, which is negative; "
            "0 means same-depth-only and is the strictest meaningful value"
        )
    return value


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


def _baseline_overrides(block, default: Baseline, *, path: Path) -> dict[str, Baseline]:
    """Fixed windows for the series that cannot cover the canonical one (docs/04 s3).

    Declared per site and never derived from whatever years happen to have
    landed. A window computed from the available record would grow with every
    backfill and move every anomaly ever taken against it, which is the one
    thing a fixed window exists to prevent -- so a station whose record
    post-dates the canonical window gets a window an operator wrote down, or it
    gets no anomaly at all.

    `min_years` is deliberately not overridable, and is taken from the canonical
    window. How thin is too thin for a climatology is a property of the method
    rather than of a station, and letting each override carry its own would make
    the weakest baselines the ones nearest the beds -- exactly where a thin
    anomaly is most likely to be read as a result.
    """
    if block is None:
        return {}
    if not isinstance(block, dict) or not block:
        raise ValueError(
            f"{path} `policy.baseline_overrides` is {block!r}; expected a block of "
            "site_id -> window, or no such key at all"
        )
    return {
        site_id: _override(entry, site_id=site_id, default=default, path=path)
        for site_id, entry in block.items()
    }


def _override(entry, *, site_id: str, default: Baseline, path: Path) -> Baseline:
    """One declared window, refused rather than repaired when it cannot work."""
    where = f"policy.baseline_overrides.{site_id}"
    if not isinstance(entry, dict) or not entry:
        raise ValueError(f"{path} `{where}` is empty; declare {sorted(_OVERRIDE_KEYS)}")
    _reject_unknown(entry, _OVERRIDE_KEYS, what=f"{where} key", path=path)

    missing = sorted(_OVERRIDE_KEYS - set(entry))
    if missing:
        raise ValueError(f"{path} `{where}` is missing {missing}")

    window = Baseline(
        start_year=_integer(entry["start_year"], where=f"{where}.start_year", path=path),
        end_year=_integer(entry["end_year"], where=f"{where}.end_year", path=path),
        min_years=default.min_years,
    )
    if window.end_year < window.start_year:
        raise ValueError(
            f"{path} `{where}` ends in {window.end_year}, before it starts in {window.start_year}"
        )
    # The same refusal the canonical window gets, for the same reason: a window
    # too short to reach the minimum produces nothing but nulls, and no output
    # column says why.
    if window.min_years > window.span:
        raise ValueError(
            f"{path} `{where}` spans {window.span} year(s) ({window.label}) against a "
            f"min_years of {window.min_years}; no anomaly could ever be computed. Widen it, "
            "or leave the series without an override and let its anomalies be null"
        )
    return window


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
        role=_role(entry.get("role"), name=name, path=path),
    )


def _role(value, *, name: str, path: Path) -> str:
    """The docs/04 s5 analysis role, defaulted when absent and refused when wrong.

    Optional for the same reason the depth tolerance is: the default is a
    documented position rather than a guess, and requiring it would invalidate
    every features.json written before roles existed. Refused when misspelled
    because the failure is silent in both directions -- `"controls"` would leave
    a demoted parameter in the pre-registration pool, and there is no output
    column in which that is visible.
    """
    if value is None:
        return DEFAULT_ANALYSIS_ROLE
    if value not in ANALYSIS_ROLES:
        raise ValueError(
            f"{name!r} in {path} declares role {value!r}; known roles are {sorted(ANALYSIS_ROLES)}"
        )
    return value


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


def _values(values, *, kind: str, name: str, path: Path) -> tuple[Threshold, ...]:
    shape = "[low, high] pairs" if kind in _PAIR_VALUED_KINDS else "threshold values"
    if not isinstance(values, list) or not values:
        raise ValueError(
            f"{name!r} in {path} declares {kind} as {values!r}; expected a non-empty list of "
            f"{shape}"
        )
    where = f"{name}.thresholds.{kind}"
    if kind in _PAIR_VALUED_KINDS:
        parsed: tuple[Threshold, ...] = tuple(
            _pair(value, where=where, path=path) for value in values
        )
    else:
        parsed = tuple(_number(value, where=where, path=path) for value in values)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{name!r} in {path} declares a {kind} threshold twice: {list(parsed)}")
    return parsed


def _pair(value, *, where: str, path: Path) -> tuple[float, float]:
    """One `[low, high]` band, refused unless it is two numbers in that order.

    An equal pair is refused with the reversed one. A band of zero width tests
    for one exact float, which a measured value meets by accident if ever, so it
    would produce a column of zeros that reads as a real measurement of absence.
    """
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(
            f"{path} `{where}` declares {value!r}; expected a [low, high] pair of numbers"
        )
    low, high = (_number(edge, where=where, path=path) for edge in value)
    if low >= high:
        raise ValueError(
            f"{path} `{where}` declares [{low:g}, {high:g}], whose low edge is not below its high"
        )
    return low, high


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
