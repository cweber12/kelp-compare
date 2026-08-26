"""Observations in, both feature tables out (docs/03).

The three stages in the order they have to run: quarterly features, then the
climatology built from them, then the anomalies taken against that climatology.
Nothing here reads or writes a file, which is what makes it usable from a
notebook: a sensitivity rerun at pass-only strictness can call this against an
in-memory frame and compare, without producing a competing file of record.
Hard rule 7 forbids bypassing the CLI to *write* Parquet, not to read
observations into an analysis.

The climatology is rebuilt on every run rather than read back from the last one.
That is what keeps the tables a pure function of the observations zone and the
configuration -- so `rebuild` means something, and two runs over unchanged
inputs produce the same file. It costs nothing in correctness because the
baseline window is fixed: recomputing it gives the same numbers unless the
baseline period itself was backfilled, which is exactly when the anomalies
*should* move.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from kelpcompare.features.climatology import build_climatology, with_anomalies
from kelpcompare.features.config import FeatureConfig
from kelpcompare.features.quarterly import SeriesQuarters, build_quarterly
from kelpcompare.storage import FLAG_NOT_EVALUATED


@dataclass(frozen=True)
class BuildOutcome:
    """Both docs/03 feature tables, plus what the run should report about them."""

    quarterly: pd.DataFrame
    climatology: pd.DataFrame
    series: tuple[SeriesQuarters, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def quarters(self) -> int:
        return len(self.quarterly)

    @property
    def usable(self) -> int:
        return int(self.quarterly["usable"].sum()) if len(self.quarterly) else 0


def build_features(
    frame: pd.DataFrame,
    config: FeatureConfig,
    *,
    qc_max_flag: int = FLAG_NOT_EVALUATED,
    now: pd.Timestamp | None = None,
) -> BuildOutcome:
    """Build `quarterly_env` and `climatology_env` from a docs/03 observation frame."""
    quarterly = build_quarterly(frame, config, qc_max_flag=qc_max_flag, now=now)
    climatology = build_climatology(quarterly.frame, config)
    return BuildOutcome(
        quarterly=with_anomalies(quarterly.frame, climatology, config),
        climatology=climatology,
        series=quarterly.series,
        warnings=quarterly.warnings,
    )
