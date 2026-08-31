#!/usr/bin/env python3
"""WDA-compatible firmware-update REST API for the WAGO Edge Computer.

Mirrors WAGO's production WDA surface as closely as a re-implementation can, so
the same clients (e.g. the wago-plc-mcp-server `fw_update.py`) and the same call
sequence work against an x86 edge - but the backend is `rauc` over the host
D-Bus, not WAGO's closed provider.

Faithful to the real WDA:
  * base /wda, JSON:API (application/vnd.api+json), id/type/attributes envelopes
  * GET  /wda                                         service root
  * GET  /wda/parameters/0-0-firmwareupdate-status|-progress|-errorcause
                                                      |-debuginfo|-revertable
  * GET  /wda/parameters/0-0-version-firmwareversion|0-0-identity-ordernumber
  * GET  /wda/parameter-definitions/0-0-firmwareupdate-status|-errorcause/enum
  * POST /wda/methods/0-0-firmwareupdate-<m>/runs?result-behavior=sync
         m = activate|getuploadids|start|finish|clear|cancel|getlastlogentries
  * PATCH /files/{id}   chunked bundle upload (multipart/byteranges, Content-Range)

State machine (mapped to rauc):
  Inactive(0) --activate--> Prepared(2) --getuploadids--> (upload /files)
    --start--> Started(3) --rauc install--> Unconfirmed(4)
    --finish--> Finished(8) (rauc mark-good) --clear--> Inactive(0)
  any failure -> Error(7) with a numbered errorcause.

No OAuth2/PAM: this is the update state machine, not the auth stack. For real
auth put it behind the wda-container (lighttpd + authd) gate.
ponytail: stdlib only. Chunk assembly is by Content-Range offset - the exact
protocol fw_update.py speaks.
"""
import json
import os
import re
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JSONAPI = "application/vnd.api+json"

EMBEDDED = os.environ.get("BUNDLE", "/firmware/bundle.raucb")
STAGE_DIR = os.environ.get("STAGE_DIR", "/docker/rauc-stage")
KEYRING = os.environ.get("KEYRING", "/etc/rauc/keyring.pem")
ORDER = os.environ.get("ORDER_NUMBER", "0752-9xxx")
FW_VERSION = os.environ.get("FIRMWARE_VERSION", "04.01.00")
PORT = int(os.environ.get("PORT", "8080"))

# WDA enums (verified live off a TP600, WDA 1.5.2 - see fw_update.py)
STATUS_NAMES = {0: "Inactive", 1: "Init", 2: "Prepared", 3: "Started",
                4: "Unconfirmed", 5: "Confirmed", 6: "Revert", 7: "Error",
                8: "Finished", 9: "NotAvailable"}
ERROR_CAUSES = {0: "NoError", 100: "InternalError", 101: "AbortByUser",
                102: "AbortInitializationFailed", 103: "AbortCheckSystemFailed",
                200: "SignatureInvalid", 300: "NotEnoughResources",
                301: "StopRuntimeFailed", 400: "SettingsBackupFailed",
                401: "FirmwareBackupFailed", 402: "UserBackupFailed",
                403: "SaveModifiedSettingsFailed", 500: "SettingsRestoreFailed",
                600: "UpdateFailed", 601: "SignatureTooNew", 602: "SignatureTooOld",
                603: "PartitionError", 604: "ErrorRevertNotSupported",
                700: "BootloaderUpdateFailed", 800: "RestartFailed",
                900: "SelftestFailed", 1000: "ConfirmationTimeout"}
# domainSpecificStatusCode values fw_update.py checks for
DSC_NOT_ACTIVATED = "95"
DSC_ALREADY_ACTIVE = "90"

_lock = threading.Lock()
st = {"status": 0, "progress": 0, "errorcause": 0, "debuginfo": "",
      "revertable": True, "uploads": {}, "staged": None}
_log = []  # ring of recent log lines


def logline(msg):
    with _lock:
        _log.append(msg)
        del _log[:-200]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def rauc_info(path):
    r = run(["rauc", "info", "--keyring", KEYRING, path])
    if r.returncode != 0:
        return False, None, None, (r.stderr or r.stdout).strip()
    out = r.stdout
    ver = re.search(r"^Version:\s*'(.*)'", out, re.M)
    comp = re.search(r"^Compatible:\s*'(.*)'", out, re.M)
    return True, (ver.group(1) if ver else None), (comp.group(1) if comp else None), None


def slots():
    r = run(["rauc", "status"])
    booted = re.search(r"^Booted from:\s*(.*)$", r.stdout, re.M)
    active = re.search(r"^Activated:\s*(.*)$", r.stdout, re.M)
    return (booted.group(1).strip() if booted else None,
            active.group(1).strip() if active else None)


