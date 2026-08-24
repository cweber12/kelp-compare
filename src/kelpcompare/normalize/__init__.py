"""kelpcompare.normalize -- UTC + SI + controlled parameter names (docs/01 layer 1).

The shared, vendor-agnostic stage: every adapter and every fetcher converges here,
and nothing downstream knows where a row came from (docs/06 s4).
"""

from kelpcompare.normalize.observations import (
    NormalizedBatch,
    convert_unit,
    to_observations,
)

__all__ = ["NormalizedBatch", "convert_unit", "to_observations"]
