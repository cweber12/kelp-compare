"""kelpcompare.features — see docs/01 layer boundaries and CLAUDE.md hard rules.

Layer 2's aggregation stage (docs/01): it reads QC-flagged observations and
turns a stream of 10-minute readings into one row per QC series per quarter, in
the Kelp Watch calendar.

`config` reads what to build from the registry; `quarters` owns the UTC calendar
that decides which quarter an instant belongs to; `quarterly` computes the
features and the coverage bookkeeping that makes them interpretable.
"""

from kelpcompare.features.config import (
    DEFERRED_FEATURE_SETS,
    IMPLEMENTED_FEATURE_SETS,
    Baseline,
    FeatureConfig,
    ParameterFeatures,
    load_feature_config,
)
from kelpcompare.features.quarterly import (
    BOOKKEEPING_COLUMNS,
    QUARTERLY_KEY,
    STATISTICS,
    QuarterlyOutcome,
    SeriesQuarters,
    build_quarterly,
    feature_columns,
    quarterly_columns,
    threshold_label,
)

__all__ = [
    "BOOKKEEPING_COLUMNS",
    "DEFERRED_FEATURE_SETS",
    "IMPLEMENTED_FEATURE_SETS",
    "QUARTERLY_KEY",
    "STATISTICS",
    "Baseline",
    "FeatureConfig",
    "ParameterFeatures",
    "QuarterlyOutcome",
    "SeriesQuarters",
    "build_quarterly",
    "feature_columns",
    "load_feature_config",
    "quarterly_columns",
    "threshold_label",
]
