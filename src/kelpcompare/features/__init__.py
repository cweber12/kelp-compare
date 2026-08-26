"""kelpcompare.features — see docs/01 layer boundaries and CLAUDE.md hard rules.

Layer 2's aggregation stage (docs/01): it reads QC-flagged observations and
turns a stream of 10-minute readings into one row per QC series per quarter, in
the Kelp Watch calendar. `config` reads what to build from the registry; the
modules added beside it compute it.
"""

from kelpcompare.features.config import (
    DEFERRED_FEATURE_SETS,
    IMPLEMENTED_FEATURE_SETS,
    Baseline,
    FeatureConfig,
    ParameterFeatures,
    load_feature_config,
)

__all__ = [
    "DEFERRED_FEATURE_SETS",
    "IMPLEMENTED_FEATURE_SETS",
    "Baseline",
    "FeatureConfig",
    "ParameterFeatures",
    "load_feature_config",
]
