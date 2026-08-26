"""The shared fetcher contract: payloads land untouched, and land only once."""

import pytest

from kelpcompare.fetchers.base import Payload, SourceUnavailable, land, new_payload
from kelpcompare.storage import Zones


def _payload(body: bytes = b"#YY  MM\n2023 06\n", label: str = "LJAC1.txt") -> Payload:
    return new_payload("ndbc", "LJAC1", label, "https://example.invalid/LJAC1.txt", body)


def test_landing_writes_the_bytes_verbatim(tmp_path):
    zones = Zones.at(tmp_path)
    body = b"#YY  MM DD\r\n2023 06 20\r\n"  # CRLF, as a fixed-width feed may send

    landed = land(_payload(body), zones)

    assert landed.read_bytes() == body
    assert landed.parent == zones.raw_source("ndbc") / "LJAC1"


def test_landing_is_content_addressed_so_a_refetch_is_a_no_op(tmp_path):
    zones = Zones.at(tmp_path)

    first = land(_payload(), zones)
    second = land(_payload(), zones)

    assert first == second
    assert len(list(first.parent.iterdir())) == 1


def test_two_different_payloads_never_collide(tmp_path):
    zones = Zones.at(tmp_path)

    first = land(_payload(b"one"), zones)
    second = land(_payload(b"two"), zones)

    assert first != second
    assert {p.read_bytes() for p in first.parent.iterdir()} == {b"one", b"two"}


def test_landing_never_overwrites_an_existing_file(tmp_path):
    """Raw is append-only forever (hard rule 1), digest collision or not."""
    zones = Zones.at(tmp_path)
    landed = land(_payload(), zones)
    landed.write_bytes(b"an operator edited this")

    again = land(_payload(), zones)

    assert again == landed
    assert landed.read_bytes() == b"an operator edited this"


def test_decode_survives_a_stray_high_byte(tmp_path):
    """One bad byte is a data problem to see, not an exception at the boundary."""
    assert _payload(b"2023 06 20 \xff 15.1").decode().endswith("15.1")


def test_source_unavailable_is_an_exception_the_cli_can_catch():
    with pytest.raises(SourceUnavailable):
        raise SourceUnavailable("station page returned 503")
