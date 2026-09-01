# edge-computer-fw-update

Firmware update for the WAGO Edge Computer (752-9xxx, x86-64, Debian + RAUC
A/B), plus a WDA-compatible REST API so the same tooling that drives a PFC/TP600
can drive the edge.

The edge is not a PFC: no WDA firmware stack, no barebox. Updates go through
standard **RAUC** (grub + ext4 A/B slots). This project builds a signed RAUC
bundle from a running edge, wraps it as a `.wup`, and ships a container that
installs it to the inactive slot - either one-shot or behind a REST API shaped
like WAGO's production WDA.

## Using the pre-built image (no build required)

The published image is **self-contained** - the firmware bundle is baked in, so
you don't clone this repo or build anything. You only need the image and a
one-time device prep.

Image: `wagoalex/wago-fw-update-edge-computer`
- `bundle-latest` / `bundle-V040100_IX05` - bundle embedded (use these)
- `rauc` - client only, mount your own bundle from the device

**One-time device prep** (a factory edge; images built by this repo already
carry it). On the edge as root:
```bash
# trust the bundle's self-signed line
cp /etc/rauc/cert.pem /etc/rauc/keyring.pem 2>/dev/null || true   # or install your keyring
grep -q '^\[keyring\]' /etc/rauc/system.conf || \
  printf '\n[keyring]\npath=/etc/rauc/keyring.pem\n' >> /etc/rauc/system.conf
# RAUC mounts verity bundles via loop + dm-verity
printf 'loop\ndm-verity\n' > /etc/modules-load.d/rauc.conf
modprobe loop dm-verity
```

**Run - one-shot flash** (Portainer stack or CLI on the edge):
```yaml
services:
  edge-fwupdate:
    image: wagoalex/wago-fw-update-edge-computer:bundle-latest
    restart: "no"
    environment:
      DRY_RUN: "true"     # verify only; set "false" to flash, REBOOT "true" to reboot after
    volumes:
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket
      - /etc/rauc/keyring.pem:/etc/rauc/keyring.pem:ro
      - /docker/rauc-stage:/docker/rauc-stage
```
```bash
DRY_RUN=false docker compose run --rm edge-fwupdate   # writes the inactive slot
reboot                                                # activate; bad boot auto-falls-back
```

**Run - WDA-compatible REST API** (long-running): use
`container/docker-compose.server.yml` (HTTPS + Basic auth on :443, like PFC/CC
WDA), or the same service with `environment: { MODE: server, WDA_PASSWORD: ... }`
and `ports: ["443:8443"]`. Then:
```bash
curl -sk -u admin:PASS https://EDGE/wda/parameters/0-0-firmwareupdate-status
# flash the built-in image: activate, then start with NO UploadFiles
curl -sk -u admin:PASS -X POST https://EDGE/wda/methods/0-0-firmwareupdate-activate/runs -d '{"data":{"attributes":{"inArgs":{}}}}'
curl -sk -u admin:PASS -X POST https://EDGE/wda/methods/0-0-firmwareupdate-start/runs -d '{"data":{"attributes":{"inArgs":{}}}}'
```

It installs to the **inactive** A/B slot via the host `rauc.service`,
unprivileged. Reboot activates; a bad boot auto-falls-back to the running slot.

## Full docker-compose reference (every option)

One service showing all configurable env vars and volumes. Delete what you
don't need - only the three volumes and (for a factory edge) the keyring are
actually required. `MODE` selects behaviour; options for the other mode are
ignored.

