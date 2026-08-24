"""kelpcompare.adapters — vendor file parsers for project sensors (docs/06).

Every adapter implements the same three functions -- `sniff`, `parse`,
`metadata` -- and returns the shared types in `base`. Adapters extract
faithfully and nothing more: no timezone conversion, no unit conversion, no
deployment-window trimming, no writes. That is what keeps the rest of the
system from revolving around any one vendor's format (docs/01 layer boundaries,
CLAUDE.md hard rules).
"""

from kelpcompare.adapters.base import (
    Check,
    Provenance,
    RawSeries,
    SeriesInfo,
    ValidationReport,
    registry_gate,
)

__all__ = [
    "Check",
    "Provenance",
    "RawSeries",
    "SeriesInfo",
    "ValidationReport",
    "registry_gate",
]
