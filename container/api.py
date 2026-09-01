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
import os
import re
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import providers
import openapi
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

    def do_PATCH(self):
        if not self._authed():
            return
        path = self.path.split("?", 1)[0].rstrip("/")
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
        self._send(204, None)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ok = (fw.rauc_info(fw.EMBEDDED)[0] if os.path.isfile(fw.EMBEDDED) else False)
    scheme = "https" if WDA_TLS else "http"
    print(f"WDA-compatible API on {scheme}://0.0.0.0:{PORT}  order={ORDER} "
          f"fw={FW_VERSION}  embedded={'yes' if ok else 'no'}  "
          f"auth={'basic' if WDA_AUTH else 'off'} user={WDA_USER}  "
          f"params={len(providers.PARAMS)}+dynamic methods={len(providers.METHODS)}",
          flush=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    if WDA_TLS:
        # HTTPS like PFC/CC WDA. Cert generated by the entrypoint (or mount your
        # own via TLS_CERT/TLS_KEY). Self-signed - clients use verify=False.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()
