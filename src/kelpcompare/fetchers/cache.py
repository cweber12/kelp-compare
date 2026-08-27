"""What we already have, so a re-run does not ask for it again (docs/02).

A pulled source is re-fetched every time a run touches its window: `land` only
notices an existing copy once the bytes have arrived, which is too late to save
the download. HTTP has an answer to that and NDBC implements it -- a response
carries an `ETag` and a `Last-Modified`, and handing one back on the next request
gets `304 Not Modified` with an empty body if nothing changed. This module is
where the tokens are kept between runs.

**This is a cache, not a record.** Nothing derived depends on it, deleting it is
always safe, and losing it costs exactly one re-download. That is why it lives in
its own zone rather than in `raw/` -- which hard rule 1 reserves for landings and
manifests -- and rather than in the run manifests, which would make an audit
record into a *read* dependency of ingest, where a deleted manifest would change
behaviour instead of merely losing history.

The corollary is that every failure here is soft. An absent file, a truncated
one, a JSON syntax error left by a half-written edit: all read as "we know
nothing about this URL", and the run pays for a download it might not have
needed. There is no failure mode worth raising over, because there is nothing
here that cannot be re-earned from the source.

**A validator means the window was fully ingested at that version**, which is
why `remember` is called after the observation rows are written rather than when
the payload lands. The two are not the same moment: a landing whose parse or
write then failed leaves bytes on disk and no rows in the zone, and a validator
recorded at landing time would let the next run `304` straight past it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from kelpcompare.storage import Zones

#: The cache file's own version. Bumped if the shape below ever changes, so an
#: older file is discarded rather than misread -- cheap, because discarding it
#: costs one download.
FORMAT = 1


def validators_for(zones: Zones, url: str) -> dict[str, str]:
    """The `ETag` and `Last-Modified` last recorded for one URL.

    Empty when nothing is known, which is the answer for a URL never fetched, a
    cache that does not exist yet, and a cache that will not parse. All three
    mean the same thing to a caller: ask for the whole file.
    """
    entry = _load(zones).get(url, {})
    return {
        key: str(entry[key])
        for key in ("etag", "last_modified")
        if isinstance(entry, dict) and entry.get(key)
    }


def remember(
    zones: Zones, url: str, *, etag: str | None = None, last_modified: str | None = None
) -> None:
    """Record what the server said about the version we just finished ingesting.

    A URL the server described with neither validator is *forgotten* rather than
    stored empty: an entry that cannot condition a request is indistinguishable
    from no entry at the point of use, and keeping it would let a server that
    stopped sending validators leave a stale token behind.
    """
    entries = _load(zones)
    validators = {
        key: value for key, value in (("etag", etag), ("last_modified", last_modified)) if value
    }
    if not validators:
        entries.pop(url, None)
    else:
        entries[url] = {**validators, "recorded_at": _now()}
    _store(zones, entries)


def forget(zones: Zones, url: str) -> None:
    """Drop one URL, so the next run fetches it whole.

    Not used by the pipeline. It exists for the operator who suspects the cache
    of hiding an upstream change and wants to re-earn one window without
    deleting what is known about every other.
    """
    entries = _load(zones)
    if entries.pop(url, None) is not None:
        _store(zones, entries)


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------


def _load(zones: Zones) -> dict[str, dict]:
    path = zones.http_validators
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Absent, unreadable, or not JSON. All mean the same: know nothing.
        return {}
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        return {}
    entries = payload.get("urls")
    return entries if isinstance(entries, dict) else {}


def _store(zones: Zones, entries: dict[str, dict]) -> None:
    """Stage, then move into place, as every other write in this project does.

    An interrupted write leaves the previous cache intact rather than a
    truncated file -- which would read as empty and cost a re-download, so this
    is tidiness rather than safety. It costs one line to be consistent with the
    zones that do depend on it.
    """
    path = zones.http_validators
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".{path.name}.writing"
    payload = {"format": FORMAT, "urls": dict(sorted(entries.items()))}
    staging.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    staging.replace(path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = ["FORMAT", "forget", "remember", "validators_for"]
