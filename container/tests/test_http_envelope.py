"""End-to-end over real HTTP: the wire format a client actually receives."""
import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import api

AUTH = "Basic " + base64.b64encode(b"admin:wago").decode()


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def call(url, method="GET", body=None, auth=True):
    req = urllib.request.Request(url, method=method, data=body)
    if auth:
        req.add_header("Authorization", AUTH)
    if body:
        req.add_header("Content-Type", "application/vnd.api+json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def test_parameter_envelope_matches_the_real_device_shape(base_url):
    code, d = call(f"{base_url}/wda/parameters/0-0-firmwareupdate-status")
    assert code == 200
    assert set(d["data"]["attributes"]) == {"dataType", "dataRank", "path", "value"}
    assert d["data"]["attributes"]["path"] == "FirmwareUpdate/Status"
    assert d["data"]["links"]["self"] == "/wda/parameters/0-0-firmwareupdate-status"
    assert set(d["data"]["relationships"]) == {"definition", "device"}
    assert d["jsonapi"] == {"version": "1.0"}
    assert d["meta"]["version"].endswith("-compat")   # never claim to be real WDA


def test_method_run_returns_201_like_the_spec(base_url):
    code, d = call(f"{base_url}/wda/methods/0-0-firmwareupdate-clear/runs",
                   method="POST", body=b'{"data":{"attributes":{"inArgs":{}}}}')
    assert code == 201
    assert d["data"]["attributes"]["executionStatus"] == "done"


def test_method_error_is_still_201_with_the_wda_error_body(base_url):
    """fw_update.py branches on domainSpecificStatusCode, not the HTTP code."""
    code, d = call(f"{base_url}/wda/methods/0-0-firmwareupdate-start/runs",
                   method="POST", body=b'{"data":{"attributes":{"inArgs":{}}}}')
    attrs = d["data"]["attributes"]
    assert code == 201
    assert (attrs["code"], attrs["domainSpecificStatusCode"]) == ("26", "95")
    assert attrs["executionStatus"] == "error"


def test_spec_endpoint_requires_auth(base_url):
    """A real device serves this anonymously; we deliberately do not."""
    assert call(f"{base_url}/openapi/wda.openapi.json", auth=False)[0] == 401
    code, d = call(f"{base_url}/openapi/wda.openapi.json")
    assert code == 200 and d["openapi"] == "3.1.0"


def test_health_stays_anonymous(base_url):
    assert call(f"{base_url}/health", auth=False) == (200, {"status": "ok"})


def test_unknown_parameter_is_a_jsonapi_error(base_url):
    code, d = call(f"{base_url}/wda/parameters/0-0-nope")
    assert code == 404 and d["errors"][0]["status"] == "404"