```yaml
services:
  edge-fwupdate:
    image: wagoalex/wago-fw-update-edge-computer:bundle-latest
    # bundle-latest / bundle-V040100_IX05 = self-contained (bundle embedded)
    # rauc = client only; then mount your own bundle (see rauc-container/)

    restart: "no"          # one-shot: never restart. Use "unless-stopped" for MODE=server.

    environment:
      # --- mode ---
      MODE: oneshot        # oneshot = flash once and exit | server = REST API

      # --- one-shot mode (MODE=oneshot) ---
      DRY_RUN: "true"      # "true" = verify service + signature, do NOT flash
                           # "false" = install to the inactive slot
      REBOOT: "false"      # "true" = reboot host via systemd after a successful flash
                           #          (opt-in; off keeps the success log/exit status)

      # --- server mode (MODE=server) ---
      PORT: "8080"         # REST API listen port
      ORDER_NUMBER: "0752-9xxx"    # reported at /wda/parameters/0-0-identity-ordernumber
      FIRMWARE_VERSION: "04.01.00" # reported at /wda/parameters/0-0-version-firmwareversion

      # --- both modes (rarely changed) ---
      BUNDLE: /firmware/bundle.raucb   # embedded bundle path inside the image
      KEYRING: /etc/rauc/keyring.pem   # keyring used to verify the bundle
      STAGE_DIR: /docker/rauc-stage    # host-visible staging dir (must match the
                                       # bind mount below, SAME path both sides)

    # server mode only - publish the API port
    ports:
      - "8080:8080"

    volumes:
      # REQUIRED: host rauc.service socket - lets the container drive the host
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket
      # REQUIRED on a factory edge: keyring that trusts the bundle signature
      - /etc/rauc/keyring.pem:/etc/rauc/keyring.pem:ro
      # REQUIRED: host-visible staging dir - the host service reads the bundle
      # here during install; MUST be the same path as STAGE_DIR above
      - /docker/rauc-stage:/docker/rauc-stage
      # OPTIONAL (only for the ":rauc" client image): mount a bundle from the
      # device and point BUNDLE at it, instead of using the embedded one
      # - /docker/edge-build/my.raucb:/firmware/bundle.raucb:ro
```

Run it:
```bash
# one-shot
DRY_RUN=false docker compose run --rm edge-fwupdate
# server (set MODE=server, restart=unless-stopped above)
docker compose up -d
```

## Layout

```
build/                     Build a bundle FROM a running edge (run on the device)
  make_edge_raucb.sh        tar live rootfs -> signed verity .raucb (self-signed
                            cert + keyring + loop/dm-verity + squashfs-tools baked in)
  wrap_wup.sh               wrap the .raucb into a WAGO-style .wup (run on host)

container/                 Self-contained updater image (carries the bundle)
  Dockerfile                debian-slim + rauc + python3, bundle.raucb inside
  entrypoint.sh             one-shot install (MODE=oneshot) OR REST API (MODE=server)
  api.py                    WDA transport only: HTTP, JSON:API, Basic auth, TLS
  providers/                one module per WDA namespace, registered in __init__
    firmwareupdate.py         0-0-firmwareupdate-* state machine over rauc
    networking.py             0-0-networking-* from /sys/class/net + /proc/net/route
    system.py                 identity/version/systemtime/systems (A/B slots)/memorycard
    localusers.py             0-0-localusers-<uid>-* from the host's /etc/passwd
  presets.py                presets/*.json store (no apply - see below)
  tests/                    pytest, backends mocked at the file/subprocess boundary
  docker-compose.yml        one-shot: flash inactive slot, exit
  docker-compose.server.yml REST API: long-running WDA-shaped service
  docker-compose.full.yml   reference: EVERY env var and mount, documented,
                            with both services (wda-api + oneshot flash)

rauc-container/            Variant that mounts the bundle from the device
                            instead of embedding it (Dockerfile, entrypoint,
                            portainer-stack.yml)

bundles/                   Built artifacts
  *.wup                     WAGO-style wrapper (package-info.xml + .raucb)
  *.raucb                   the signed RAUC bundle
```

## Image

`wagoalex/wago-fw-update-edge-computer` on Docker Hub:
- `bundle-latest` / `bundle-V040100_IX05` - self-contained (bundle embedded)
- `rauc` - the non-embedded client (mount the bundle from the device)

## How updates actually work here

RAUC installs to the **inactive** slot (rootfs.1 <-> rootfs.2) and marks it for
the next boot; the running slot is untouched, so a bad flash is one reboot from
recovery. The container never touches slot logic - it hands the bundle to the
host `rauc.service` over the mounted D-Bus socket and stays unprivileged.

Device prereqs (baked into any image built by `build/make_edge_raucb.sh`):
- `/etc/rauc/keyring.pem` + `[keyring]` in `system.conf` - trusts the self-signed line
- `/etc/modules-load.d/rauc.conf` - `loop` + `dm-verity` (verity bundle mount)
- `squashfs-tools` - so the device can build its own bundles

A pristine factory edge needs those set once before the first update.

## Quick start

