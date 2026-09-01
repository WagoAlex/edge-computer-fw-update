# CLAUDE.md - edge-computer-fw-update

Handoff guide for Claude Code working in this repo. Read `README.md` first for
the user-facing overview; this file is the operational context that isn't
obvious from the tree.

## What this is

Firmware update for the **WAGO Edge Computer** (order `0752-9xxx`, x86-64,
Debian + RAUC A/B). The edge is NOT a PFC: it has no WDA firmware stack and no
barebox. Updates go through stock **RAUC** (grub + ext4 A/B slots). This repo:

1. builds a signed RAUC bundle from a running edge (`build/`),
2. wraps it as a WAGO-style `.wup` (`build/wrap_wup.sh`),
3. ships a container that installs it to the inactive slot - one-shot or behind
   a **WDA-compatible REST API** (`container/`).

Sibling project: `~/Documents/mcp/wago-plc-mcp-server` - the MCP server and the
reverse-engineered real WDA mechanism (`fwupdate/fw_update.py`,
`docs/wda-firmware-update.md`). The REST API here deliberately mimics that
client's contract. `~/Documents/wago-sdk/` has the paramd/authd x86 build this
API stands in for.

## Hard rules

- **Never commit firmware bundles.** `*.raucb` / `*.wup` are ~1.3 GB and
  `.gitignore`d. `container/bundle.raucb` is a hardlink to a real bundle - it
  must exist for `docker build` but must never be committed.
- **RAUC picks the slot, not us.** `rauc install` always writes the inactive
  slot and marks it for next boot. Never add slot-selection logic.
- **The container is unprivileged.** It drives the host `rauc.service` over the
  mounted D-Bus socket. Do NOT reintroduce `privileged`/`pid:host`/nsenter - an
  earlier attempt was correctly blocked as a container-escape shape.
- **Self-signed only.** Bundles are signed with a self-signed cert; they install
  via `rauc` with the baked keyring but are rejected by a genuine PFC/TP600 WDA
  (production-signature check). Don't claim otherwise.
- **WAGO nomenclature for every new resource - no exceptions.** Do NOT invent
  REST paths like `/update/start-embedded` or `/update/status`. Model everything
  as WDA parameters/methods under `0-0-<feature>-<name>` and drive it through
  `POST /wda/methods/<id>/runs` or `GET /wda/parameters/<id>`. Extend an existing
  method's inArgs before adding a new resource (e.g. embedded flash = the standard
  `0-0-firmwareupdate-start` with empty `UploadFiles`, not a new endpoint). The
  only allowed non-WDA path is `/health` (container liveness, auth-exempt).
- **Network resources follow WAGO too, and never touch IPs.** When adding
  networking (NIC bridging - e.g. bridging ports X11/X12, bridge mode; DNS;
  routes; hostname), use WAGO's parameter scheme and WAGO port names (`X1`,
  `X2`, `X11`, `X12`, ...), not Linux `ethN`/invented names. NEVER modify or
  hardcode a device's IP addresses as a side effect - read/report them, change
  them only when that is the explicit task.

## Layout & the moving parts

```
build/make_edge_raucb.sh   RUN ON THE EDGE (root). tar live rootfs -> signed
                           verity .raucb. Bakes into the captured image:
                           /etc/rauc/keyring.pem + [keyring] in system.conf,
                           /etc/modules-load.d/rauc.conf (loop, dm-verity),
                           squashfs-tools. EXCLUDES /etc/rauc/key.pem (private
                           key never ships). Builds under /docker (33G), NOT
                           /tmp (tmpfs, OOM risk).
build/wrap_wup.sh          RUN ON HOST. zip package-info.xml + .raucb -> .wup.
                           Paths point at this repo's bundles/.
container/Dockerfile       debian-trixie-slim + rauc + dbus + python3 + iproute2.
                           Stages: base -> dev (adds pytest + tests/) -> prod
                           (adds /firmware/bundle.raucb). Default target = prod.
container/entrypoint.sh    MODE=server -> exec python3 /api.py; else one-shot
                           install (WAGO "==>" log style, opt-in REBOOT).
container/api.py           Transport only: HTTP, JSON:API, Basic auth, TLS.
container/providers/       One module per WDA namespace; __init__ merges their
                           PARAMS/RESOLVE/METHODS/ENUMS into one registry.
container/presets.py       Preset store (no apply, not exposed over HTTP).
container/docker-compose.full.yml
                           Reference compose: every env var the code reads, with
                           its real default as the value, plus both services.
                           Keep it in sync when you add an os.environ.get().
container/tests/           pytest; backends mocked at the file/subprocess edge.
```

