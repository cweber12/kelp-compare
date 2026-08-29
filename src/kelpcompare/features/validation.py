"""Neighbor validation: does a project sensor agree with the public network?

The standing table docs/04 §1 asks for, and the evidence base for the claim that
an instrument nobody else operates can be trusted. One row per deployment ×
reference × parameter × depth pair, carrying how many bins the two series shared,
how far apart their sensors sat, and — subject to that gap — bias, RMSE and
correlation.

**The depth rule is the whole point of this module, and it is per statistic.**
A reference at another depth is not a worse version of one at the same depth; for
two of the three statistics it is a different measurement. Below the thermocline
the offset between two depths *is* most of the signal, so:

- **Bias is refused across a depth gap.** It measures stratification and prints
  it as instrument error, inverting what this table is evidence for.
- **RMSE is refused**, inheriting the same offset.
- **Correlation is reported**, because both series still track the same synoptic
  forcing — with `depth_gap_m` on the row, so it is never read as agreement.

A null in `bias` here therefore means "this comparison was refused", not "the
data was missing". `n_pairs` is populated either way, which is how the two are
told apart.

**Both sides are binned to a common cadence.** A 10-minute logger and a 6-minute
station do not share timestamps, and joining on exact ones would answer a
question about clock alignment. The bin is the coarser of the two median native
intervals and each side contributes its mean within it; docs/03 records the two
alternatives rejected.

**One platform is one reference.** `NDBC:LJAC1` and `COOPS:9410230` are one NOS
package under two identifiers, and docs/04 says validation must not count them
twice. References are grouped by platform, the first member with rows produces
the row, and the rest are named in `collapsed_refs` so the fold is visible in
the table rather than inferred from an absence.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from kelpcompare.registry import Registry, find_station, neighbor_refs

#: docs/03 `validation.parquet`. `reference_depth_m` is in the key because
#: agreement at 0.5 m says nothing about agreement at 5 m on the same station.
VALIDATION_KEY = (
    "site_id",
    "serial",
    "deployment_number",
    "parameter",
    "depth_m",
    "reference_site_id",
    "reference_depth_m",
)

VALIDATION_COLUMNS = (
    *VALIDATION_KEY,
    "source",
    "reference_source",
    "depth_gap_m",
    "depth_comparable",
    "cadence_s",
    "n_pairs",
    "overlap_start",
    "overlap_end",
    "correlation",
    "bias",
    "rmse",
    "collapsed_refs",
    "qc_max_flag",
)

#: Used when neither series is long enough to have a median interval. One second
#: makes the bin a no-op rather than a guess: the join then falls back to exact
#: timestamps, which is the honest answer for a pair of one-row series.
_FALLBACK_CADENCE = pd.Timedelta(seconds=1)


@dataclass(frozen=True)
class _Series:
    """One side of a comparison: its rows, and where the instrument sat."""

    site_id: str
    source: str
    depth_m: float | None
    frame: pd.DataFrame


def build_validation(
    observations: pd.DataFrame,
    registry: Registry,
    *,
    tolerance_m: float,
    qc_max_flag: int,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Compare every registered deployment against its declared references.

    Returns the table and the warnings worth putting in a manifest — a reference
    naming a site nobody registered, or one that has no overlapping rows. Those
    are gaps a reader has to be told about: a deployment silently absent from
    this table is indistinguishable from one that agreed with everything.
    """
    warnings: list[str] = []
    if observations.empty:
        return empty_validation(), ()

    kept = observations[observations["qc_flag"] <= qc_max_flag]
    rows: list[dict] = []

    for deployment in registry.deployments:
        if deployment.depth_m is None or not deployment.series_map:
            continue
        refs = neighbor_refs(registry, deployment.site_id)
        if not refs:
            warnings.append(f"{deployment.site_id}: no neighbor_refs declared, nothing to validate")
            continue

        window = _window_utc(deployment.window_local, deployment.tz)
        for parameter in sorted(set(deployment.series_map.values())):
            own = _deployment_series(kept, deployment, parameter, window)
            if own is None:
                warnings.append(
                    f"{deployment.site_id}: no {parameter} rows at {deployment.depth_m} m "
                    f"within the deployment window, so it cannot be validated"
                )
                continue
            rows.extend(
                _rows_for(
                    own,
                    deployment=deployment,
                    parameter=parameter,
                    refs=refs,
                    kept=kept,
                    registry=registry,
                    tolerance_m=tolerance_m,
                    qc_max_flag=qc_max_flag,
                    warnings=warnings,
                )
            )

    return _typed(rows), tuple(warnings)


def empty_validation() -> pd.DataFrame:
    """The table's shape with no rows, so a build with nothing to do still writes
    a readable file rather than one whose columns depend on what it found."""
    return _typed([])