### One-shot (Portainer stack or CLI)
```bash
cd container
DRY_RUN=true  docker compose run --rm edge-fwupdate   # verify, no flash
DRY_RUN=false docker compose run --rm edge-fwupdate   # flash inactive slot
# then reboot the device (or REBOOT=true)
```

### REST API (WDA-compatible)
```bash
cd container
docker compose -f docker-compose.server.yml up -d     # API on :8080
```

The API mirrors WAGO's WDA firmware-update surface (JSON:API, the
`0-0-firmwareupdate-*` method-runs, chunked `/files` upload, real status/
errorcause enums), so the wago-plc-mcp-server `fw_update.py` sequence works
against it: `activate -> getuploadids -> PATCH /files/{id} -> start -> poll
status/progress -> finish -> clear`.

WDA methods, `POST /wda/methods/<id>/runs?result-behavior=sync`: `activate`,
`getuploadids`, `start`, `finish`, `clear`, `cancel`, `settimeout`,
`getlastlogentries`. Success -> `{"data":{"attributes":{"outArgs":{…},
"executionStatus":"done"}}}`; a precondition failure -> the WDA error envelope
(`code:"26"`, `domainSpecificStatusCode` 95=not-activated / 90=already-active).
Status params: `0-0-firmwareupdate-status` (enum 0-9), `-progress` (0-100),
`-errorcause` (numbered), `-debuginfo`, `-revertable`; identity
`0-0-version-firmwareversion`, `0-0-identity-ordernumber`; enum definitions at
`/wda/parameter-definitions/<id>/enum`. Service root `GET /wda` returns a
`devices` document with `meta.version`.

Direct calls (HTTPS + Basic auth, like PFC/CC WDA):
```bash
IP=192.168.2.17; A="-u admin:PASS"   # PASS = WDA_PASSWORD
# activate
curl -sk $A -X POST "https://$IP/wda/methods/0-0-firmwareupdate-activate/runs?result-behavior=sync" \
  -H 'Content-Type: application/vnd.api+json' \
  -d '{"data":{"type":"runs","attributes":{"inArgs":{"KeepCustomerApplication":{"value":false}}}}}'

# --- flash the BUILT-IN image: start with NO UploadFiles ---
curl -sk $A -X POST "https://$IP/wda/methods/0-0-firmwareupdate-start/runs" \
  -d '{"data":{"type":"runs","attributes":{"inArgs":{}}}}'

# --- OR flash an UPLOADED bundle ---
# reserve an upload id, PATCH chunks, then start with the id:
curl -sk $A -X POST "https://$IP/wda/methods/0-0-firmwareupdate-getuploadids/runs" \
  -d '{"data":{"type":"runs","attributes":{"inArgs":{"FileNames":{"value":["edge.raucb"]}}}}}'
# upload chunks -> PATCH /files/{id} (multipart/byteranges, Content-Range), then:
# curl ... -d '{"data":{"attributes":{"inArgs":{"UploadFiles":{"value":["<id>"]}}}}}'  .../0-0-firmwareupdate-start/runs

# poll
curl -sk $A "https://$IP/wda/parameters/0-0-firmwareupdate-status"
curl -sk $A "https://$IP/wda/parameters/0-0-firmwareupdate-progress"
# finish + clear once status reaches 4 (Unconfirmed)
curl -sk $A -X POST "https://$IP/wda/methods/0-0-firmwareupdate-finish/runs" -d '{"data":{"attributes":{"inArgs":{}}}}'
curl -sk $A -X POST "https://$IP/wda/methods/0-0-firmwareupdate-clear/runs"  -d '{"data":{"attributes":{"inArgs":{}}}}'
```

## Read-only device parameters

Beyond the update state machine the API serves the device's live state as real
WDA parameters. **Every ID comes from the FW31 cassette**
(`wago-plc-mcp-server/docs/edge-fw31-parameters-raw.json`) - none are invented,
per the naming rule in `CLAUDE.md`.

