# edge-computer-fw-update

Firmware update for the **WAGO Edge Computer** (order `0752-9xxx`, x86-64,
Debian + RAUC A/B), plus a WDA-compatible REST API so the tooling that drives a
PFC or TP600 drives the edge unchanged.

The edge is not a PFC: there is no WDA firmware stack and no barebox. Updates go
through stock **RAUC** (grub + ext4 A/B slots). RAUC always writes the *inactive*
slot and marks it for the next boot, so the running system is untouched until it
reboots and a bad flash is one reboot from recovery. The container never chooses
a slot; it hands the bundle to the host `rauc.service` over the mounted D-Bus
socket and stays unprivileged.

Each topic below appears exactly once. Sections 5, 6 and 7 are independent: read
the one you need. The same material as a single illustrated page, both update
routes side by side and the bundle build as a numbered sequence, is at
**https://wagoalex.github.io/edge-computer-fw-update/**
([`docs/index.html`](docs/index.html)).

1. [Pick a path](#1-pick-a-path)
2. [Device prerequisites](#2-device-prerequisites)
3. [Images and compose files](#3-images-and-compose-files)
4. [Configuration reference](#4-configuration-reference)
5. [Path A: one-shot container update](#5-path-a-one-shot-container-update)
6. [Path B: the WDA REST API](#6-path-b-the-wda-rest-api)
7. [Building your own RAUC bundle](#7-building-your-own-rauc-bundle)
8. [Repository layout](#8-repository-layout)
9. [Development and tests](#9-development-and-tests)
10. [Limits](#10-limits)

---

## 1. Pick a path

| You want to | Use | Section |
|---|---|---|
| Flash one device now, from the shell or Portainer | One-shot container | [5](#5-path-a-one-shot-container-update) |
| Drive updates from a client, a script, or `fw_update.py` | REST API | [6](#6-path-b-the-wda-rest-api) |
| Read live device state (ports, slots, users, clock) | REST API | [6.7](#67-parameters) |
| Ship firmware you built yourself | Build a bundle first, then either path | [7](#7-building-your-own-rauc-bundle) |

The payload is a separate choice from the path:

| Payload | One-shot | REST API |
|---|---|---|
| Bundle baked into the image (`bundle-latest`) | default | `start` with an empty `UploadFiles` |
| Your own bundle, present on the device | mount it at `BUNDLE` | mount it at `BUNDLE` |
| Your own bundle, pushed over the network | not supported | upload it, then `start` with its id |

---

## 2. Device prerequisites

One-time, on the edge, as root. Any device flashed from a bundle built by
[section 7](#7-building-your-own-rauc-bundle) already carries the first three;
a pristine factory edge does not.

```bash
# 1. trust the bundle's signing line
cp /etc/rauc/cert.pem /etc/rauc/keyring.pem
grep -q '^\[keyring\]' /etc/rauc/system.conf || \
  printf '\n[keyring]\npath=/etc/rauc/keyring.pem\n' >> /etc/rauc/system.conf

# 2. RAUC mounts verity bundles through loop + dm-verity
printf 'loop\ndm-verity\n' > /etc/modules-load.d/rauc.conf
modprobe loop dm-verity

# 3. only if this device will build bundles itself
apt-get install -y --no-install-recommends squashfs-tools

# 4. REST API only: host network mode is not behind Docker's port publishing,
#    so firewalld applies and the public zone allows only cockpit/dhcp/ssh
firewall-cmd --permanent --zone=public --add-port=443/tcp && firewall-cmd --reload
```

Skipping 1 gives `signature verification failed` at install; skipping 2 gives
`Failed to open /dev/loop-control`; skipping 4 leaves the API answering on the
device but unreachable from the network.

`/docker` is the 33 GB work disk and is where staging and bundle builds belong.
Never stage under `/tmp`, which is a tmpfs.

---

## 3. Images and compose files

`wagoalex/wago-fw-update-edge-computer` on Docker Hub:

| Tag | Size | Contains | Use for |
|---|---|---|---|
| `bundle-latest` | 2.8 GB | API + one-shot + embedded bundle | either path, nothing to pre-place |
| `bundle-V040100_IX05` | 2.79 GB | same, pinned firmware revision | reproducible rollout |
| `api-latest` | 238 MB | API + one-shot, no bundle | REST API where firmware is uploaded or mounted |
| `rauc` | 168 MB | one-shot client only | mount your own bundle, no REST surface |

Compose files in `container/`:

| File | Launches |
|---|---|
| `docker-compose.yml` | one-shot install, exits |
| `docker-compose.server.yml` | REST API on `bundle-latest` |
| `docker-compose.api.yml` | REST API on `api-latest`, no embedded bundle |
| `docker-compose.full.yml` | reference: every env var with its real default, both services |

Build locally instead of pulling:

```bash
cd container
docker build -t wago-fw-edge .                      # needs bundle.raucb present
docker build --target base -t wago-fw-edge:api .    # no bundle
docker build --target dev  -t wda-dev .             # adds pytest and tests/
```

---

## 4. Configuration reference

Every environment variable the code reads, with its real default.
`docker-compose.full.yml` is the same list in compose form.

| Variable | Default | Applies to | Meaning |
|---|---|---|---|
| `MODE` | `oneshot` | both | `server` runs the REST API, anything else installs once and exits |
| `BUNDLE` | `/firmware/bundle.raucb` | both | bundle inside the container |
| `STAGE_DIR` | `/docker/rauc-stage` | both | staging dir, must be the **same path** on host and container |
| `KEYRING` | `/etc/rauc/keyring.pem` | both | verification keyring, `/dev/null` skips verification (tests only) |
| `DRY_RUN` | `false` | one-shot | `true` verifies service and signature, installs nothing |
| `REBOOT` | `false` | one-shot | `true` reboots the host after a successful install |
| `PORT` | `8443` | server | listen port, use `443` in host network mode |
| `WDA_USER` | `admin` | server | Basic auth user |
| `WDA_PASSWORD` | `wago` | server | Basic auth password, always override |
| `WDA_AUTH` | `true` | server | `false` disables auth entirely, debugging only |
| `WDA_TLS` | `true` | server | `false` serves plain HTTP, debugging only |
| `TLS_CERT` / `TLS_KEY` | `/run/wda/cert.pem`, `key.pem` | server | self-signed at first start if absent |
| `WDA_LOG_LEVEL` | `INFO` | server | `DEBUG` adds `/health` and per-chunk upload lines |
| `ORDER_NUMBER` | `0752-9xxx` | server | reported by `0-0-identity-ordernumber` |
| `DEVICE_DESCRIPTION` | `WAGO Edge Computer` | server | reported by `0-0-identity-description` |
| `FIRMWARE_VERSION` | `04.01.00` | server | reported by `0-0-version-firmwareversion`, quote it |
| `HARDWARE_RELEASE_INDEX` / `SOFTWARE_RELEASE_INDEX` | empty | server | reported by `0-0-version-*` |
| `SERIAL_NUMBER` | from DMI | server | override the serial read from `product_serial` |
| `PASSWD_FILE` | `/etc/passwd` | server | mount the host's at `/host/etc/passwd` for real accounts |
| `SHADOW_FILE` | `/etc/shadow` | server | not mounted by default, so `ispasswordexpired` reads `false` |
| `PORT_MAP` | unset | server | escape hatch, `X1=enp1s0,...`, only where the udev rules did not run |

Volumes:

| Mount | Required | Why |
|---|---|---|
| `/run/dbus/system_bus_socket` | yes | the only route to the host `rauc.service` |
| `/etc/rauc/keyring.pem:ro` | yes | signature verification |
| `/docker/rauc-stage` | yes | same path both sides, needs room for a full bundle |
| `/etc/passwd:/host/etc/passwd:ro` | recommended | real accounts for `0-0-localusers-*` |
| `./data:/app/data` | recommended | custom presets survive a redeploy |
| your `.raucb` at `BUNDLE` | optional | install a different bundle without rebuilding |
| your cert at `TLS_CERT`/`TLS_KEY` | optional | replaces the generated self-signed cert |

---

## 5. Path A: one-shot container update

Installs the bundle into the inactive slot, prints WAGO-style `==>` progress,
exits. No listener, no auth, nothing left running.

```bash
cd container
DRY_RUN=true  docker compose run --rm edge-fwupdate    # verify service + signature only
DRY_RUN=false docker compose run --rm edge-fwupdate    # install, ~5 minutes
reboot                                                 # activates the new slot
```

Or as a Portainer stack:

```yaml
services:
  edge-fwupdate:
    image: wagoalex/wago-fw-update-edge-computer:bundle-latest
    restart: "no"
    environment:
      DRY_RUN: "false"
      REBOOT: "false"      # "true" reboots the host after a successful install
    volumes:
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket
      - /etc/rauc/keyring.pem:/etc/rauc/keyring.pem:ro
      - /docker/rauc-stage:/docker/rauc-stage
      # own bundle instead of the embedded one:
      # - /docker/edge-build/my.raucb:/firmware/bundle.raucb:ro
```

What it does, in order: check the host RAUC service over D-Bus, report the
running slot, stage the bundle to `STAGE_DIR`, verify it with `rauc info`
against the keyring, `rauc install` with live progress, remove the staged copy,
print the slot summary. Any failure is `FATAL:` on stderr with a non-zero exit,
and the staged file is cleaned up.

---

## 6. Path B: the WDA REST API

### 6.1 Deploy

```bash
cd container
WDA_PASSWORD=... docker compose -f docker-compose.server.yml up -d   # with embedded bundle
WDA_PASSWORD=... docker compose -f docker-compose.api.yml    up -d   # without
curl -sk https://192.168.2.17/health
```

Both files use `network_mode: host` and `PORT: 443`. Host mode is required
because the `0-0-networking-*` parameters read the host's namespace, where
`X1`/`X2` exist; a container's own namespace has none, and without it the ports
correctly read as absent instead of passing the container's `eth0` off as `X1`.
Host mode forbids `ports:`, so the API binds 443 directly, which is where an
unmodified WDA client looks (`https://<ip>/wda`).

### 6.2 Conventions

- Media type `application/vnd.api+json` (JSON:API). Envelopes follow WAGO's own
  OpenAPI 3.1 document, diffed against a live CC100.
- Auth is HTTP Basic over self-signed TLS, the PFC/CC posture. Clients use
  `curl -k` or `verify=False`.
- `meta.version` is `1.5.2-compat`. The suffix is how a client tells this
  rauc-backed re-implementation apart from a genuine WDA device.
- **`POST /runs` returns 201**, including for a failed method: a run is a
  created resource. Branch on the body, never the status code.

```
GET   /wda                                  service root
GET   /wda/parameters/<id>                  one parameter
GET   /wda/parameter-definitions/<id>/enum  enum members, where WDA has them
POST  /wda/methods/<id>/runs?result-behavior=sync
PATCH /files/{id}                           chunked upload
GET   /openapi/wda.openapi.json             generated spec, authenticated
GET   /health                               liveness, auth-exempt
```

A parameter response:

```json
{"data": {"id": "0-0-networking-ethernetports-1-name", "type": "parameters",
          "attributes": {"dataType": "string", "dataRank": "scalar",
                         "path": "Networking/EthernetPorts/1/Name", "value": "X1"},
          "links": {"self": "/wda/parameters/0-0-networking-ethernetports-1-name"},
          "relationships": {"definition": {}, "device": {}}},
 "jsonapi": {"version": "1.0"},
 "meta": {"version": "1.5.2-compat", "doc": "/openapi/wda.openapi.json"}}
```

A successful run returns `{"outArgs": {...}, "executionStatus": "done"}`; a
failed one returns `{"code": "26", "domainSpecificStatusCode": "<n>", "detail":
"...", "executionStatus": "error"}`.

### 6.3 The update state machine

The [walkthrough page](https://wagoalex.github.io/edge-computer-fw-update/)
draws this as a track with both routes beside it.

```
Inactive(0) --activate--> Prepared(2) --getuploadids--> upload --start-->
Started(3) --rauc install--> Unconfirmed(4) --finish--> Finished(8) --clear--> Inactive(0)
                    any failure --> Error(7) + errorcause
```

Poll `0-0-firmwareupdate-status` and `-progress`; on `Error(7)` read
`-errorcause` and `-debuginfo`.

### 6.4 Sequence: the embedded bundle

An empty or omitted `UploadFiles` is the documented signal for the bundle inside
the image. There is no separate endpoint.

```bash
IP=192.168.2.17; A="-u admin:$WDA_PASSWORD"; H="Content-Type: application/vnd.api+json"
M() { curl -sk $A -X POST "https://$IP/wda/methods/0-0-firmwareupdate-$1/runs?result-behavior=sync" -H "$H" -d "${2:-{\}}"; }

M activate '{"data":{"type":"runs","attributes":{"inArgs":{
   "KeepCustomerApplication":{"value":false},"CustomKeyValuePairs":{"value":[]}}}}}'
M start    '{"data":{"type":"runs","attributes":{"inArgs":{"UploadFiles":{"value":[]}}}}}'

curl -sk $A "https://$IP/wda/parameters/0-0-firmwareupdate-progress"
# at status 4:
M finish; M clear
```

On `api-latest` this route answers `no embedded bundle and no UploadFiles given`
until a bundle is mounted at `BUNDLE`.

### 6.5 Sequence: your own bundle

A `.wup` is a zip; the API takes the `.raucb` inside it, never the wrapper.

```bash
unzip -o firmware.wup -d /tmp/fwwork          # yields *.raucb

M activate '...'                               # as above
M getuploadids '{"data":{"type":"runs","attributes":{"inArgs":{
   "FileNames":{"value":["firmware.raucb"]}}}}}'
# -> outArgs.UploadFiles.value[0], e.g. 9f2c41ab77e05d13

# upload, then:
M start '{"data":{"type":"runs","attributes":{"inArgs":{
   "UploadFiles":{"value":["9f2c41ab77e05d13"]}}}}}'
```

The upload is `PATCH /files/{id}`, one `multipart/byteranges` part per request,
placed by its `Content-Range` offset. 4 MB is the verified safe chunk size; each
accepted chunk answers 204.

```
PATCH /files/9f2c41ab77e05d13
Content-Type: multipart/byteranges; boundary=WAGOFW

--WAGOFW
Content-Type: application/octet-stream
Content-Range: bytes 0-3999999/1342177280

<4 MB of binary>
--WAGOFW--
```

Chunk reassembly strips exactly one trailing CRLF. A client that trims a byte
set instead corrupts any chunk ending in `0x0d`, `0x0a` or `0x2d`.

`fw_update.py` from the sibling `wago-plc-mcp-server` speaks this protocol
already: `PLC_IP=192.168.2.17 PLC_USERNAME=admin PLC_PASSWORD=... WUP_PATH=...`.

### 6.6 Methods

`POST /wda/methods/<id>/runs?result-behavior=sync`

| Method id | inArgs | outArgs |
|---|---|---|
| `0-0-firmwareupdate-activate` | `KeepCustomerApplication`, `CustomKeyValuePairs` | none |
| `0-0-firmwareupdate-getuploadids` | `FileNames` (list) | `UploadFiles` (list of ids) |
| `0-0-firmwareupdate-start` | `UploadFiles`, empty for the embedded bundle | none |
| `0-0-firmwareupdate-finish` | none | none, runs `rauc status mark-good` |
| `0-0-firmwareupdate-clear` | none | none, deletes staged uploads |
| `0-0-firmwareupdate-cancel` | none | none, errorcause 101 |
| `0-0-firmwareupdate-settimeout` | `Timeout` (seconds) | none |
| `0-0-firmwareupdate-getlastlogentries` | `EntryCount` (default 25) | `Entries` (list) |
| `0-0-presets-list` / `-get` / `-save` / `-delete` | see [6.10](#610-presets) | |
| `0-0-presets-apply` | - | not implemented, returns a WDA error |

### 6.7 Parameters

Read-only. Every id comes from the FW31 cassette
(`wago-plc-mcp-server/docs/edge-fw31-parameters-raw.json`); none are invented.

| Parameter | Value | Backend |
|---|---|---|
| `0-0-firmwareupdate-status` | 0-9, see [6.9](#69-enums) | in-memory state |
| `0-0-firmwareupdate-progress` | 0-100 | `rauc install` output |
| `0-0-firmwareupdate-errorcause` | numbered cause | state machine |
| `0-0-firmwareupdate-debuginfo` | last RAUC output lines | state machine |
| `0-0-firmwareupdate-revertable` | bool | state machine |
| `0-0-identity-ordernumber` / `-description` / `-serialnumber` | device identity | env, DMI |
| `0-0-version-firmwareversion` / `-hardwarereleaseindex` / `-softwarereleaseindex` | versions | env |
| `0-0-systems` | slot instance list | `rauc status --output-format=json` |
| `0-0-systems-{1,2}-active` / `-configured` / `-available` | the A/B slots | same |
| `0-0-systemtime-now` / `-local-now` | clock | host clock |
| `0-0-networking-ethernetports` | port instance list | `/sys/class/net` |
| `0-0-networking-ethernetports-<n>-name` / `-enabled` / `-haslink` / `-macaddress` / `-currentspeedduplex` | per port | `/sys/class/net` |
| `0-0-networking-bridges` | bridge instance list | `/sys/class/net` |
| `0-0-networking-bridges-<n>-name` / `-label` / `-macaddress` / `-connectedethernetports` / `-ipconfiguration-currentaddresses` / `-ipconfiguration-currentdefaultgateway` | per bridge | `/sys/class/net`, `ip -o -4 addr` |
| `0-0-networking-routing-currentroutes` | route list | `/proc/net/route` |
| `0-0-networking-routing-currentroutes-<n>-address` / `-gatewayaddress` / `-gatewaymetric` / `-interface` | per route | same |
| `0-0-networking-hostname-currentname`, `-domain-currentdomain`, `-dns-utilizeddnsservers` | live names and resolvers | hostname, `/etc/resolv.conf` |
| `0-0-localusers` | account instance list, root is instance 1 | mounted `/etc/passwd` |
| `0-0-localusers-<uid>-name` / `-ispasswordexpired` | per account | `/etc/passwd`, `/etc/shadow` if mounted |
| `0-0-memorycard-isavailable` / `-iswriteprotected` / `-volumename` | SD/MMC | `/sys/block` |
| `0-0-ledstates` | one instance, id 1 | see below |
| `0-0-ledstates-1-name` / `-colors` / `-diagnosticinformation` | the RUN LED | `/sys/class/leds`, else systemd `SystemState` |

Notes that change what you read:

- Ports need no mapping table: `/etc/udev/rules.d/20-network-names.rules` already
  names the NICs `X1`, `X2`, and `X11`/`X12` on expansion models, so ports are
  discovered by name and the instance id is the number in the name. Addon-card
  ports (`LAN_A`, `LAN_B`) have no number and get no instance.
- Docker's own `docker0`, `br-<netid>` and `veth*` are filtered out of bridges
  and routes; they are not device networking and would shift instance ids
  whenever a container starts.
- An absent backend reports absent (`false`, `""`, `[]`), never a plausible
  guess. Without `/etc/passwd` mounted you get the container's own accounts,
  which looks right and is not.
- Four parameters are writable, see [6.8](#68-writable-parameters). Every
  other `custom*` / `static*` id is still absent rather than stubbed.
- **One LED, not five.** A PFC300 publishes SYS, RUN, IO and more because its
  firmware drives them. On an x86 edge, PWR, HDD and BTR are wired to the power
  rail, the SATA activity pin and the RTC battery: nothing in software reads
  them. So `0-0-ledstates` carries a single instantiation, WDA's `RUN` LED,
  backed by the device's running state. `0-0-ledstates-2-*` and up are absent,
  not stubbed green. If this platform does expose a PWR node, point
  `LED_PWR_SYSFS` at it (e.g. `/sys/class/leds/platform::power`) and it is used
  in preference to the systemd fallback.

### 6.8 Writable parameters

Four parameters accept a write. None of them can cut the connection the request
arrived on, which is why they are the first slice.

| Parameter | Backend | Applies to |
|---|---|---|
| `0-0-networking-hostname-customname` | `hostname1.SetStaticHostname` | `/etc/hostname` on the host |
| `0-0-networking-domain-customdomain` | NM `ipv4/ipv6.dns-search` | the resolver search domain on the link's profile |
| `0-0-networking-dns-customdnsservers` | `resolve1.SetLinkDNS`, else NM `ipv4/ipv6.dns` | the link carrying the default route, else the first with carrier |
| `0-0-networking-routing-ipforwarding-enabled` | sysctl drop-in + `systemd-sysctl` restart | `net.ipv4.ip_forward`, `net.ipv6.conf.all.forwarding` |

The first three go over the system D-Bus socket the container already mounts, so
nothing gains a privilege: systemd or NetworkManager does the privileged part and
polkit decides. The edge has no systemd-resolved, so DNS and the search domain go
through NetworkManager there; a device with resolved uses `SetLinkDNS`.

**Forwarding is opt-in and off by default.** `/proc/sys` is read-only in a
container and running `sysctl -w` on the host would mean arbitrary root exec over
D-Bus, which this project refuses. Instead the API writes
`/etc/sysctl.d/99-wda-ipforwarding.conf` and restarts one named unit, and that
needs a mount you have to add deliberately:

```yaml
    volumes:
      - /etc/sysctl.d:/etc/sysctl.d      # only if you want forwarding writable
```

Without it the write answers `503` naming the mount, and the container's
privileges are exactly what they were.

```bash
curl -sk -u admin:$WDA_PASSWORD -X PATCH \
  -H 'Content-Type: application/vnd.api+json' \
  -d '{"data":{"type":"parameters","attributes":{"value":"edge-lab-01"}}}' \
  https://192.168.2.17/wda/parameters/0-0-networking-hostname-customname

curl -sk -u admin:$WDA_PASSWORD -X PATCH \
  -H 'Content-Type: application/vnd.api+json' \
  -d '{"data":{"type":"parameters","attributes":{"value":["9.9.9.9","2620:fe::fe"]}}}' \
  https://192.168.2.17/wda/parameters/0-0-networking-dns-customdnsservers
```

Status codes follow the real device (read off a CC100, WDA 1.5.2):

| Code | Meaning |
|---|---|
| `204` | Applied, value stored as sent |
| `200` | Applied, but the value was normalised; the body carries the effective one |
| `400` | Invalid value. Nothing was touched |
| `404` | Unknown id, or the id is read-only |
| `415` | Content type is not `application/vnd.api+json` |
| `503` | The backend refused or is absent. Nothing was stored |

`PATCH /wda/parameters` takes a `data` array for several at once. It is applied
in order and is not atomic: the first failure stops the batch and is reported.

Three rules the implementation keeps:

- **`custom*` is not `current*`.** Writing the custom value stores the override
  and asks the system to apply it; `currentname` keeps reading the live system.
  An empty custom value means "no override" and never renames the running host.
- **Validate, then apply once.** A rejected hostname or a malformed address
  never reaches D-Bus, and nothing is persisted.
- **Nothing that carries an IP address is writable** - bridge addresses,
  gateways, static routes. A regression test enforces it.

Custom values live in `/app/data/network-custom.json` and are pushed back at
container start, because `SetLinkDNS` is runtime state that a reboot drops.
Discover writability at `GET /wda/parameter-definitions/<id>`, which reports
`writeable` and `userSetting` from the same registry the write path dispatches on.

### 6.9 Enums

`GET /wda/parameter-definitions/<id>/enum`, for
`0-0-firmwareupdate-status`, `0-0-firmwareupdate-errorcause` and
`0-0-ledstates-1-colors`. Values are the ones a real WDA 1.5.2 device reports
and are never renumbered.

`LEDColor`, read off a real PFC300: 0 `LED_COLOR_RED`, 1 `LED_COLOR_GREEN`,
2 `LED_COLOR_YELLOW`, 3 `LED_COLOR_BLUE`, 4 `LED_COLOR_CYAN`,
5 `LED_COLOR_MAGENTA`, 6 `LED_COLOR_WHITE`, 7 `LED_COLOR_OFF`. `Colors` is an
array: two entries mean the LED blinks between them, which is how WAGO encodes
"working but not nominal" (a PFC300 RUN LED reads `[1, 7]`).

| status | | errorcause | | `domainSpecificStatusCode` | |
|---|---|---|---|---|---|
| 0 | Inactive | 0 | NoError | 90 | update already active |
| 1 | Init | 101 | AbortByUser | 95 | not activated |
| 2 | Prepared | 200 | SignatureInvalid | 1 | other precondition failure |
| 3 | Started | 300 | NotEnoughResources | | |
| 4 | Unconfirmed | 600 | UpdateFailed | | |
| 5 | Confirmed | 601/602 | SignatureTooNew / TooOld | | |
| 6 | Revert | 603 | PartitionError | | |
| 7 | Error | 800 | RestartFailed | | |
| 8 | Finished | 900 | SelftestFailed | | |
| 9 | NotAvailable | 1000 | ConfirmationTimeout | | |

This build reports 101, 200 (`signature` seen in the RAUC output) and 600
(anything else). The rest are served for enum compatibility.

### 6.10 Presets

A preset is a named network-configuration fragment stored as WDA parameters:
`{"parameters": {param-id: value}}`. Predefined ones ship in the image; custom
ones live on the `/app/data` mount and survive a redeploy.

```bash
P() { curl -sk $A -X POST "https://$IP/wda/methods/0-0-presets-$1/runs" -H "$H" -d "${2:-{\}}"; }
P list
P save '{"data":{"attributes":{"inArgs":{
  "Name":{"value":"site-lab"},"Description":{"value":"lab X1 static"},
  "Parameters":{"value":{"0-0-networking-dns-customdnsservers":["192.168.2.1"]}}}}}}'
P get    '{"data":{"attributes":{"inArgs":{"Name":{"value":"site-lab"}}}}}'
P delete '{"data":{"attributes":{"inArgs":{"Name":{"value":"site-lab"}}}}}'
```

Names are validated against path traversal and every key must be a `0-0-`
parameter id, so a preset can only hold something the device could apply.
`0-0-presets-apply` returns an explicit WDA error rather than a 404: applying
means writing `custom*` parameters, which is Phase 3. `presets` is also the one
namespace here with no cassette entry behind it, named deliberately.

### 6.11 The generated spec

```bash
curl -sk $A "https://$IP/openapi/wda.openapi.json" | jq .
```

Generated from the provider registry at request time, so it lists exactly what
this build serves and cannot drift from the code. It is a strict subset of
WAGO's 40-path WDA 1.5.2 document and `info.description` says what is missing:
no discovery collections, no OAuth2 or bearer auth, no parameter `PATCH`. A
generated client therefore fails at build time on what is absent rather than at
runtime against a device. Unlike a real device, which serves its spec
anonymously, this one requires authentication.

There is no Swagger UI in the image. To browse it:

```bash
curl -sk $A "https://$IP/openapi/wda.openapi.json" > /tmp/wda.json
docker run --rm -p 8080:8080 -e SWAGGER_JSON=/spec/wda.json -v /tmp:/spec swaggerapi/swagger-ui
```

"Try it out" will not execute: the API sends no CORS headers and the certificate
is self-signed. Read there, call with `curl`.

### 6.12 Logs

Everything goes to stdout with an ISO 8601 timestamp, so `docker logs -f` is the
audit trail.

```
2026-09-01T08:00:57+00:00 INFO  wda.update activate -> Prepared
2026-09-01T08:00:57+00:00 INFO  wda.method 0-0-firmwareupdate-activate inArgs=[] done
2026-09-01T08:00:57+00:00 INFO  wda.http 127.0.0.1 admin POST /wda/methods/.../runs 201 0ms
2026-09-01T08:00:58+00:00 WARNING wda.method 0-0-firmwareupdate-start inArgs=[] ERROR dsc=95 firmware update not activated
```

`wda.http` is one line per request (client, user, verb, path, status, duration),
`wda.method` one per invocation with its outcome, `wda.update` the state
transitions. Credentials are never logged: not the Authorization header, not the
password, not request bodies. The username is, deliberately.

`/health` and individual upload chunks are DEBUG, since a 30 s healthcheck is
2880 lines a day and a 1.3 GB bundle is over a thousand chunks; uploads still
get one INFO line per 100 chunks. `WDA_LOG_LEVEL=DEBUG` turns them on.

---

## 7. Building your own RAUC bundle

The same eight steps, laid out visually with the failure each one prevents:
[the walkthrough page](https://wagoalex.github.io/edge-computer-fw-update/).

There is no cross-build. A bundle is a capture of a running edge rootfs, so
steps 1 to 7 run **on the device, as root, under `/docker`**. Roughly 25 minutes
and about 3 GB of scratch. `build/make_edge_raucb.sh` is all of it in one
script; the steps below are what it does and why.

**1. Build tools.** `mksquashfs` is not in the base image and `rauc bundle`
cannot run without it. Install it *before* the capture so it lands in the image
and every device flashed from the result can build its own bundles.

```bash
apt-get update && apt-get install -y --no-install-recommends squashfs-tools
```

**2. Signing key and certificate.** Self-signed, 4096 bit, ten years. The key
stays on this device and is excluded from the capture in step 5.

```bash
openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout /etc/rauc/key.pem -out /etc/rauc/cert.pem \
  -subj "/O=WAGO/CN=WAGO Edge Self-Signed" -days 3650
chmod 600 /etc/rauc/key.pem
```

**3. Trust it.** Register the certificate as the keyring before the capture, so
the trust travels with the image (this is [section 2](#2-device-prerequisites)
step 1, done once at the source instead of on every target).

```bash
cp /etc/rauc/cert.pem /etc/rauc/keyring.pem
printf '\n[keyring]\npath=/etc/rauc/keyring.pem\n' >> /etc/rauc/system.conf
```

**4. Kernel modules at boot.**

```bash
printf 'loop\ndm-verity\n' > /etc/modules-load.d/rauc.conf
```

**5. Capture the rootfs.** Volatile mounts, the work disk and the private key
are excluded; everything else, including steps 1 to 4, goes in.

```bash
OUT=/docker/edge-build; mkdir -p $OUT/content
tar --numeric-owner --one-file-system \
    --exclude=./tmp/* --exclude=./proc/* --exclude=./sys/* \
    --exclude=./dev/* --exclude=./run/* --exclude=./mnt/* \
    --exclude=./media/* --exclude=./docker/* --exclude=./lost+found \
    --exclude=./etc/rauc/key.pem \
    -czf $OUT/content/rootfs.tar.gz -C / .
```

**6. Manifest.** `compatible` must match `/etc/rauc/system.conf` character for
character or the install is refused before anything is written.

```bash
cat > $OUT/content/manifest.raucm <<'EOF'
[update]
compatible=WAGO Edge Computer 752-9xxx
version=4.1.0

[bundle]
format=verity

[image.rootfs]
filename=rootfs.tar.gz
EOF
```

Do not add `sha256` or `size`. `rauc bundle` computes them and aborts with
`Unexpected digest` if it finds them pre-filled.

**7. Sign, then read back.** `rauc info` is the same check the API runs at
`start`.

```bash
NAME=WAGO_OS0752-9xxx_Edge_FW5_V040100_IX05
rauc bundle --cert=/etc/rauc/cert.pem --key=/etc/rauc/key.pem \
  $OUT/content $OUT/$NAME.raucb
rauc info --keyring /etc/rauc/keyring.pem $OUT/$NAME.raucb
```

**8. Wrap as `.wup`** (on the workstation, only for catalog-resolving clients).
A `.wup` is a zip of `package-info.xml` plus the bundle, XML stored first,
exactly as WAGO ships them. `build/wrap_wup.sh` writes it.

```bash
scp root@192.168.2.17:/docker/edge-build/$NAME.raucb bundles/
bash build/wrap_wup.sh
```

The `OrderNo` in `package-info.xml` is the literal placeholder `0752-9xxx`. A
catalog client matches on that field, so set the real order number before
pointing one at a live device.

**Then use it** through [section 5](#5-path-a-one-shot-container-update) with the
bundle mounted at `BUNDLE`, or through
[section 6.5](#65-sequence-your-own-bundle) by uploading it. Never commit
`.raucb` or `.wup`: they are about 1.3 GB and `.gitignore`d.

---

## 8. Repository layout

```
build/
  make_edge_raucb.sh        run ON the edge as root: live rootfs -> signed verity .raucb
  wrap_wup.sh               run on the workstation: .raucb -> WAGO-style .wup

container/
  Dockerfile                base (API + one-shot) -> dev (+ pytest) -> prod (+ bundle)
  entrypoint.sh             MODE=server runs api.py, otherwise the one-shot install
  api.py                    transport only: HTTP, JSON:API, Basic auth, TLS
  openapi.py                generates the spec from the provider registry
  wdalog.py                 the three stdout loggers
  presets.py                preset store
  providers/                one module per WDA namespace, merged in __init__
    firmwareupdate.py         the update state machine over rauc
    networking.py             ports, bridges, routes, DNS, hostname
    system.py                 identity, version, systemtime, A/B slots, memorycard
    localusers.py             accounts from the mounted /etc/passwd
    wda_meta.json             dataType/dataRank/path, GENERATED from the cassette
  tests/                    pytest, backends mocked at the file and subprocess edge
  docker-compose*.yml       see section 3

rauc-container/             older variant that mounts the bundle from the device
bundles/                    build output, git-ignored
docs/
  index.html                the illustrated walkthrough, published by GitHub
                            Pages at wagoalex.github.io/edge-computer-fw-update/
```

## 9. Development and tests

Tests run in the container, never on the host:

```bash
cd container
docker build --target dev -t wda-dev .
docker run --rm --entrypoint python3 wda-dev -m pytest /tests -q
```

Smoke the API locally without a device:

```bash
docker run --rm -d --name wdat -e MODE=server -e WDA_PASSWORD=wago \
  -e KEYRING=/dev/null -p 18443:8443 wagoalex/wago-fw-update-edge-computer:api-latest
curl -sk -u admin:wago https://localhost:18443/wda/parameters/0-0-firmwareupdate-status
docker rm -f wdat
```

RAUC calls fail without the host D-Bus socket, which is expected; the state
machine, envelopes and auth are fully exercisable this way. There is no
host-side integration test for the install itself - it needs the real host
`rauc.service`.

Verified end to end against the edge at 192.168.2.17 on 2026-09-01: `activate`,
`start` with no `UploadFiles`, about 5 minutes of `rauc install` with live
progress, `Unconfirmed(4)`, `finish`, `Finished(8)`, `clear`, `Inactive(0)`,
with slot B written and both slots reporting boot status good.

## 10. Limits

- **Not real WDA.** Same URLs, JSON and enums for drop-in tooling, but no
  OAuth2/PAM, no full parameter tree, no `wdx` provider: the update state machine
  plus the read-only projections in [6.7](#67-parameters) and the two writable
  ids in [6.8](#68-writable-parameters).
- **Auth is HTTP Basic over self-signed TLS**, the PFC/CC posture. For the real
  stack, front it with the `wago-wda:x86` container (lighttpd + authd) from the
  WAGO SDK rather than growing `api.py`.
- **Self-signed bundles install here and nowhere else.** A genuine PFC or TP600
  WDA rejects them on the production signature check.
- **The API never reboots the device.** `rauc install` marks the inactive slot
  for the next boot; the device keeps running the current slot until it
  restarts, including an unplanned restart.
- **Writes are one slice deep.** Hostname, domain, DNS and IP forwarding are writable
  ([6.8](#68-writable-parameters)); every other `custom*` / `static*`
  parameter and `0-0-presets-apply` are not implemented and not stubbed.
  Nothing that sets an IP address is writable, by standing rule.

See the sibling `wago-plc-mcp-server` repo for the reverse-engineered WDA
mechanism and the paramd/authd x86 build this API stands in for.