## The REST API (container/api.py)

Mirrors WAGO's production WDA firmware-update surface so `fw_update.py` drives
it unchanged. Key invariants to preserve if you touch it:

- JSON:API (`application/vnd.api+json`). Envelopes follow WAGO's own OpenAPI 3.1
  document, diffed against a live CC100 (192.168.42.110) on 2026-09-01:
  `attributes` carries `dataType`/`dataRank`/`path` beside `value`, resources
  carry `links`+`relationships`, documents carry `jsonapi` and `meta`. Keep them.
- `meta.version` is `1.5.2-compat`. The `-compat` suffix is load-bearing: it is
  how a client tells this apart from real WDA. Never drop it.
- **`POST /runs` returns 201**, not 200 - a run is a created resource. A method
  that fails also returns 201 with the error envelope in the body; `fw_update.py`
  ignores the status code and branches on `domainSpecificStatusCode`.
- `dataType`/`dataRank`/`path` come from `providers/wda_meta.json`, GENERATED
  from the cassette - regenerate it, never hand-edit. They are not derivable from
  the id (`...-dns-utilizeddnsservers` is `Networking/DNS/UtilizedDNSServers`).
- `GET /openapi/wda.openapi.json` is generated from the provider registry at
  request time, so the spec cannot drift from the code. It is a STRICT SUBSET of
  WAGO's 40-path document and `info.description` says exactly what is missing.
  **It requires auth; a real device serves it anonymously.** Deliberate.
- Methods at `POST /wda/methods/0-0-firmwareupdate-<m>/runs`; success returns
  `outArgs`, failure returns the WDA error envelope with `code:"26"` +
  `domainSpecificStatusCode` (**95** = not activated, **90** = already active -
  `fw_update.py` branches on these exact strings).
- Chunked upload `PATCH /files/{id}`, `multipart/byteranges` + `Content-Range`.
  Chunk reassembly strips EXACTLY one trailing CRLF - never a byte set, or
  binary firmware chunks ending in 0x0d/0x0a/0x2d corrupt. There's a self-check
  for this; keep it green.
- Enums `STATUS_NAMES` (0-9) and numbered `ERROR_CAUSES` come from
  `fw_update.py` (verified off a TP600, WDA 1.5.2). Don't renumber them.
- **`_lock` is an RLock and must stay one.** `logline()` takes it and is called
  from inside sections that already hold it (`_install_worker`'s terminal
  branches). A plain `Lock` there deadlocks the whole API the instant a real
  install finishes - every parameter read blocks forever. It survived months
  because it needs a full ~5 min rauc install to trigger, not a smoke test.
  Regression test: `tests/test_registry.py::test_install_completion_does_not_deadlock`.
- State machine: Inactive(0) -activate-> Prepared(2) -getuploadids-> upload
  -start-> Started(3) -rauc install-> Unconfirmed(4) -finish(mark-good)->
  Finished(8) -clear-> Inactive(0). Failure -> Error(7) + errorcause
  (200 if "signature" in rauc output, else 600).
- Auth is HTTP Basic over self-signed TLS (PFC/CC posture), not OAuth2/PAM.
  For the real stack front with `wago-wda:x86` (lighttpd+authd); don't grow api.py.

## Providers (read-only, Phase 0-1)

`api.py` dispatches every parameter through `providers.param_value(pid)`. A
provider module exports `PARAMS` (fixed ids), `RESOLVE` (dynamic ids, returns
`NOTFOUND` when it does not own the id), `METHODS`, `ENUMS`. Adding a namespace
= one module + one entry in `providers/__init__._MODULES`.

