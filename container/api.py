#!/usr/bin/env python3
"""WDA-compatible REST API for the WAGO Edge Computer.

Mirrors WAGO's production WDA surface as closely as a re-implementation can, so
the same clients (e.g. the wago-plc-mcp-server `fw_update.py`) and the same call
sequence work against an x86 edge - but the backends are `rauc` over the host
D-Bus and the kernel's own /sys and /proc views, not WAGO's closed provider.

This file is transport only: HTTP, JSON:API envelopes, Basic auth, TLS. Every
parameter and method lives in a provider under `providers/` and is registered
there. Parameter IDs come from the FW31 cassette
(`wago-plc-mcp-server/docs/edge-fw31-parameters-raw.json`) - none are invented.

  GET   /wda                                  service root
  GET   /wda/parameters/<id>                  see providers/ for the ID map
  GET   /wda/parameter-definitions/<id>/enum  enum members where WDA has them
  POST  /wda/methods/<id>/runs?result-behavior=sync    -> 201, as the spec says
  PATCH /files/{id}                           chunked bundle upload
  GET   /openapi/wda.openapi.json             what this build actually implements
  GET   /health                               auth-exempt liveness

Response envelopes follow WAGO's own OpenAPI 3.1 document (WDA 1.5.2, served by
every real device at /openapi/wda.openapi.json and diffed against a live CC100 on
2026-09-01): attributes carry dataType/dataRank/path beside the value, resources
carry links/relationships, documents carry jsonapi and meta. This is a strict
subset of the 40-path spec - discovery collections are not implemented - so the
spec we serve describes only these paths and says so.

Everything served today is read-only apart from the firmware-update state
machine; writable `custom*`/`static*` parameters are Phase 3.

ponytail: stdlib only. Chunk assembly is by Content-Range offset - the exact
protocol fw_update.py speaks.
"""
import base64
import hmac
import json
import logging
import os
import re
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import providers
import openapi
import wdalog
from providers import meta
from providers import firmwareupdate as fw

JSONAPI = "application/vnd.api+json"
WDA_VERSION = "1.5.2-compat"
JSONAPI_VERSION = "1.0"

ORDER = os.environ.get("ORDER_NUMBER", "0752-9xxx")
FW_VERSION = os.environ.get("FIRMWARE_VERSION", "04.01.00")
PORT = int(os.environ.get("PORT", "8443"))

# Auth/TLS: same posture as PFC/CC WDA - HTTPS with HTTP Basic auth, self-signed.
# fw_update.py (https + auth=(user,pass) + verify=False) drives this unchanged.
WDA_USER = os.environ.get("WDA_USER", "admin")
WDA_PASSWORD = os.environ.get("WDA_PASSWORD", "wago")
WDA_AUTH = os.environ.get("WDA_AUTH", "true").lower() not in ("0", "false", "no")
WDA_TLS = os.environ.get("WDA_TLS", "true").lower() not in ("0", "false", "no")
TLS_CERT = os.environ.get("TLS_CERT", "/run/wda/cert.pem")
TLS_KEY = os.environ.get("TLS_KEY", "/run/wda/key.pem")


