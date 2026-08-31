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
container/Dockerfile       debian-trixie-slim + rauc + dbus + python3, bundle
                           embedded at /firmware/bundle.raucb.
container/entrypoint.sh    MODE=server -> exec python3 /api.py; else one-shot
                           install (WAGO "==>" log style, opt-in REBOOT).
container/api.py           WDA-compatible REST API over rauc (see below).
```

## The REST API (container/api.py)

Mirrors WAGO's production WDA firmware-update surface so `fw_update.py` drives
it unchanged. Key invariants to preserve if you touch it:

- JSON:API (`application/vnd.api+json`), envelopes `{"data":{"id","type","attributes":{"value":…}}}`.
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
- State machine: Inactive(0) -activate-> Prepared(2) -getuploadids-> upload
  -start-> Started(3) -rauc install-> Unconfirmed(4) -finish(mark-good)->
  Finished(8) -clear-> Inactive(0). Failure -> Error(7) + errorcause
  (200 if "signature" in rauc output, else 600).
- No auth by design. For real auth, front with `wago-wda:x86` (lighttpd+authd),
  don't grow api.py.

## Build / run / verify

```bash
# rebuild + smoke the API (host)
cd container && docker build -t wagoalex/wago-fw-update-edge-computer:bundle-latest .
docker run --rm -d --name t -e MODE=server -e KEYRING=/dev/null -p 18080:8080 \
  wagoalex/wago-fw-update-edge-computer:bundle-latest
curl -s localhost:18080/wda/parameters/0-0-firmwareupdate-status ; docker rm -f t

# api.py self-checks (parser + enums) - run before committing api changes
cd container && python3 - <<'PY'
src=open("api.py").read().replace('if __name__ == "__main__":','if False:')
ns={}; exec(compile(src,"api.py","exec"),ns)
p=ns["parse_byteranges"]; B="b"
for pl in [b"\x0d", b"\x0a", b"-", bytes(range(256))]:
    body=(f"--{B}\r\nContent-Range: bytes 0-{len(pl)-1}/{len(pl)}\r\n\r\n").encode()+pl+f"\r\n--{B}--\r\n".encode()
    assert p(body,f"multipart/byteranges; boundary={B}")==(0,pl)
assert ns["ERROR_CAUSES"][602]=="SignatureTooOld"
print("ok")
PY
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