def _rows_for(
    own: _Series,
    *,
    deployment,
    parameter: str,
    refs: tuple[str, ...],
    kept: pd.DataFrame,
    registry: Registry,
    tolerance_m: float,
    qc_max_flag: int,
    warnings: list[str],
) -> list[dict]:
    """Every row this deployment's series produces, one platform at a time."""
    rows: list[dict] = []
    for group in _platforms(refs, registry, warnings):
        chosen, collapsed = _choose(group, kept, parameter)
        if chosen is None:
            warnings.append(
                f"{deployment.site_id}: no {parameter} rows from {'/'.join(group)}; "
                "that reference contributes no validation row"
            )
            continue
        for reference in _reference_series(kept, chosen, parameter):
            rows.append(
                _compare(
                    own,
                    reference,
                    deployment=deployment,
                    parameter=parameter,
                    collapsed=collapsed,
                    tolerance_m=tolerance_m,
                    qc_max_flag=qc_max_flag,
                )
            )
    return rows


def _platforms(
    refs: tuple[str, ...], registry: Registry, warnings: list[str]
) -> list[tuple[str, ...]]:
    """Group `neighbor_refs` into platforms, in first-appearance order.

    Two site records that name each other in `same_platform_as` describe one
    instrument package, and docs/04 §1 forbids counting them as two independent
    references. The grouping is symmetric on purpose: only one of the pair has to
    declare the relationship for both to be folded, so a registry that records it
    once is not a registry that records it wrongly.
    """
    groups: list[list[str]] = []
    placed: dict[str, int] = {}
    for ref in refs:
        station = find_station(registry, ref)
        if station is None:
            warnings.append(f"{ref}: named in neighbor_refs but not a registered public station")
            continue
        kin = {ref, *station.same_platform_as}
        index = next((placed[name] for name in kin if name in placed), None)
        if index is None:
            index = len(groups)
            groups.append([])
        groups[index].append(ref)
        for name in kin:
            placed[name] = index
    return [tuple(group) for group in groups if group]


def _choose(
    group: tuple[str, ...], kept: pd.DataFrame, parameter: str
) -> tuple[str | None, tuple[str, ...]]:
    """The first member of a platform with rows, and the members folded into it.

    Every other member is reported as collapsed whether or not it has rows of its
    own: they are one instrument, so the fold is a fact about the hardware rather
    than about what happened to be downloaded.
    """
    for ref in group:
        if not kept[(kept["site_id"] == ref) & (kept["parameter"] == parameter)].empty:
            return ref, tuple(name for name in group if name != ref)
    return None, ()


def _deployment_series(
    kept: pd.DataFrame, deployment, parameter: str, window: tuple | None
) -> _Series | None:
    """This deployment's own rows: its site, its depth, inside its window.

    Restricted by window as well as by site and depth, because `site_id` and
    `depth_m` do not distinguish two deployments of one logger at one place —
    `deployment_number` does, and the window is how that reaches the rows. Rows
    outside it already carry `qc_flag = 4` and are usually filtered before this,
    but a caller passing `--qc-max-flag 9` must not silently merge two
    deployments into one comparison.
    """
    frame = kept[
        (kept["site_id"] == deployment.site_id)
        & (kept["parameter"] == parameter)
        & (kept["depth_m"] == deployment.depth_m)
    ]
    if window is not None:
        frame = frame[(frame["timestamp"] >= window[0]) & (frame["timestamp"] <= window[1])]
    if frame.empty:
        return None
    return _Series(
        site_id=deployment.site_id,
        source=str(frame["source"].iloc[0]),
        depth_m=deployment.depth_m,
        frame=frame,
    )


def _reference_series(kept: pd.DataFrame, site_id: str, parameter: str) -> list[_Series]:
    """One series per depth the reference actually carries this parameter at.

    Read from the rows rather than from `sensor_depths_m`, for the reason the
    comparison table takes its series from `quarterly_env` rather than the
    registry: a declared depth nobody has downloaded would produce a row that can
    only ever be null, and a landed depth nobody declared would silently vanish.
    """
    frame = kept[(kept["site_id"] == site_id) & (kept["parameter"] == parameter)]
    if frame.empty:
        return []
    series = []
    for depth, part in frame.groupby("depth_m", dropna=False):
        series.append(
            _Series(
                site_id=site_id,
                source=str(part["source"].iloc[0]),
                depth_m=None if pd.isna(depth) else float(depth),
                frame=part,
            )
        )
    return series