- IDs come from `~/Documents/mcp/wago-plc-mcp-server/docs/edge-fw31-parameters-raw.json`.
  **There is no `storage`, `metrics`, `log`, `softwareupdate` or `presets`
  namespace in WDA** - do not add one. A/B slots are `0-0-systems-{1,2}-*`;
  logs are `0-0-firmwareupdate-getlastlogentries`.
- Read-only means `current*`/actuals only. `custom*`/`static*` writes are Phase 3
  and gated on the watchdog-reboot issue. Absent backend -> report absent
  (`false`/`""`/`[]`), never a plausible-looking guess.
- **Ports need no mapping table.** `/etc/udev/rules.d/20-network-names.rules` on
  the edge already renames the NICs to `X1`/`X2` (+ `X11`/`X12` on expansion
  models), so ports are discovered by name and the instance id is the number in
  the name. `PORT_MAP` is only an escape hatch. Addon-card ports (`LAN_A`/`LAN_B`
  in those rules) have no number and get no instance - do not renumber them.
- Networking needs the host netns (`network_mode: host`); localusers needs the
  host `/etc/passwd` mounted. `/etc/shadow` is intentionally not mounted.
- **Host mode is not behind Docker's port mapping, so firewalld applies.** The
  edge's `public` zone (X1) allows only cockpit/dhcp/ssh; 443 must be opened with
  `firewall-cmd --permanent --zone=public --add-port=443/tcp`. A port-mapped
  container bypassed firewalld, which is why this never came up before.
- Docker's own `docker0`/`br-<netid>`/`veth*` are filtered from bridges AND
  routes: they are not device networking, and they would shift instance ids
  whenever a container starts.
- `0-0-presets-*` is the ONE namespace with no cassette entry, named deliberately
  by the maintainer on 2026-09-01. It is not licence for a second exception.
- Unverified: the SpeedDuplex enum. `1000/full` is reported as member 5 from a
  table inferred off the cassette; the real definition is not published, so no
  enum is served for it. Confirm against a genuine WDA before relying on it.

## Build / run / verify

```bash
# rebuild + smoke the API (host)
cd container && docker build -t wagoalex/wago-fw-update-edge-computer:bundle-latest .
docker run --rm -d --name wdat -e MODE=server -e WDA_PASSWORD=wago \
  -e KEYRING=/dev/null -p 18443:8443 wagoalex/wago-fw-update-edge-computer:bundle-latest
curl -sk -u admin:wago https://localhost:18443/wda/parameters/0-0-firmwareupdate-status
docker rm -f wdat

# provider + preset tests (in-container, per the global testing rule)
cd container && docker build --target dev -t wda-dev . \
  && docker run --rm --entrypoint python3 wda-dev -m pytest /tests -q

# the parse_byteranges CRLF self-check and the enum guard now live in
# tests/test_parse_byteranges.py and tests/test_registry.py - keep them green
```

Real-device validation (needs a route to the edge, usually unavailable from the
dev host): drive `container/docker-compose.server.yml` up on the edge, then run
the sibling `fw_update.py` against `PLC_IP=192.168.2.17:8080`. Confirm the full
activate->…->clear sequence. There is no host-side integration test for the
rauc install itself - it requires the real host rauc.service.

## Docker Hub

`wagoalex/wago-fw-update-edge-computer`: `bundle-latest`, `bundle-V040100_IX05`
(self-contained), `rauc` (non-embedded client). Push after any container change.

## Device facts

- Edge `192.168.2.17` (X1, static; X2 is DHCP), `compatible=WAGO Edge Computer
  752-9xxx`, grub, slots `rootfs.1 (A)` / `rootfs.2 (B)`, `/docker` = 33G work
  disk. Order number in the bundle is the literal placeholder `0752-9xxx`; set
  the real one before catalog-resolving against a live device.

## Style

- No em-dash characters anywhere; use a plain hyphen. Conventional commits
  (`feat:`, `fix:`, `docs:`, `chore:`).
