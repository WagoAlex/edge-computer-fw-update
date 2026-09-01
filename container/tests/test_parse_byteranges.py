"""Chunk reassembly must strip exactly one trailing CRLF - never a byte set, or
binary firmware chunks ending in 0x0d/0x0a/0x2d get silently corrupted."""
import pytest

from api import parse_byteranges

BOUNDARY = "b"


def wrap(payload):
    return ((f"--{BOUNDARY}\r\nContent-Range: bytes 0-{len(payload) - 1}/"
             f"{len(payload)}\r\n\r\n").encode() + payload
            + f"\r\n--{BOUNDARY}--\r\n".encode())


@pytest.mark.parametrize("payload", [b"\x0d", b"\x0a", b"-", b"\r\n", bytes(range(256))])
def test_roundtrip_binary_payloads(payload):
    assert parse_byteranges(wrap(payload), f"multipart/byteranges; boundary={BOUNDARY}") \
        == (0, payload)


def test_rejects_non_multipart():
    assert parse_byteranges(b"nope", "application/octet-stream") is None
