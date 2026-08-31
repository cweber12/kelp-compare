"""kelpcompare.features — see docs/01 layer boundaries and CLAUDE.md hard rules.

Layer 2's aggregation stage (docs/01): it reads QC-flagged observations and
turns a stream of 10-minute readings into one row per QC series per quarter, in
the Kelp Watch calendar.

`config` reads what to build from the registry; `quarters` owns the UTC calendar
that decides which quarter an instant belongs to; `quarterly` computes the
features and the coverage bookkeeping that makes them interpretable; and
`climatology` takes the seasonal cycle back out again, which is what makes a
kelp-vs-environment correlation a relationship rather than a picture of summer.
"""

from kelpcompare.features.build import BuildOutcome, build_features
from kelpcompare.features.climatology import (
    CLIMATOLOGY_COLUMNS,
    CLIMATOLOGY_KEY,
    ENV_SERIES,
    anomaly_columns,
    build_climatology,
    climatology_columns,
    climatology_key,
    override_warnings,
    quarterly_env_columns,
    with_anomalies,
)
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
    "CLIMATOLOGY_COLUMNS",
    "CLIMATOLOGY_KEY",
    "DEFERRED_FEATURE_SETS",
    "ENV_SERIES",
    "IMPLEMENTED_FEATURE_SETS",
    "QUARTERLY_KEY",
    "STATISTICS",
    "Baseline",
    "BuildOutcome",
    "FeatureConfig",
    "ParameterFeatures",
    "QuarterlyOutcome",
    "SeriesQuarters",
    "anomaly_columns",
    "build_climatology",
    "build_features",
    "build_quarterly",
    "climatology_columns",
    "climatology_key",
    "feature_columns",
    "load_feature_config",
    "override_warnings",
    "quarterly_columns",
    "quarterly_env_columns",
    "threshold_label",
    "with_anomalies",
]
