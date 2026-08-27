"""The HTTP validator cache (docs/02, docs/03 "The cache").

Everything here is about one property: **this file can be wrong, missing or
corrupt and the only cost is a download.** So the tests are mostly about the
ways it can be broken, each asserting that the answer is "know nothing" rather
than an exception — a cache that can fail a run is worse than no cache.

The one thing it must not do is claim to know something it does not, because a
false validator makes an unchanged answer out of a changed file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kelpcompare.fetchers import cache
from kelpcompare.storage import Zones

URL = "https://www.ndbc.noaa.gov/data/historical/stdmet/ljac1h2023.txt.gz"
OTHER = "https://www.ndbc.noaa.gov/data/realtime2/LJAC1.txt"
ETAG = '"e3dc8-52be5aaf150c0"'
MODIFIED = "Tue, 16 Feb 2016 16:31:39 GMT"


@pytest.fixture
def zones(tmp_path) -> Zones:
    return Zones.at(tmp_path / "data")


def corrupt(zones: Zones, text: str) -> None:
    zones.http_validators.parent.mkdir(parents=True, exist_ok=True)
    zones.http_validators.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_what_was_remembered_comes_back(zones):
    cache.remember(zones, URL, etag=ETAG, last_modified=MODIFIED)
    assert cache.validators_for(zones, URL) == {"etag": ETAG, "last_modified": MODIFIED}


def test_either_validator_alone_round_trips(zones):
    """A server may send one and not the other; the request is conditional on
    whichever it gave."""
    cache.remember(zones, URL, etag=ETAG)
    assert cache.validators_for(zones, URL) == {"etag": ETAG}

    cache.remember(zones, OTHER, last_modified=MODIFIED)
    assert cache.validators_for(zones, OTHER) == {"last_modified": MODIFIED}


def test_urls_are_kept_apart(zones):
    cache.remember(zones, URL, etag=ETAG)
    cache.remember(zones, OTHER, etag='"different"')

    assert cache.validators_for(zones, URL)["etag"] == ETAG
    assert cache.validators_for(zones, OTHER)["etag"] == '"different"'


def test_remembering_again_supersedes_rather_than_appends(zones):
    cache.remember(zones, URL, etag=ETAG)
    cache.remember(zones, URL, etag='"newer"')

    assert cache.validators_for(zones, URL) == {"etag": '"newer"'}


def test_when_the_recording_happened_is_kept_beside_it(zones):
    """Not used to condition anything -- it is there so an operator looking at
    the file can tell a token from last week from one from last year."""
    cache.remember(zones, URL, etag=ETAG)
    entry = json.loads(zones.http_validators.read_text())["urls"][URL]

    assert entry["recorded_at"].endswith("+00:00")
    assert "etag" in entry


def test_forgetting_one_url_leaves_the_others(zones):
    cache.remember(zones, URL, etag=ETAG)
    cache.remember(zones, OTHER, etag='"other"')

    cache.forget(zones, URL)

    assert cache.validators_for(zones, URL) == {}
    assert cache.validators_for(zones, OTHER) == {"etag": '"other"'}


def test_forgetting_a_url_nobody_recorded_is_not_an_error(zones):
    cache.forget(zones, URL)
    assert cache.validators_for(zones, URL) == {}


# --------------------------------------------------------------------------
# Every way it can be broken reads as "know nothing"
# --------------------------------------------------------------------------


def test_an_absent_cache_knows_nothing(zones):
    assert cache.validators_for(zones, URL) == {}
    assert not zones.cache.exists()  # ...and reading did not create one


def test_a_url_never_recorded_knows_nothing(zones):
    cache.remember(zones, OTHER, etag=ETAG)
    assert cache.validators_for(zones, URL) == {}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json at all",
        '{"format": 1, "urls": ',  # truncated by an interrupted write
        "[]",
        '{"urls": {}}',  # no format marker
        '{"format": 99, "urls": {"x": {"etag": "y"}}}',  # a shape we do not know
    ],
)
def test_a_cache_that_will_not_parse_reads_as_empty(zones, text):
    """A cache that can fail a run is worse than no cache: everything in here can
    be re-earned from the source, so nothing in here is worth raising over."""
    corrupt(zones, text)
    assert cache.validators_for(zones, URL) == {}


def test_a_malformed_entry_reads_as_empty_without_touching_its_neighbours(zones):
    corrupt(zones, json.dumps({"format": 1, "urls": {URL: "a string, not an entry"}}))
    assert cache.validators_for(zones, URL) == {}


def test_an_entry_with_empty_validators_reads_as_empty(zones):
    corrupt(zones, json.dumps({"format": 1, "urls": {URL: {"etag": "", "last_modified": None}}}))
    assert cache.validators_for(zones, URL) == {}


def test_writing_over_a_corrupt_cache_repairs_it(zones):
    corrupt(zones, "not json at all")
    cache.remember(zones, URL, etag=ETAG)

    assert cache.validators_for(zones, URL) == {"etag": ETAG}


def test_a_server_that_offers_no_validator_leaves_no_entry(zones):
    """An entry that cannot condition a request is indistinguishable from no
    entry at the point of use -- and storing one would let a server that stopped
    sending validators leave a stale token behind."""
    cache.remember(zones, URL, etag=ETAG)
    cache.remember(zones, URL, etag=None, last_modified=None)

    assert cache.validators_for(zones, URL) == {}
    assert URL not in json.loads(zones.http_validators.read_text())["urls"]


# --------------------------------------------------------------------------
# The file itself
# --------------------------------------------------------------------------


def test_the_cache_lives_outside_every_zone_that_carries_a_record(zones):
    """`raw/` is append-only and holds landings and manifests; this is neither,
    and it must not look like either (hard rule 1)."""
    cache.remember(zones, URL, etag=ETAG)
    written = zones.http_validators

    assert written.parent == zones.cache
    assert zones.raw not in written.parents
    assert zones.observations not in written.parents
    assert zones.features not in written.parents


def test_no_staging_file_survives_a_write(zones):
    cache.remember(zones, URL, etag=ETAG)
    assert [p.name for p in zones.cache.iterdir()] == ["http-validators.json"]


def test_the_file_is_readable_by_a_human_and_ordered(zones):
    """An operator has to be able to look at this and see which URL is pinned to
    which version, since the whole mechanism is invisible otherwise."""
    cache.remember(zones, OTHER, etag='"second"')
    cache.remember(zones, URL, etag=ETAG)

    payload = json.loads(zones.http_validators.read_text())
    assert payload["format"] == cache.FORMAT
    assert list(payload["urls"]) == sorted([URL, OTHER])
    assert "\n" in zones.http_validators.read_text()  # indented, not one line


def test_a_cache_under_a_data_root_is_gitignored_by_the_existing_rule():
    """`data/*` is ignored except `data/registry/`, so a contact address or an
    access pattern in here can never reach the public repo by accident."""
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
    assert "data/*" in ignore
    assert "!data/cache" not in ignore
