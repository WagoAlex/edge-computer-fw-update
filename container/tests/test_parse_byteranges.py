"""Chunk reassembly. Two ways to corrupt a firmware bundle silently, both pinned
here: stripping a byte SET rather than exactly one trailing CRLF, and cutting the
payload where the boundary delimiter happens to occur inside it."""
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


@pytest.mark.parametrize("payload", [
    b"before\r\n--b\r\nafter",              # a whole delimiter, mid-payload
    b"\r\n--b--\r\n",                       # the closing delimiter, mid-payload
    b"x" * 100 + b"\r\n--b\r\n" + b"y" * 100,
])
def test_a_payload_containing_the_boundary_is_not_truncated(payload):
    """A 1.3 GB bundle is arbitrary bytes; sooner or later a chunk contains the
    client's own delimiter. Content-Range states the length - trust that, not
    the first delimiter-looking byte sequence."""
    assert parse_byteranges(wrap(payload),
                            f"multipart/byteranges; boundary={BOUNDARY}") == (0, payload)


def test_offset_comes_from_content_range():
    body = (b"--b\r\nContent-Range: bytes 4096-4099/8192\r\n\r\nABCD"
            b"\r\n--b--\r\n")
    assert parse_byteranges(body, "multipart/byteranges; boundary=b") == (4096, b"ABCD")


def test_rejects_non_multipart():
    assert parse_byteranges(b"nope", "application/octet-stream") is None
