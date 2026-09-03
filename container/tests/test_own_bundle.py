"""Item 5: a bundle that is NOT the embedded one, driven entirely over REST.

The sibling edge-commissioning-service is dropping its `docker run` shim and
will install WDS-supplied bundles through this API, so README section 6.5 has to
be true rather than plausible: activate -> getuploadids -> PATCH /files/{id} ->
start with that id -> the bundle rauc is handed is byte-for-byte what was
uploaded. And the status it polls has to be honest when the install fails.

`rauc` is faked at providers.firmwareupdate's own subprocess seam - the same
seam the other rauc tests use - so this runs anywhere.
"""
import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import api
from providers import firmwareupdate as fw

AUTH = "Basic " + base64.b64encode(b"admin:wago").decode()
JSONAPI = "application/vnd.api+json"
# Not a round number, and containing every byte the chunk reassembly is known to
# be able to eat: 0x0d, 0x0a, 0x2d, and a trailing CRLF inside the payload.
BUNDLE = (bytes(range(256)) * 41)[:10475] + b"\r\n--WAGOFW\r\n"


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def call(url, method="GET", body=None, ctype=JSONAPI):
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("Authorization", AUTH)
    if body:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def run_method(base_url, mid, inargs=None):
    body = json.dumps({"data": {"type": "runs",
                                "attributes": {"inArgs": inargs or {}}}}).encode()
    code, doc = call(f"{base_url}/wda/methods/0-0-firmwareupdate-{mid}/runs"
                     f"?result-behavior=sync", "POST", body)
    return code, doc["data"]["attributes"]


def get_param(base_url, pid):
    code, doc = call(f"{base_url}/wda/parameters/0-0-firmwareupdate-{pid}")
    return doc["data"]["attributes"]["value"] if code == 200 else None


def upload_chunks(base_url, fid, blob, chunk=4096):
    """Exactly the protocol README 6.5 documents: one multipart/byteranges part
    per request, placed by its Content-Range offset."""
    for off in range(0, len(blob), chunk):
        part = blob[off:off + chunk]
        b = b"WAGOFW"
        body = (b"--" + b + b"\r\nContent-Type: application/octet-stream\r\n"
                b"Content-Range: bytes %d-%d/%d\r\n\r\n"
                % (off, off + len(part) - 1, len(blob)) + part
                + b"\r\n--" + b + b"--\r\n")
        code, _ = call(f"{base_url}/files/{fid}", "PATCH", body,
                       "multipart/byteranges; boundary=WAGOFW")
        assert code == 204, f"chunk at {off} rejected with {code}"


class _Proc:
    """Stands in for the `rauc install` Popen: yields progress, then exits."""
    def __init__(self, lines, rc):
        self.stdout, self.returncode = iter(lines), rc

    def wait(self):
        return self.returncode


