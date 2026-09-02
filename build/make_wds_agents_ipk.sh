#!/bin/bash
# Build an x86-64 wds-agents package the WAGO Edge Computer can actually consume.
#
# WHY THIS EXISTS
# Device Sphere's appload ships wds-agents-ptxdist-FW31-native_1.3.1_arm64.ipk.
# The edge is x86-64 Debian with no opkg, so nothing in it can run - our
# commissioning agent records the workflow and installs nothing. The two pieces
# that matter are then missing on the device:
#
#   pp_wds            the WDA provider serving 0-0-wds* / wdsdeployment* /
#                     wdsbackup* / wdsrestore*  -> replaced by the API's
#                     providers/wds.py, which serves the same ids
#   wds-device-agent  the persistent server channel -> the containerised python
#                     agent from the sibling edge-commisioning-service
#
# So this package does NOT reimplement those binaries. It installs the glue that
# points the device at the two x86 components that already exist, in the layout
# and with the control metadata Device Sphere expects, so an operator can upload
# it to WDS as the device's application package instead of the arm64 one.
#
# The archive format is opkg's older container: a GZIPPED TAR holding
# control.tar.gz, data.tar.gz and debian-binary - not ar(1). Checked against
# WAGO's own file, which `ar t` cannot read and `tar tzf` lists in that order.
set -euo pipefail

VERSION="${VERSION:-1.3.1}"
ARCH="${ARCH:-x86_64}"
PKG="wds-agents-edge-FW31-native"
OUT="${OUT:-$(cd "$(dirname "$0")/.." && pwd)/bundles}"
API_IMAGE="${API_IMAGE:-wagoalex/wago-fw-update-edge-computer:api-latest}"
AGENT_IMAGE="${AGENT_IMAGE:-wago-edge-commissioning:latest}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/data/opt/wago/wds/bin" \
         "$WORK/data/opt/wago/wds/share" \
         "$WORK/data/lib/systemd/system" \
         "$WORK/control" "$OUT"

# ---- data: what lands on the device ----------------------------------------

cat > "$WORK/data/opt/wago/wds/share/wds-agents.env" <<ENV
# Written by $PKG $VERSION. Read by the units below.
WDA_API_IMAGE=$API_IMAGE
WDS_AGENT_IMAGE=$AGENT_IMAGE
WDA_URL=https://127.0.0.1:443
ENV

# The device-agent stand-in: keeps the containerised agent running. Named the
# way the arm64 package names it so an operator finds the same thing here.
cat > "$WORK/data/opt/wago/wds/bin/wds-device-agent" <<'AGENT'
#!/bin/sh
# x86-64 stand-in for WAGO's arm64 /usr/bin/wds-device-agent. The channel logic
# lives in the container; this is the entry point systemd starts.
set -eu
. /opt/wago/wds/share/wds-agents.env
exec docker run --rm --name wds-edge-commissioning --network host \
  -v /etc/wds:/etc/wds -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
  "$WDS_AGENT_IMAGE"
AGENT

# The pp_wds stand-in is a health check, not a daemon: the parameters it would
# serve are served by the WDA API container (providers/wds.py). This script says
# so out loud and verifies they are actually answering, so a failed integration
# is visible at install time rather than at the first twin read.
cat > "$WORK/data/opt/wago/wds/bin/pp_wds-check" <<'CHECK'
#!/bin/sh
# 0-0-wds* is served by the WDA API container, not by a local pp_wds binary.
set -eu
. /opt/wago/wds/share/wds-agents.env
: "${WDA_USER:=admin}"
for p in 0-0-wds-heartbeatinterval 0-0-wdsdeployment-ipks 0-0-wdsbackup-enabled; do
  code=$(curl -sk -o /dev/null -w '%{http_code}' -u "$WDA_USER:${WDA_PASSWORD:-}" \
         "$WDA_URL/wda/parameters/$p" || echo 000)
  printf '%-46s %s\n' "$p" "$code"
  [ "$code" = 200 ] || exit 1
done
echo "0-0-wds* served by the WDA API - pp_wds not required on this device"
CHECK

chmod 755 "$WORK/data/opt/wago/wds/bin/wds-device-agent" \
          "$WORK/data/opt/wago/wds/bin/pp_wds-check"

# systemd, not /etc/rc.d: the edge is Debian. Same unit name as the arm64 init
# script so the two platforms are talked about the same way.
cat > "$WORK/data/lib/systemd/system/wds-device-agent.service" <<'UNIT'
[Unit]
Description=WAGO Device Sphere device agent (x86-64 edge)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
ExecStart=/opt/wago/wds/bin/wds-device-agent
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT

# ---- control: the metadata Device Sphere reads -----------------------------
# Mirrors WAGO's own control file, which is:
#   Package/Priority/Version/Description/Architecture/Maintainer, in that order.
cat > "$WORK/control/control" <<CTRL
Package: $PKG
Priority: optional
Version: $VERSION
Description: WDS Agents in native mode (x86-64 edge computer build)
Architecture: $ARCH
Maintainer: WAGO
CTRL

cat > "$WORK/control/postinst" <<'POST'
#!/bin/sh
set -e
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
  systemctl enable --now wds-device-agent.service || true
fi
exit 0
POST
cat > "$WORK/control/prerm" <<'PRE'
#!/bin/sh
set -e
if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now wds-device-agent.service || true
fi
exit 0
PRE
chmod 755 "$WORK/control/postinst" "$WORK/control/prerm"

# ---- assemble ---------------------------------------------------------------
( cd "$WORK/data"    && tar --numeric-owner --owner=0 --group=0 -czf ../data.tar.gz . )
( cd "$WORK/control" && tar --numeric-owner --owner=0 --group=0 -czf ../control.tar.gz . )
echo "2.0" > "$WORK/debian-binary"

IPK="$OUT/${PKG}_${VERSION}_${ARCH}.ipk"
rm -f "$IPK"
# Same member order as WAGO's file: control, data, debian-binary.
( cd "$WORK" && tar --numeric-owner --owner=0 --group=0 \
    -czf "$IPK" control.tar.gz data.tar.gz debian-binary )

echo ">> wrote $IPK"
tar tzvf "$IPK" | awk '{print "   ", $1, $3, $6}'
echo "   payload:"
tar tzf "$WORK/data.tar.gz" | grep -v '/$' | sed 's/^/      /'
