#!/bin/bash
# Run ON the Edge (192.168.2.17, as root). Captures the live rootfs and packs
# a signed verity RAUC bundle compatible with this device.
# Output: /docker/edge-build/WAGO_OS0752-9xxx_Edge_FW5_V040100_IX05.raucb
set -euo pipefail

COMPATIBLE="WAGO Edge Computer 752-9xxx"   # must match /etc/rauc/system.conf
VERSION="4.1.0"                            # V040100
BUILD="$(date -u +%FT%T%z)"
OUT=/docker/edge-build
NAME=WAGO_OS0752-9xxx_Edge_FW5_V040100_IX05

command -v rauc >/dev/null || { echo "rauc missing"; exit 1; }
# squashfs-tools (mksquashfs) is required to build a .raucb and is NOT in the
# base OS. Install it before tarring so it is captured into the rootfs image -
# every device flashed from the resulting .wup can then build its own bundles.
if ! command -v mksquashfs >/dev/null; then
  echo ">> installing squashfs-tools (missing from base image)"
  apt-get update && apt-get install -y --no-install-recommends squashfs-tools
fi

rm -rf "$OUT"; mkdir -p "$OUT/content"

# Self-signed signing cert if none present
CERT=/etc/rauc/cert.pem
KEY=/etc/rauc/key.pem
KEYRING=/etc/rauc/keyring.pem
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
  echo ">> generating self-signed signing cert"
  openssl req -x509 -newkey rsa:4096 -nodes -keyout "$KEY" -out "$CERT" \
    -subj "/O=WAGO/CN=WAGO Edge Self-Signed" -days 3650
  chmod 600 "$KEY"
fi

# Register the cert as the verification keyring so `rauc install` accepts this
# self-signed bundle. Captured into the rootfs below, so every device flashed
# from this .wup trusts the same self-signed line - no per-device prep needed.
cp "$CERT" "$KEYRING"
if ! grep -q '^\[keyring\]' /etc/rauc/system.conf; then
  echo ">> adding [keyring] to /etc/rauc/system.conf"
  printf '\n[keyring]\npath=%s\n' "$KEYRING" >> /etc/rauc/system.conf
fi

# RAUC mounts verity bundles via a loop device + dm-verity. Ensure both modules
# load at boot so `rauc install` works on every device flashed from this .wup
# (otherwise: "Failed to open /dev/loop-control").
echo ">> ensuring loop + dm-verity load at boot"
mkdir -p /etc/modules-load.d
printf 'loop\ndm-verity\n' > /etc/modules-load.d/rauc.conf

# Tar the running rootfs (exclude volatile mounts, /docker data, and the build dir)
echo ">> tarring rootfs (this takes a while)"
tar --numeric-owner --one-file-system \
    --exclude=./tmp/* --exclude=./proc/* --exclude=./sys/* \
    --exclude=./dev/* --exclude=./run/* --exclude=./mnt/* \
    --exclude=./media/* --exclude=./docker/* --exclude=./lost+found \
    --exclude=./etc/rauc/key.pem \
    -czf "$OUT/content/rootfs.tar.gz" -C / .

# No sha256/size here: `rauc bundle` computes and writes them itself.
# Pre-filling them makes RAUC assert on them and abort ("Unexpected digest").
cat > "$OUT/content/manifest.raucm" <<MANIFEST
[update]
compatible=$COMPATIBLE
version=$VERSION
build=$BUILD

[bundle]
format=verity

[image.rootfs]
filename=rootfs.tar.gz
MANIFEST

echo ">> building verity bundle"
rauc bundle --cert="$CERT" --key="$KEY" "$OUT/content" "$OUT/$NAME.raucb"
rauc info --cert="$CERT" "$OUT/$NAME.raucb" || true
echo ">> done: $OUT/$NAME.raucb"