def check_auth(header):
    """HTTP Basic against WDA_USER/WDA_PASSWORD; constant-time compare."""
    if not WDA_AUTH:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(user, WDA_USER) and hmac.compare_digest(pw, WDA_PASSWORD)


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

    def _404(self, detail):
        self._send(404, {"errors": [{"status": "404", "detail": detail}]})

    def _doc(self, data, self_link):
        """Wrap a resource as a JSON:API document the way a real WDA does."""
        return {"data": data, "jsonapi": {"version": JSONAPI_VERSION},
                "links": {"self": self_link},
                "meta": {"version": WDA_VERSION,
                         "doc": "/openapi/wda.openapi.json"}}

    def _param(self, pid):
        v = providers.param_value(pid)
        if v is None:
            return self._404(pid)
        link = f"/wda/parameters/{pid}"
        attrs = meta.describe(pid, v)
        attrs["value"] = v
        self._send(200, self._doc({
            "id": pid, "type": "parameters", "attributes": attrs,
            "links": {"self": link},
            "relationships": {
                "definition": {"links": {"related": f"{link}/definition"}},
                "device": {"links": {"related": f"{link}/device"}}}}, link))

    def _authed(self):
        """Enforce Basic auth on everything except /health (auth-exempt, like the
        MCP server's /health). Returns False and sends 401 if unauthorized."""
        if self.path.split("?", 1)[0].rstrip("/") == "/health":
            return True
        if check_auth(self.headers.get("Authorization")):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="WDA"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        self._t0 = time.monotonic()
        if not self._authed():
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/wda", ""):
            # WDA service root. "-compat" in meta.version flags the rauc-backed
            # re-implementation - a client must not mistake this for real WDA.
            return self._send(200, self._doc(
                {"id": "0-0", "type": "devices",
                 "attributes": {"orderNumber": ORDER, "firmwareVersion": FW_VERSION},
                 "links": {"self": "/wda"}}, "/wda"))
        if path == "/openapi/wda.openapi.json":
            # Generated from the provider registry, so the spec cannot drift from
            # the code. Auth-gated: a real device serves this anonymously, we do
            # not - there is no client compatibility reason to hand the shape of
            # the API to an unauthenticated caller.
            return self._send(200, openapi.document(ORDER, FW_VERSION, WDA_VERSION),
                              "application/json")
        if path == "/health":
            return self._send(200, {"status": "ok"}, "application/json")
        if path.startswith("/wda/parameters/"):
            return self._param(path[len("/wda/parameters/"):])
        if path.startswith("/wda/parameter-definitions/") and not path.endswith("/enum"):
            # How a client discovers what it may write. `writeable` comes from
            # the same WRITES registry the PATCH handler dispatches on, so the
            # two can never disagree.
            pid = path[len("/wda/parameter-definitions/"):]
            v = providers.param_value(pid)
            if v is None:
                return self._404(pid)
            attrs = meta.describe(pid, v)
            attrs["writeable"] = providers.writable(pid)
            attrs["userSetting"] = attrs["writeable"]
            link = f"/wda/parameter-definitions/{pid}"
            return self._send(200, self._doc(
                {"id": pid, "type": "parameter-definitions", "attributes": attrs,
                 "links": {"self": link}}, link))
        if path.startswith("/wda/parameter-definitions/") and path.endswith("/enum"):
            pid = path[len("/wda/parameter-definitions/"):-len("/enum")]
            table = providers.ENUMS.get(pid)
            if table is None:
                return self._404(pid)
            link = f"/wda/parameter-definitions/{pid}/enum"
            return self._send(200, self._doc(
                {"id": pid, "type": "parameter-definitions",
                 "attributes": {"enum": [{"value": k, "name": v}
                                         for k, v in table.items()]},
                 "links": {"self": link}}, link))
        self._404(self.path)

    def do_POST(self):
        self._t0 = time.monotonic()
        if not self._authed():
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        m = re.match(r"/wda/methods/(.+)/runs$", path)
        if not m:
            return self._404(self.path)
        mid = m.group(1)
        fn = providers.METHODS.get(mid)
        if not fn:
            return self._404(mid)
        try:
            inargs = json.loads(body or b"{}").get("data", {}).get("attributes", {}).get("inArgs", {})
        except (ValueError, AttributeError):
            inargs = {}
        outargs, dsc, detail = fn(inargs)
        # One line per invocation carrying what the generic request line cannot:
        # which inArgs were supplied (names only, never values) and the outcome.
        # A failure is a WARNING - it is the line someone greps for at 3am.
        if dsc is None:
            wdalog.method.info("%s inArgs=%s done", mid, sorted(inargs) or "[]")
        else:
            wdalog.method.warning("%s inArgs=%s ERROR dsc=%s %s",
                                  mid, sorted(inargs) or "[]", dsc, detail)
        link = f"/wda/methods/{mid}/runs"
        # 201, per the spec - a run is a created resource. fw_update.py ignores
        # the code and reads the body, so this is safe for the proven client.
        if dsc is not None:  # WDA method-error envelope
            return self._send(201, self._doc({"type": "runs", "attributes": {
                "code": "26", "domainSpecificStatusCode": dsc,
                "detail": detail or "method could not be invoked",
                "executionStatus": "error"}}, link))
        self._send(201, self._doc({"type": "runs", "attributes": {
            "outArgs": outargs or {}, "executionStatus": "done"}}, link))

    # ---- parameter writes (Phase 3) ---------------------------------------
    # Contract read off a live CC100 (genuine WDA 1.5.2) on 2026-09-02, not
    # invented: PATCH /wda/parameters/{id} with {"data":{"id","type","attributes"
    # :{"value"}}}, 204 applied verbatim, 200 applied with a modified value in
    # the body, 400 bad value, 404 unknown or read-only, 415 wrong content type.
    # 202 (deferred, "the change would prevent a response") is WAGO's code for a
    # write that drops the connection it arrived on - nothing writable here can
    # do that, so it is not emitted; the branch structure leaves room for it.

    def _write_one(self, res):
        """Apply one JSON:API parameter resource. (status, effective|detail)."""
        if not isinstance(res, dict):
            return 400, "each data entry must be an object"
        pid = res.get("id")
        attrs = res.get("attributes")
        if not isinstance(pid, str) or not isinstance(attrs, dict) or "value" not in attrs:
            return 400, "data.id and data.attributes.value are required"
        if not providers.writable(pid):
            # 404 for read-only too: a client learns writability from the
            # definition, and a value that cannot be written has no PATCH
            # resource to address.
            return 404, pid
        try:
            effective = providers.set_param(pid, attrs["value"])
        except providers.WriteError as e:
            wdalog.write.warning("%s by %s REJECTED %s: %s",
                                 pid, self._user(), e.status, e.detail)
            return e.status, e.detail
        modified = effective != attrs["value"]
        wdalog.write.info("%s by %s applied%s", pid, self._user(),
                          " (value normalised)" if modified else "")
        return (200 if modified else 204), effective

    def _param_doc(self, pid):
        v = providers.param_value(pid)
        link = f"/wda/parameters/{pid}"
        attrs = meta.describe(pid, v)
        attrs["value"] = v
        return {"id": pid, "type": "parameters", "attributes": attrs,
                "links": {"self": link}}

    def _patch_parameters(self, path, body):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype and ctype != JSONAPI:
            return self._send(415, {"errors": [{"status": "415",
                                                "detail": f"expected {JSONAPI}"}]})
        try:
            data = json.loads(body or b"{}").get("data")
        except ValueError:
            return self._send(400, {"errors": [{"status": "400",
                                                "detail": "body is not JSON"}]})
        bulk = path == "/wda/parameters"
        if bulk:
            if not isinstance(data, list):
                return self._send(400, {"errors": [{"status": "400",
                                                    "detail": "data must be an array"}]})
            resources = data
        else:
            pid = path[len("/wda/parameters/"):]
            if not isinstance(data, dict):
                return self._send(400, {"errors": [{"status": "400",
                                                    "detail": "data must be an object"}]})
            data.setdefault("id", pid)
            if data["id"] != pid:
                return self._send(400, {"errors": [{"status": "400",
                                                    "detail": "data.id does not match the URL"}]})
            resources = [data]
        # Validate-then-apply is per resource, so a bulk write is not atomic;
        # WDA is not either - it answers 200 with only the modified members.
        modified, worst, detail = [], 204, None
        for res in resources:
            status, result = self._write_one(res)
            if status >= 400:
                worst, detail = status, result
                break
            if status == 200:
                modified.append(self._param_doc(res["id"]))
        if worst >= 400:
            msg = f"parameter not writable: {detail}" if worst == 404 else detail
            return self._send(worst, {"errors": [{"status": str(worst), "detail": msg}]})
        if not modified:
            return self._send(204, None)
        payload = modified if bulk else modified[0]
        return self._send(200, {"data": payload, "jsonapi": {"version": JSONAPI_VERSION},
                                "links": {"self": path},
                                "meta": {"version": WDA_VERSION,
                                         "doc": "/openapi/wda.openapi.json"}})

    def do_PATCH(self):
        self._t0 = time.monotonic()
        if not self._authed():
            return
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/wda/parameters" or path.startswith("/wda/parameters/"):
            n = int(self.headers.get("Content-Length", 0))
            return self._patch_parameters(path, self.rfile.read(n) if n else b"")
        m = re.match(r"/files/([0-9a-f]+)$", path)
        if not m:
            return self._404(self.path)
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        up = fw.upload(m.group(1))
        if not up:
            return self._404("unknown upload id")
        parsed = parse_byteranges(body, self.headers.get("Content-Type", ""))
        if not parsed:
            return self._send(400, {"errors": [{"status": "400",
                                                "detail": "expected multipart/byteranges"}]})
        offset, data = parsed
        with open(up["path"], "r+b") as f:
            f.seek(offset)
            f.write(data)
        fw.note_upload_size(up, offset + len(data))
        # A 1.3 GB bundle is over a thousand chunks: one INFO line per 100 keeps
        # the upload visible in `docker logs` without burying everything else.
        up["chunks"] = up.get("chunks", 0) + 1
        total = offset + len(data)
        if up["chunks"] == 1 or up["chunks"] % 100 == 0:
            wdalog.http.info("upload %s chunk %d, %.1f MiB written",
                             m.group(1), up["chunks"], total / 1048576)
        else:
            wdalog.http.debug("upload %s chunk %d offset %d len %d",
                              m.group(1), up["chunks"], offset, len(data))
        self._send(204, None)

    # ---- logging ----------------------------------------------------------
    # log_request() is called by send_response() for every response, including
    # the 401s that never reach _send(), so nothing can slip past it.

    def _user(self):
        """Username from the Authorization header, for the audit line. Never
        the password - only the part before the colon."""
        h = self.headers.get("Authorization") or ""
        if not h.startswith("Basic "):
            return "-"
        try:
            return base64.b64decode(h[6:]).decode("utf-8").partition(":")[0] or "-"
        except (ValueError, UnicodeDecodeError):
            return "-"

    def log_request(self, code="-", size="-"):
        path = self.path.split("?", 1)[0].rstrip("/")
        ms = int((time.monotonic() - getattr(self, "_t0", time.monotonic())) * 1000)
        # /health is a 30s healthcheck and each upload chunk is one of a
        # thousand: both are DEBUG so a healthy device stays readable.
        chunk = self.command == "PATCH" and path.startswith("/files/")
        level = logging.DEBUG if (path == "/health" or chunk) else logging.INFO
        wdalog.http.log(level, "%s %s %s %s %s %dms",
                        self.client_address[0], self._user(), self.command,
                        self.path, code, ms)

    def log_error(self, fmt, *args):
        wdalog.http.warning("%s %s", self.client_address[0], fmt % args)

    def log_message(self, fmt, *args):        # anything else the base class emits
        wdalog.http.info("%s %s", self.client_address[0], fmt % args)


if __name__ == "__main__":
    wdalog.setup()
    # Stored custom* values are pushed back before the listener opens:
    # resolved's SetLinkDNS is runtime state, so a restart would otherwise drop
    # the configured servers while the parameter still claims them.
    wdalog.write.info("host config backends: %s", providers.hostcfg.probe())
    providers.networking.reapply()
    ok = (fw.rauc_info(fw.EMBEDDED)[0] if os.path.isfile(fw.EMBEDDED) else False)
    scheme = "https" if WDA_TLS else "http"
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    wdalog.http.info(
        "listening on %s://0.0.0.0:%d order=%s fw=%s embedded=%s auth=%s user=%s "
        "params=%d+dynamic writable=%d methods=%d loglevel=%s",
        scheme, PORT, ORDER, FW_VERSION, "yes" if ok else "no",
        "basic" if WDA_AUTH else "OFF", WDA_USER,
        len(providers.PARAMS), len(providers.WRITES), len(providers.METHODS),
        wdalog.LEVEL)
    if WDA_TLS:
        # HTTPS like PFC/CC WDA. Cert generated by the entrypoint (or mount your
        # own via TLS_CERT/TLS_KEY). Self-signed - clients use verify=False.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()