def _compare(
    own: _Series,
    reference: _Series,
    *,
    deployment,
    parameter: str,
    collapsed: tuple[str, ...],
    tolerance_m: float,
    qc_max_flag: int,
) -> dict:
    """One row: the two series binned to a common cadence and joined."""
    cadence = _cadence(own.frame, reference.frame)
    left = _binned(own.frame, cadence)
    right = _binned(reference.frame, cadence)
    paired = left.join(right, how="inner", lsuffix="_own", rsuffix="_ref")

    gap = _depth_gap(own.depth_m, reference.depth_m)
    # A null gap is an unknown one -- a met parameter, or a provider that never
    # published a depth -- and an unknown gap is not a small one. Refusing bias
    # there keeps "we checked and it is close" distinct from "we do not know".
    comparable = bool(gap is not None and gap <= tolerance_m)

    return {
        "site_id": deployment.site_id,
        "serial": deployment.serial,
        "deployment_number": deployment.deployment_number,
        "parameter": parameter,
        "depth_m": own.depth_m,
        "reference_site_id": reference.site_id,
        "reference_depth_m": reference.depth_m,
        "source": own.source,
        "reference_source": reference.source,
        "depth_gap_m": gap,
        "depth_comparable": comparable,
        "cadence_s": int(cadence.total_seconds()),
        "n_pairs": len(paired),
        "overlap_start": paired.index.min() if len(paired) else pd.NaT,
        "overlap_end": paired.index.max() if len(paired) else pd.NaT,
        "correlation": _correlation(paired),
        "bias": _bias(paired) if comparable else np.nan,
        "rmse": _rmse(paired) if comparable else np.nan,
        "collapsed_refs": ";".join(collapsed),
        "qc_max_flag": qc_max_flag,
    }


def _window_utc(window_local: tuple[str, str] | None, tz: str | None):
    """The deployment window as naive UTC, matching the stored timestamps."""
    if not window_local or not tz:
        return None
    return tuple(
        pd.Timestamp(edge, tz=tz).tz_convert("UTC").tz_localize(None) for edge in window_local
    )


def _cadence(left: pd.DataFrame, right: pd.DataFrame) -> pd.Timedelta:
    """The coarser of the two median native intervals.

    Coarser rather than finer, because binning to the finer one would leave most
    bins holding a reading from only one side and throw the pairs away — the
    exact-timestamp join under another name.
    """
    medians = [median for median in (_median_interval(left), _median_interval(right)) if median]
    return max(medians) if medians else _FALLBACK_CADENCE


def _median_interval(frame: pd.DataFrame) -> pd.Timedelta | None:
    if len(frame) < 2:
        return None
    median = frame["timestamp"].sort_values().diff().median()
    return None if pd.isna(median) or median <= pd.Timedelta(0) else median


def _binned(frame: pd.DataFrame, cadence: pd.Timedelta) -> pd.DataFrame:
    """Mean value per bin, indexed by the bin's start."""
    binned = frame.assign(bin=frame["timestamp"].dt.floor(cadence))
    return binned.groupby("bin")["value"].mean().to_frame("value")


def _depth_gap(own: float | None, reference: float | None) -> float | None:
    if own is None or reference is None or pd.isna(own) or pd.isna(reference):
        return None
    return abs(float(own) - float(reference))


def _correlation(paired: pd.DataFrame) -> float:
    """Pearson r, or null where it would not mean anything.

    Two points always correlate perfectly and a flat series has no variance to
    correlate; both would print 1.0 or NaN as though they were findings.
    """
    if len(paired) < 3:
        return np.nan
    own, ref = paired["value_own"], paired["value_ref"]
    if own.std(ddof=1) == 0 or ref.std(ddof=1) == 0:
        return np.nan
    return float(own.corr(ref))


def _bias(paired: pd.DataFrame) -> float:
    if paired.empty:
        return np.nan
    return float((paired["value_own"] - paired["value_ref"]).mean())


def _rmse(paired: pd.DataFrame) -> float:
    if paired.empty:
        return np.nan
    return float(np.sqrt(((paired["value_own"] - paired["value_ref"]) ** 2).mean()))


def _typed(rows: list[dict]) -> pd.DataFrame:
    """The frame with its declared dtypes, so an empty build writes the same
    schema a populated one does."""
    frame = pd.DataFrame(rows, columns=list(VALIDATION_COLUMNS))
    return frame.astype(
        {
            "site_id": "string",
            "serial": "string",
            "deployment_number": "Int64",
            "parameter": "string",
            "depth_m": "float64",
            "reference_site_id": "string",
            "reference_depth_m": "float64",
            "source": "string",
            "reference_source": "string",
            "depth_gap_m": "float64",
            "depth_comparable": "boolean",
            "cadence_s": "Int64",
            "n_pairs": "Int64",
            "overlap_start": "datetime64[us]",
            "overlap_end": "datetime64[us]",
            "correlation": "float64",
            "bias": "float64",
            "rmse": "float64",
            "collapsed_refs": "string",
            "qc_max_flag": "Int8",
        }
    )
