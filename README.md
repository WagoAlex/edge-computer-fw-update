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
`container/docker-compose.server.yml`, or the same service with
`environment: { MODE: server }` and `ports: ["8080:8080"]`. Then:
```bash
curl -sk http://EDGE:8080/update/status
curl -sk http://EDGE:8080/wda/parameters/0-0-firmwareupdate-status
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
  api.py                    WDA-compatible firmware-update REST API over rauc
  docker-compose.yml        one-shot: flash inactive slot, exit
  docker-compose.server.yml REST API: long-running WDA-shaped service

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

Direct calls:
```bash
IP=192.168.2.17
# activate
curl -sk -X POST "http://$IP:8080/wda/methods/0-0-firmwareupdate-activate/runs?result-behavior=sync" \
  -H 'Content-Type: application/vnd.api+json' \
  -d '{"data":{"type":"runs","attributes":{"inArgs":{"KeepCustomerApplication":{"value":false}}}}}'
# reserve an upload id
curl -sk -X POST "http://$IP:8080/wda/methods/0-0-firmwareupdate-getuploadids/runs" \
  -H 'Content-Type: application/vnd.api+json' \
  -d '{"data":{"type":"runs","attributes":{"inArgs":{"FileNames":{"value":["edge.raucb"]}}}}}'
# upload chunks -> PATCH /files/{id} (multipart/byteranges, Content-Range)
# start, then poll:
curl -sk "http://$IP:8080/wda/parameters/0-0-firmwareupdate-status"
curl -sk "http://$IP:8080/wda/parameters/0-0-firmwareupdate-progress"
# one-call human view:
curl -sk "http://$IP:8080/update/status"
```

## Honest boundaries

- **Not real WDA.** Same URLs/JSON/enums for drop-in tooling, but no OAuth2/PAM,
  no full parameter tree, no `wdx` provider. It's the update state machine only.
- **No auth on the API.** Trusted LAN only. For real auth/TLS, front it with the
  `wago-wda:x86` container (lighttpd + authd) built from the WAGO SDK.
- **Self-signed bundles.** Install via `rauc` with the baked keyring; they are
  NOT accepted by a real PFC/TP600 WDA (which checks WAGO's production signature).

See the wago-plc-mcp-server repo for the reverse-engineered WDA mechanism and
the paramd/authd x86 build this API stands in for.