@pytest.fixture
def rauc(tmp_path, monkeypatch):
    """Fake rauc install. Records the path it was handed so the test can read
    back exactly what would have been flashed."""
    seen = {}
    plan = {"lines": ["  0% installing\n", " 55% writing\n", "100% done\n"], "rc": 0}

    def fake_popen(cmd, **kw):
        seen["cmd"] = list(cmd)
        seen["blob"] = open(cmd[-1], "rb").read()
        return _Proc(plan["lines"], plan["rc"])

    monkeypatch.setattr(fw.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(fw, "STAGE_DIR", str(tmp_path))
    monkeypatch.setattr(fw, "EMBEDDED", str(tmp_path / "nonexistent.raucb"))
    fw.m_clear({})
    yield seen, plan
    fw.m_clear({})


def wait_for_status(base_url, want, tries=200):
    for _ in range(tries):
        if get_param(base_url, "status") == want:
            return True
        threading.Event().wait(0.01)
    return False


# ---- the happy path --------------------------------------------------------

def test_own_bundle_upload_then_start_reaches_rauc_byte_for_byte(base_url, rauc):
    seen, _plan = rauc
    code, attrs = run_method(base_url, "activate")
    assert (code, attrs["executionStatus"]) == (201, "done")
    assert get_param(base_url, "status") == 2                    # Prepared

    _code, attrs = run_method(
        base_url, "getuploadids", {"FileNames": {"value": ["firmware.raucb"]}})
    fid = attrs["outArgs"]["UploadFiles"]["value"][0]

    upload_chunks(base_url, fid, BUNDLE)

    _code, attrs = run_method(base_url, "start",
                              {"UploadFiles": {"value": [fid]}})
    assert attrs["executionStatus"] == "done"
    assert wait_for_status(base_url, 4), "install never reached Unconfirmed"

    # The whole point: what rauc was handed is the uploaded bundle, unmodified.
    assert seen["blob"] == BUNDLE
    assert seen["cmd"][:2] == ["rauc", "install"]
    assert seen["cmd"][2].endswith(fid + ".raucb")
    assert get_param(base_url, "progress") == 100


def test_a_second_bundle_is_not_the_embedded_one(base_url, rauc):
    """An explicit UploadFiles must never fall through to the built-in bundle,
    which is what `api-latest` does not even ship."""
    seen, _plan = rauc
    run_method(base_url, "activate")
    _c, a = run_method(base_url, "getuploadids",
                       {"FileNames": {"value": ["other.raucb"]}})
    fid = a["outArgs"]["UploadFiles"]["value"][0]
    upload_chunks(base_url, fid, b"a different bundle entirely")
    run_method(base_url, "start", {"UploadFiles": {"value": [fid]}})
    assert wait_for_status(base_url, 4)
    assert seen["blob"] == b"a different bundle entirely"


# ---- honesty about failure -------------------------------------------------

def test_a_failed_install_reports_error_not_unconfirmed(base_url, rauc):
    _seen, plan = rauc
    plan["lines"], plan["rc"] = ["  0% installing\n", "failed to write slot\n"], 1
    run_method(base_url, "activate")
    _c, a = run_method(base_url, "getuploadids",
                       {"FileNames": {"value": ["bad.raucb"]}})
    fid = a["outArgs"]["UploadFiles"]["value"][0]
    upload_chunks(base_url, fid, BUNDLE)
    run_method(base_url, "start", {"UploadFiles": {"value": [fid]}})
    assert wait_for_status(base_url, 7), "a failed install must reach Error(7)"
    assert get_param(base_url, "errorcause") == 600               # UpdateFailed
    assert "failed to write slot" in get_param(base_url, "debuginfo")


def test_a_rejected_signature_is_distinguishable_from_any_other_failure(base_url, rauc):
    _seen, plan = rauc
    plan["lines"], plan["rc"] = ["signature verification failed\n"], 1
    run_method(base_url, "activate")
    _c, a = run_method(base_url, "getuploadids",
                       {"FileNames": {"value": ["unsigned.raucb"]}})
    fid = a["outArgs"]["UploadFiles"]["value"][0]
    upload_chunks(base_url, fid, BUNDLE)
    run_method(base_url, "start", {"UploadFiles": {"value": [fid]}})
    assert wait_for_status(base_url, 7)
    assert get_param(base_url, "errorcause") == 200               # SignatureInvalid


def test_start_with_an_unknown_upload_id_is_an_error_not_a_silent_embedded_install(
        base_url, rauc):
    seen, _plan = rauc
    run_method(base_url, "activate")
    _code, attrs = run_method(base_url, "start",
                              {"UploadFiles": {"value": ["deadbeefdeadbeef"]}})
    assert attrs["executionStatus"] == "error"
    assert "unknown upload id" in attrs["detail"]
    assert "cmd" not in seen


def test_upload_to_an_unknown_file_id_is_404(base_url, rauc):
    code, _doc = call(f"{base_url}/files/0123456789abcdef", "PATCH", b"x",
                      "multipart/byteranges; boundary=WAGOFW")
    assert code == 404
