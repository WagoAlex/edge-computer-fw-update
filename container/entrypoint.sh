#!/bin/sh
# WAGO Edge firmware update - RAUC A/B, driven against the host rauc.service
# over D-Bus. Log style follows the WAGO fwupdate house format: "==>" phases
# with 4-space-indented detail, "FATAL:" for hard stops, a closing summary.
set -eu

# MODE=server -> long-running WDA-shaped REST API (POST /files, status/version
# endpoints). Anything else -> the one-shot install below. Both drive the same
# host rauc.service; the API just adds a REST surface on top.
if [ "${MODE:-oneshot}" = "server" ]; then
  exec python3 /api.py
fi

EMBEDDED="${BUNDLE:-/firmware/bundle.raucb}"        # inside the image
STAGE_DIR="${STAGE_DIR:-/docker/rauc-stage}"        # SAME path host+container (bind mount)
KEYRING="${KEYRING:-/etc/rauc/keyring.pem}"         # host keyring, mounted ro
STAGED="$STAGE_DIR/bundle.raucb"

show()  { printf '%s\n' "$*"; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; [ -n "${1:-}" ] && shift; for l in "$@"; do printf '    %s\n' "$l" >&2; done; rm -f "$STAGED" 2>/dev/null || true; exit 1; }
# booted/active slot from `rauc status` (e.g. "rootfs.1 (A)")
booted_slot() { rauc status 2>/dev/null | sed -n 's/^Booted from:[[:space:]]*//p'; }
active_slot() { rauc status 2>/dev/null | sed -n 's/^Activated:[[:space:]]*//p' | head -1; }

show "======================================================================"
show " WAGO Edge Computer - Firmware Update (RAUC A/B)"
show "======================================================================"

show "==> Checking host RAUC service"
rauc status >/dev/null 2>&1 || fatal "cannot reach host rauc.service over D-Bus" \
  "is /run/dbus/system_bus_socket mounted into this container?"
FROM_SLOT="$(booted_slot)"
show "    running slot: ${FROM_SLOT:-unknown}"

show "==> Preparing bundle"
[ -f "$EMBEDDED" ] || fatal "embedded bundle missing: $EMBEDDED"
[ -f "$KEYRING" ]  || fatal "keyring not mounted at $KEYRING" \
  "mount the device's /etc/rauc/keyring.pem read-only"
mkdir -p "$STAGE_DIR"
cp "$EMBEDDED" "$STAGED"
show "    staged to host-visible $STAGED"

show "==> Verifying bundle"
INFO="$(rauc info --keyring "$KEYRING" "$STAGED" 2>&1)" || fatal "signature/verify failed" "$INFO"
VER="$(printf '%s\n' "$INFO"   | sed -n "s/^Version:[[:space:]]*'\(.*\)'/\1/p")"
COMPAT="$(printf '%s\n' "$INFO"| sed -n "s/^Compatible:[[:space:]]*'\(.*\)'/\1/p")"
SIGNER="$(printf '%s\n' "$INFO"| sed -n "s/.*Verified inline signature by '\(.*\)'.*/\1/p" | head -1)"
show "    compatible: ${COMPAT:-?}"
show "    version:    ${VER:-?}"
show "    signature:  verified (${SIGNER:-self-signed})"

if [ "${DRY_RUN:-false}" = "true" ]; then
  show "==> DRY_RUN=true - service reachable and bundle verified. Not installing."
  rm -f "$STAGED"
  rmdir "$STAGE_DIR" 2>/dev/null || true
  show "==> Done (dry run)."
  exit 0
fi

show "==> Installing to inactive slot"
# stdbuf line-buffers both ends so RAUC's progress streams live instead of
# block-buffering through the pipe and dumping all at once at 100%.
stdbuf -oL -eL rauc install "$STAGED" 2>&1 | stdbuf -oL sed 's/^/    /' \
  || fatal "install failed" "see the RAUC output above"

TO_SLOT="$(active_slot)"
# clean up: staged bundle + the staging dir (if we can) so nothing is left on /docker
rm -f "$STAGED"
rmdir "$STAGE_DIR" 2>/dev/null || true
show "    cleaned up staged bundle"

show "======================================================================"
show "==> Update complete"
show "    firmware:   ${VER:-?}  (${COMPAT:-?})"
show "    was booted: ${FROM_SLOT:-unknown}"
show "    next boot:  ${TO_SLOT:-inactive slot}   <-- staged, not yet active"
show "    action:     reboot the device to activate; on failure RAUC falls"
show "                back to ${FROM_SLOT:-the current slot} automatically."
show "======================================================================"

# Reboot is OPT-IN. Default off: a container rebooting its own host loses the
# success log/exit status and turns every rollout into an immediate outage.
if [ "${REBOOT:-false}" = "true" ]; then
  show "==> REBOOT=true - rebooting host via systemd (login1) over D-Bus"
  dbus-send --system --print-reply --dest=org.freedesktop.login1 \
    /org/freedesktop/login1 org.freedesktop.login1.Manager.Reboot boolean:true \
    >/dev/null 2>&1 || fatal "reboot request failed - reboot the device manually"
fi
