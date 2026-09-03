"""Item 3: the demand for PFC300 parity has to be visible.

Parity here grows on demand - an id is implemented when a real client needs one.
That is only a policy and not an excuse if the demand is recorded, so every id a
caller asks for and this build does not serve is logged once, at WARNING.

Deliberately NOT tested for, because it deliberately does not exist: a registry
of unimplemented ids, a stub generator, or any response other than 404.
"""
import base64
import json
import logging
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import api

AUTH = "Basic " + base64.b64encode(b"admin:wago").decode()
JSONAPI = "application/vnd.api+json"


@pytest.fixture
def server(caplog):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    caplog.set_level(logging.DEBUG)
    api._ASKED_FOR.clear()
    yield f"http://127.0.0.1:{httpd.server_port}", caplog
    httpd.shutdown()
    api._ASKED_FOR.clear()


def call(url, method="GET", body=None):
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("Authorization", AUTH)
    if body:
        req.add_header("Content-Type", JSONAPI)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


def warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and "unimplemented" in r.getMessage()]


def test_an_unserved_parameter_is_recorded(server):
    url, caplog = server
    assert call(f"{url}/wda/parameters/0-0-clock-timezone") == 404
    lines = warnings(caplog)
    assert len(lines) == 1
    assert "0-0-clock-timezone" in lines[0] and "parameter" in lines[0]


def test_an_unserved_method_is_recorded(server):
    url, caplog = server
    body = json.dumps({"data": {"type": "runs", "attributes": {"inArgs": {}}}}).encode()
    assert call(f"{url}/wda/methods/0-0-device-restart/runs", "POST", body) == 404
    lines = warnings(caplog)
    assert len(lines) == 1 and "0-0-device-restart" in lines[0] and "method" in lines[0]


def test_a_write_to_a_read_only_id_is_recorded(server):
    """The twin PATCHing an id we serve read-only is the single most useful
    entry in this log - it is a parameter WDS actually manages."""
    url, caplog = server
    body = json.dumps({"data": {"id": "0-0-networking-hostname-currentname",
                                "type": "parameters",
                                "attributes": {"value": "x"}}}).encode()
    assert call(f"{url}/wda/parameters/0-0-networking-hostname-currentname",
                "PATCH", body) == 404
    lines = warnings(caplog)
    assert len(lines) == 1 and "0-0-networking-hostname-currentname" in lines[0]


def test_a_parameter_definition_is_recorded_separately_from_its_parameter(server):
    url, caplog = server
    call(f"{url}/wda/parameters/0-0-clock-timezone")
    call(f"{url}/wda/parameter-definitions/0-0-clock-timezone")
    assert len(warnings(caplog)) == 2


def test_it_is_deduplicated(server):
    """A polling client asks for the same missing id every cycle; the backlog is
    a list of ids, not a flood."""
    url, caplog = server
    for _ in range(20):
        call(f"{url}/wda/parameters/0-0-clock-timezone")
    assert len(warnings(caplog)) == 1


def test_served_ids_are_never_recorded(server):
    url, caplog = server
    assert call(f"{url}/wda/parameters/0-0-firmwareupdate-status") == 200
    assert call(f"{url}/wda/parameters/0-0-firmwareupdate-activationstate") == 200
    assert warnings(caplog) == []


def test_the_response_is_still_a_plain_404(server):
    """Recording the demand must not change the answer - no stub, no default."""
    url, _caplog = server
    assert call(f"{url}/wda/parameters/0-0-clock-timezone") == 404
