"""Every REST action must reach stdout with a timestamp, and no secret may."""
import base64
import logging
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import api
import wdalog

AUTH = "Basic " + base64.b64encode(b"admin:wago").decode()
# 2026-09-01T10:14:07+02:00
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2} ")


@pytest.fixture
def server_and_log(caplog):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), api.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    caplog.set_level(logging.DEBUG)
    yield f"http://127.0.0.1:{httpd.server_port}", caplog
    httpd.shutdown()


def call(url, method="GET", body=None, auth=True):
    req = urllib.request.Request(url, method=method, data=body)
    if auth:
        req.add_header("Authorization", AUTH)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_timestamp_format_is_iso8601_with_offset():
    rec = logging.LogRecord("wda.http", logging.INFO, "", 0, "hi", None, None)
    line = wdalog._Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s").format(rec)
    assert TS.match(line), line


def test_parameter_read_is_logged_with_user_and_status(server_and_log):
    url, caplog = server_and_log
    call(f"{url}/wda/parameters/0-0-firmwareupdate-status")
    line = [r.getMessage() for r in caplog.records if r.name == "wda.http"][-1]
    assert "admin" in line
    assert "GET /wda/parameters/0-0-firmwareupdate-status" in line
    assert " 200 " in line


def test_failed_method_logs_the_reason_at_warning(server_and_log):
    """The line someone greps for when an update did not happen."""
    url, caplog = server_and_log
    call(f"{url}/wda/methods/0-0-firmwareupdate-start/runs", method="POST",
         body=b'{"data":{"attributes":{"inArgs":{}}}}')
    fail = [r for r in caplog.records
            if r.name == "wda.method" and r.levelno == logging.WARNING]
    assert fail, [r.getMessage() for r in caplog.records]
    msg = fail[-1].getMessage()
    assert "0-0-firmwareupdate-start" in msg and "dsc=95" in msg
    assert "not activated" in msg


def test_successful_method_logs_inarg_names_not_values(server_and_log, tmp_path,
                                                       monkeypatch):
    """inArgs names are useful; values could carry configuration we should not
    scatter through the log."""
    from providers import firmwareupdate as fw
    monkeypatch.setattr(fw, "STAGE_DIR", str(tmp_path))
    url, caplog = server_and_log
    call(f"{url}/wda/methods/0-0-firmwareupdate-clear/runs", method="POST", body=b"{}")
    call(f"{url}/wda/methods/0-0-firmwareupdate-activate/runs", method="POST", body=b"{}")
    caplog.clear()
    call(f"{url}/wda/methods/0-0-firmwareupdate-getuploadids/runs", method="POST",
         body=b'{"data":{"attributes":{"inArgs":{"FileNames":{"value":["secret.raucb"]}}}}}')
    done = [r.getMessage() for r in caplog.records if r.name == "wda.method"]
    assert done and "inArgs=['FileNames']" in done[-1] and done[-1].endswith("done")
    blob = "\n".join(r.getMessage() for r in caplog.records if r.name == "wda.method")
    assert "secret.raucb" not in blob
    call(f"{url}/wda/methods/0-0-firmwareupdate-clear/runs", method="POST", body=b"{}")


def test_failed_auth_is_logged(server_and_log):
    """A 401 never reaches _send(); log_request must catch it anyway."""
    url, caplog = server_and_log
    assert call(f"{url}/wda/parameters/0-0-firmwareupdate-status", auth=False) == 401
    assert any(" 401 " in r.getMessage() for r in caplog.records)


def test_no_credentials_are_ever_logged(server_and_log):
    url, caplog = server_and_log
    call(f"{url}/wda/parameters/0-0-firmwareupdate-status")
    call(f"{url}/wda", auth=False)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "wago" not in blob            # the password
    assert AUTH.split()[1] not in blob   # the base64 blob
    assert "Basic" not in blob


def test_health_and_chunks_are_debug_not_info(server_and_log):
    """A 30s healthcheck must not bury the log."""
    url, caplog = server_and_log
    call(f"{url}/health", auth=False)
    health = [r for r in caplog.records if "/health" in r.getMessage()]
    assert health and all(r.levelno == logging.DEBUG for r in health)


def test_update_state_transitions_reach_stdout(caplog):
    """logline() feeds both the WDA log ring and docker logs."""
    from providers import firmwareupdate as fw
    caplog.set_level(logging.INFO)
    fw.logline("install done -> Unconfirmed (call finish)")
    assert any(r.name == "wda.update" and "Unconfirmed" in r.getMessage()
               for r in caplog.records)
    assert fw._log[-1] == "install done -> Unconfirmed (call finish)"
