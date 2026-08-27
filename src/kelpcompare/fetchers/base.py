"""The fetcher contract: shared types plus the raw landing (docs/02).

Every fetcher implements `stations(registry)`, `fetch(...) -> Payload`, and
`parse(payload, ...) -> DataFrame`, and returns the types defined here. Nothing
in this module knows about any particular source -- that is the point: the next
source reuses these types and `land` unchanged.

The split mirrors `adapters/base.py` and exists for the same reason. A fetcher
retrieves and extracts; it never decides what happens next. Landing the payload
is this module's job, acting on a failure is the ingest CLI's, and the
observation zone is `storage`'s -- one place decides each.

Two rules from the docs/02 cross-cutting section are enforced here rather than
left to each fetcher to remember:

**Land before parsing.** `fetch` returns bytes and the URL they came from;
`land` writes those bytes into `raw/{source}/` untouched, before anything reads
them. A parser bug is then a re-parse, not a re-fetch -- which matters most for
the sources that cannot be re-fetched at all. NDBC realtime holds roughly 45
days, so a payload not landed on the day it was retrieved is gone.

**Fail soft.** A source that is down raises `SourceUnavailable`, which the CLI
records as a gap in the run manifest and steps over. Any other exception is a
bug and is allowed to look like one.

**Ask before downloading.** A fetch that is handed the validators a previous run
recorded sends them as a conditional request, and raises `NotModified` if the
source says our copy is current -- one round trip and no payload instead of the
whole file again (docs/02). That is what makes a re-run cheap enough to hand to
something that retries.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kelpcompare.storage import Zones


class NotModified(Exception):
    """The source says our copy is current, and sent no body to prove it.

    Raised by `fetch` when a conditional request comes back `304`. Not an
    outage and not an error: it is the fetch succeeding at zero cost, and the
    CLI records the window as `unchanged` and moves on.

    Deliberately not a subclass of `SourceUnavailable`. That exception means a
    gap in the record and is noted as one; this means the opposite -- the record
    is complete and up to date -- and conflating them would put a phantom hole
    in every manifest of every re-run.
    """


class SourceUnavailable(Exception):
    """Upstream did not answer, or answered with something that is not data.

    The one failure a run is expected to survive: a missing NDBC month must
    never block a Kelp Watch update (docs/01 §5). Raised by `fetch`, recorded by
    the CLI as a manifest gap, never fatal.
    """


@dataclass(frozen=True)
class Payload:
    """One retrieval: the bytes exactly as they arrived, and where from.

    `body` is bytes rather than text on purpose. Decoding is a parsing decision
    -- the archives are gzipped, and a source that changes encoding should be
    caught by the parser rather than mangled by the fetcher on the way in.

    `etag` and `last_modified` are what the server said this version is called.
    They are carried on the payload rather than recorded by the fetcher, because
    the fetcher does not know whether the window went on to ingest -- and a
    validator recorded before that is a validator that can skip a window whose
    rows never landed (see `fetchers.cache`).
    """

    source: str
    station: str
    label: str
    url: str
    body: bytes
    retrieved_at: datetime
    etag: str | None = None
    last_modified: str | None = None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def decode(self) -> str:
        """The payload as text.

        Latin-1 rather than UTF-8: these are fixed-width ASCII tables, and a
        stray high byte in one row must not cost the run the whole file. It
        cannot raise, which is what makes a malformed row a parsing problem
        visible in the data instead of an exception at the boundary.
        """
        return self.body.decode("latin-1")


def new_payload(
    source: str,
    station: str,
    label: str,
    url: str,
    body: bytes,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> Payload:
    """A `Payload` stamped with the retrieval time."""
    return Payload(
        source=source,
        station=station,
        label=label,
        url=url,
        body=body,
        retrieved_at=datetime.now(UTC),
        etag=etag,
        last_modified=last_modified,
    )


def land(payload: Payload, zones: Zones) -> Path:
    """Write the untouched payload to `raw/{source}/{station}/`, content-addressed.

    Content-addressed for the same reason `cli._land` is: re-fetching identical
    bytes is a no-op, so a fetcher is idempotent for a given window by
    construction rather than by remembering to check, and two different payloads
    can never collide on one name.

    Never overwrites. The raw zone is append-only forever (hard rule 1), and a
    payload already on disk under its own digest is the same payload -- there is
    nothing a second write could add except risk.
    """
    target = (
        zones.raw_source(payload.source)
        / payload.station
        / f"{payload.digest[:12]}__{payload.label}"
    )
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload.body)
    return target