| Namespace | Serves | Backend |
|---|---|---|
| `0-0-networking-ethernetports-{1,2}-*` | X1/X2 `name`,`enabled`,`haslink`,`macaddress`,`currentspeedduplex` | `/sys/class/net` |
| `0-0-networking-bridges-{1,2}-*` | `name`,`macaddress`,`connectedethernetports`,`ipconfiguration-currentaddresses`,`-currentdefaultgateway` | `/sys/class/net`, `ip -o -4 addr` |
| `0-0-networking-routing-currentroutes-<n>-*` | `address`,`gatewayaddress`,`gatewaymetric`,`interface` | `/proc/net/route` |
| `0-0-networking-hostname-currentname`, `-domain-currentdomain`, `-dns-utilizeddnsservers` | live names/resolvers | hostname, `/etc/resolv.conf` |
| `0-0-systems-{1,2}-active\|configured\|available` | the RAUC A/B slots | `rauc status --output-format=json` |
| `0-0-identity-*`, `0-0-version-*` | order number, serial, versions | env + DMI |
| `0-0-systemtime-now`, `-local-now` | clock | host clock |
| `0-0-localusers-<uid>-name\|-ispasswordexpired` | login accounts (root is instance 1) | mounted `/etc/passwd` |
| `0-0-presets-*` (methods) | named network-config fragments | `presets/` + `/app/data` |
| `0-0-memorycard-*` | SD/MMC presence | `/sys/block` |

Three deploy details, all handled in `docker-compose.server.yml`:

- **Network reads need the host netns.** A container's own namespace has no
  X1/X2, so the compose file uses `network_mode: host`. Without it the ports read
  *absent* (empty list) rather than passing the container's `eth0` off as X1.
- **Host mode is not behind Docker's port mapping, so firewalld applies.**
  Docker's published ports bypass firewalld; a host-mode listener does not. The
  edge's `public` zone allows only cockpit/dhcp/ssh on X1, so open 443 once:
  `firewall-cmd --permanent --zone=public --add-port=443/tcp && firewall-cmd --reload`.
  Skip this and the API answers on the device but not from the network.
- **No port mapping table is needed.** The edge's
  `/etc/udev/rules.d/20-network-names.rules` already renames every NIC to its
  WAGO name (`X1`, `X2`, and `X11`/`X12` on the expansion models), so ports are
  discovered by name and the instance id is the number in it - `X11` is
  `ethernetports-11`. `PORT_MAP="X1=enp1s0,..."` overrides that on a box where
  the udev rules did not run.

Docker's own interfaces are filtered out: `docker0`, `br-<netid>` and `veth*` are
neither WAGO bridges nor device routes, and letting them in would shift every
instance id whenever a container starts.

Everything above is read-only. The writable twins (`custom*`/`static*` -
hostname, DNS, routes, bridge membership) are Phase 3 and are not stubbed.

### Presets

A preset is a named network-configuration fragment - per-port IP addresses, DNS
servers, routes - stored as WDA parameters: `{"parameters": {param-id: value}}`.
Predefined ones ship in the image; custom ones live on `./data` (mounted at
`/app/data`) and survive a redeploy.

```bash
A="-u admin:$WDA_PASSWORD"; B=https://192.168.2.17
curl -sk $A -X POST "$B/wda/methods/0-0-presets-list/runs" -d '{}'
curl -sk $A -X POST "$B/wda/methods/0-0-presets-save/runs" -d '{"data":{"attributes":{"inArgs":{
  "Name":{"value":"site-lab"},"Description":{"value":"lab X1 static"},
  "Parameters":{"value":{"0-0-networking-dns-customdnsservers":["192.168.2.1"]}}}}}}'
curl -sk $A -X POST "$B/wda/methods/0-0-presets-get/runs"    -d '{"data":{"attributes":{"inArgs":{"Name":{"value":"site-lab"}}}}}'
curl -sk $A -X POST "$B/wda/methods/0-0-presets-delete/runs" -d '{"data":{"attributes":{"inArgs":{"Name":{"value":"site-lab"}}}}}'
```

Names are validated (no path traversal) and every key must be a `0-0-` parameter
id, so a preset can only ever hold something the device could actually apply.

**`0-0-presets-apply` is not implemented** and returns an explicit WDA error, not
a 404: applying means writing `custom*` parameters, which is Phase 3.
`presets` is also the one namespace here with no FW31 cassette entry behind it -
WDA has no equivalent, so the name was chosen deliberately rather than invented
in passing.

### Tests

```bash
cd container
docker build --target dev -t wda-dev .
docker run --rm --entrypoint python3 wda-dev -m pytest /tests -q
```