def _install_worker(path):
    logline(f"rauc install {path}")
    proc = subprocess.Popen(["rauc", "install", path],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        tail.append(line)
        del tail[:-40]
        m = re.match(r"\s*(\d+)%", line)
        if m:
            with _lock:
                st["progress"] = int(m.group(1))
    proc.wait()
    with _lock:
        if proc.returncode == 0:
            st["status"], st["progress"] = 4, 100        # Unconfirmed - awaiting finish
            logline("install done -> Unconfirmed (call finish)")
        else:
            blob = "\n".join(tail).lower()
            st["status"] = 7                              # Error
            st["errorcause"] = 200 if "signature" in blob else 600
            st["debuginfo"] = "\n".join(tail[-8:])
            logline(f"install FAILED -> errorcause {st['errorcause']}")


# ---- method implementations (return (outArgs|None, err_dsc|None, detail)) ----

def m_activate(inargs):
    with _lock:
        if st["status"] not in (0, 9):
            return None, DSC_ALREADY_ACTIVE, "firmware update already active"
        os.makedirs(STAGE_DIR, exist_ok=True)
        st.update(status=2, progress=0, errorcause=0, debuginfo="", uploads={}, staged=None)
    logline("activate -> Prepared")
    return {}, None, None


def m_getuploadids(inargs):
    names = inargs.get("FileNames", {}).get("value", [])
    with _lock:
        if st["status"] < 2:
            return None, DSC_NOT_ACTIVATED, "firmware update not activated"
        ids = []
        for name in names:
            fid = uuid.uuid4().hex[:16]
            p = os.path.join(STAGE_DIR, fid + ".raucb")
            open(p, "wb").close()
            st["uploads"][fid] = {"name": name, "path": p, "size": 0}
            ids.append(fid)
    logline(f"getuploadids {names} -> {ids}")
    return {"UploadFiles": {"value": ids}}, None, None


def m_start(inargs):
    ids = inargs.get("UploadFiles", {}).get("value", [])
    with _lock:
        if st["status"] < 2:
            return None, DSC_NOT_ACTIVATED, "firmware update not activated"
        if not ids or ids[0] not in st["uploads"]:
            return None, "1", "unknown upload id"
        path = st["uploads"][ids[0]]["path"]
        st.update(status=3, progress=0)                  # Started
    threading.Thread(target=_install_worker, args=(path,), daemon=True).start()
    logline(f"start {ids} -> Started")
    return {}, None, None


def m_finish(inargs):
    with _lock:
        if st["status"] != 4:
            return None, "1", f"not finishable in status {st['status']}"
    r = run(["rauc", "status", "mark-good"])
    with _lock:
        st["status"] = 8                                  # Finished
    logline("finish -> mark-good -> Finished")
    return {}, None, None


def m_clear(inargs):
    with _lock:
        for u in st["uploads"].values():
            try:
                os.remove(u["path"])
            except OSError:
                pass
        st.update(status=0, progress=0, errorcause=0, debuginfo="", uploads={}, staged=None)
    logline("clear -> Inactive")
    return {}, None, None


def m_cancel(inargs):
    with _lock:
        st.update(status=0, errorcause=101, uploads={}, staged=None)  # AbortByUser
    logline("cancel -> Inactive (AbortByUser)")
    return {}, None, None


def m_getlastlogentries(inargs):
    n = inargs.get("EntryCount", {}).get("value", 25)
    with _lock:
        entries = _log[-int(n):]
    return {"Entries": {"value": entries}}, None, None


METHODS = {
    "0-0-firmwareupdate-activate": m_activate,
    "0-0-firmwareupdate-getuploadids": m_getuploadids,
    "0-0-firmwareupdate-start": m_start,
    "0-0-firmwareupdate-finish": m_finish,
    "0-0-firmwareupdate-clear": m_clear,
    "0-0-firmwareupdate-cancel": m_cancel,
    "0-0-firmwareupdate-getlastlogentries": m_getlastlogentries,
}


def param_value(pid):
    with _lock:
        s = dict(st)
    return {
        "0-0-firmwareupdate-status": s["status"],
        "0-0-firmwareupdate-progress": s["progress"],
        "0-0-firmwareupdate-errorcause": s["errorcause"],
        "0-0-firmwareupdate-debuginfo": s["debuginfo"],
        "0-0-firmwareupdate-revertable": s["revertable"],
        "0-0-version-firmwareversion": FW_VERSION,
        "0-0-identity-ordernumber": ORDER,
    }.get(pid)


def parse_byteranges(body, content_type):
    """Extract (offset, data) from one multipart/byteranges part."""
    m = re.search(r"boundary=([^\s;]+)", content_type or "")
    if not m:
        return None
    b = ("--" + m.group(1)).encode()
    for part in body.split(b):
        if b"Content-Range:" not in part:
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        cr = re.search(rb"Content-Range:\s*bytes\s+(\d+)-(\d+)/(\d+)", head)
        if not cr:
            continue
        # strip exactly the one CRLF that separates the payload from the next
        # boundary delimiter - NOT a byte set, or binary chunks ending in
        # 0x0d/0x0a/0x2d would be silently truncated.
        if data.endswith(b"\r\n"):
            data = data[:-2]
        return int(cr.group(1)), data
    return None


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj, ctype=JSONAPI):
        b = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(code)
        if b:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        if b:
            self.wfile.write(b)

    def _param(self, pid):
        v = param_value(pid)
        if v is None:
            return self._send(404, {"errors": [{"status": "404", "detail": pid}]})
        self._send(200, {"data": {"id": pid, "type": "parameters",
                                  "attributes": {"value": v}}})

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/wda", ""):
            return self._send(200, {"data": {"type": "service",
                                    "attributes": {"name": "WAGO Edge WDA (rauc)",
                                                   "version": "1.5.2-compat"}}})
        if path == "/health":
            return self._send(200, {"status": "ok"}, "application/json")
        if path.startswith("/wda/parameters/"):
            return self._param(path.rsplit("/", 1)[-1])
        if path.startswith("/wda/parameter-definitions/") and path.endswith("/enum"):
            pid = path[len("/wda/parameter-definitions/"):-len("/enum")]
            table = {"0-0-firmwareupdate-status": STATUS_NAMES,
                     "0-0-firmwareupdate-errorcause": ERROR_CAUSES}.get(pid)
            if table is None:
                return self._send(404, {"errors": [{"status": "404", "detail": pid}]})
            return self._send(200, {"data": {"id": pid, "type": "parameter-definitions",
                "attributes": {"enum": [{"value": k, "name": v} for k, v in table.items()]}}})
        if path == "/update/status":  # convenience aggregate (non-WDA)
            booted, active = slots()
            with _lock:
                s = dict(st)
            s.pop("uploads", None)
            return self._send(200, {**s, "status_name": STATUS_NAMES.get(s["status"]),
                                    "errorcause_name": ERROR_CAUSES.get(s["errorcause"]),
                                    "booted_slot": booted, "next_boot_slot": active,
                                    "order_number": ORDER, "firmware_version": FW_VERSION},
                              "application/json")
        self._send(404, {"errors": [{"status": "404", "detail": self.path}]})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        m = re.match(r"/wda/methods/(.+)/runs$", path)
        if m:
            mid = m.group(1)
            fn = METHODS.get(mid)
            if not fn:
                return self._send(404, {"errors": [{"status": "404", "detail": mid}]})
            try:
                inargs = json.loads(body or b"{}").get("data", {}).get("attributes", {}).get("inArgs", {})
            except (ValueError, AttributeError):
                inargs = {}
            outargs, dsc, detail = fn(inargs)
            if dsc is not None:  # WDA method-error envelope
                return self._send(200, {"data": {"type": "runs", "attributes": {
                    "code": "26", "domainSpecificStatusCode": dsc,
                    "detail": detail or "method could not be invoked",
                    "executionStatus": "error"}}})
            return self._send(200, {"data": {"type": "runs", "attributes": {
                "outArgs": outargs or {}, "executionStatus": "done"}}})
        self._send(404, {"errors": [{"status": "404", "detail": self.path}]})

    def do_PATCH(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        m = re.match(r"/files/([0-9a-f]+)$", path)
        if not m:
            return self._send(404, {"errors": [{"status": "404", "detail": self.path}]})
        fid = m.group(1)
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        with _lock:
            up = st["uploads"].get(fid)
        if not up:
            return self._send(404, {"errors": [{"status": "404", "detail": "unknown upload id"}]})
        parsed = parse_byteranges(body, self.headers.get("Content-Type", ""))
        if not parsed:
            return self._send(400, {"errors": [{"status": "400", "detail": "expected multipart/byteranges"}]})
        offset, data = parsed
        with open(up["path"], "r+b") as f:
            f.seek(offset)
            f.write(data)
        with _lock:
            up["size"] = max(up["size"], offset + len(data))
        self._send(204, None)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ok, ver, comp, _ = rauc_info(EMBEDDED) if os.path.isfile(EMBEDDED) else (False, None, None, None)
    print(f"WDA-compatible firmware-update API on :{PORT}  order={ORDER} "
          f"fw={FW_VERSION}  embedded={'yes' if ok else 'no'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
