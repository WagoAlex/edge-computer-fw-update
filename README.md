# edge-computer-fw-update

Firmware update for the WAGO Edge Computer (752-9xxx, x86-64, Debian + RAUC
A/B), plus a WDA-compatible REST API so the same tooling that drives a PFC/TP600
can drive the edge.

The edge is not a PFC: no WDA firmware stack, no barebox. Updates go through
standard **RAUC** (grub + ext4 A/B slots). This project builds a signed RAUC
bundle from a running edge, wraps it as a `.wup`, and ships a container that
installs it to the inactive slot - either one-shot or behind a REST API shaped
like WAGO's production WDA.

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