### Verified end to end

The full sequence has been driven through the REST API against the edge at
192.168.2.17 (2026-09-01): `activate` -> `start` with no `UploadFiles` (embedded
bundle) -> ~5 min `rauc install` with live progress -> `Unconfirmed(4)` ->
`finish` (mark-good) -> `Finished(8)` -> `clear` -> `Inactive(0)`, with slot B
written and both slots reporting boot status good.

Note that `rauc install` always marks the inactive slot for the *next* boot. The
device keeps running the current slot until it reboots - including an unplanned
reboot.

## Logs

Every REST action goes to stdout with an ISO 8601 timestamp, so `docker logs -f`
is the audit trail:

```
2026-09-01T08:00:57+00:00 INFO  wda.update activate -> Prepared
2026-09-01T08:00:57+00:00 INFO  wda.method 0-0-firmwareupdate-activate inArgs=[] done
2026-09-01T08:00:57+00:00 INFO  wda.http 127.0.0.1 admin POST /wda/methods/0-0-firmwareupdate-activate/runs 201 0ms
2026-09-01T08:00:58+00:00 WARNING wda.method 0-0-firmwareupdate-start inArgs=[] ERROR dsc=95 firmware update not activated
2026-09-01T08:00:58+00:00 INFO  wda.http upload d935bbadb4314194 chunk 1, 0.0 MiB written
```

`wda.http` is one line per request (client, user, verb, path, status, duration),
`wda.method` one per invocation with its outcome - a failure is a WARNING with
the `domainSpecificStatusCode` - and `wda.update` the firmware-update state
transitions.

Credentials are never logged: not the Authorization header, not the password,
not request bodies. The username is, deliberately - who started a firmware
update is worth knowing.

`/health` and individual upload chunks are DEBUG, since a 30s healthcheck is
2880 lines a day and a 1.3 GB bundle is over a thousand chunks; uploads still
get one INFO line per 100 chunks. `WDA_LOG_LEVEL=DEBUG` turns them on.

## Inspecting the API

```bash
curl -sk -u admin:$WDA_PASSWORD https://192.168.2.17/openapi/wda.openapi.json | jq .
```

An OpenAPI 3.1 document, generated from the provider registry at request time -
so it lists exactly the parameters, methods and enums this build serves and
cannot drift from the code. Unlike a real WAGO device, which serves its spec
anonymously, this one requires authentication.

It is a **strict subset** of WAGO's 40-path WDA 1.5.2 spec, and says so in
`info.description`: no discovery collections (`/wda/parameters`, `/wda/methods`,
`/wda/*-definitions`, `/wda/features`, `/wda/monitoring-lists`, `/wda/devices`),
no OAuth2 or bearer auth, no parameter `PATCH`. A generated client therefore
fails at build time on what is missing, rather than at runtime against a device.

Responses match the real thing - the shape below was diffed against a live CC100:

```json
{"data": {"id": "0-0-networking-ethernetports-1-name", "type": "parameters",
          "attributes": {"dataType": "string", "dataRank": "scalar",
                         "path": "Networking/EthernetPorts/1/Name", "value": "X1"},
          "links": {"self": "..."}, "relationships": {"definition": {}, "device": {}}},
 "jsonapi": {"version": "1.0"},
 "meta": {"version": "1.5.2-compat", "doc": "/openapi/wda.openapi.json"}}
```

`meta.version` carries a `-compat` suffix on purpose: it is how a client tells
this rauc-backed re-implementation apart from a genuine WDA device.

## Honest boundaries

- **Not real WDA.** Same URLs/JSON/enums for drop-in tooling, but no OAuth2/PAM,
  no full parameter tree, no `wdx` provider - the update state machine plus the
  read-only projections above.
- **Auth is HTTP Basic over self-signed TLS**, matching the PFC/CC posture, not
  WAGO's OAuth2/PAM stack. For the real thing, front it with the `wago-wda:x86`
  container (lighttpd + authd) built from the WAGO SDK.
- **Self-signed bundles.** Install via `rauc` with the baked keyring; they are
  NOT accepted by a real PFC/TP600 WDA (which checks WAGO's production signature).

See the wago-plc-mcp-server repo for the reverse-engineered WDA mechanism and
the paramd/authd x86 build this API stands in for.
