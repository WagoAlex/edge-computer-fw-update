#!/bin/sh
# Drives a full RAUC A/B update against the host rauc.service over D-Bus.
# RAUC always installs to the INACTIVE slot and marks it for the next boot -
# the running slot is untouched, so a bad flash is a reboot away from recovery.
set -eu

BUNDLE="${BUNDLE:-/firmware/WAGO_OS0752-9xxx_V040100_IX05_I.raucb}"

echo "== rauc status (before) =="
rauc status || { echo "FATAL: cannot reach host rauc.service over D-Bus"; \
                 echo "  is /run/dbus/system_bus_socket mounted into this container?"; exit 1; }

[ -f "$BUNDLE" ] || { echo "FATAL: bundle not found in container: $BUNDLE (check the volume mount)"; exit 1; }

echo "== bundle =="
rauc info "$BUNDLE"

if [ "${DRY_RUN:-false}" = "true" ]; then
  echo "DRY_RUN=true: verified bundle + reached the service. Not installing."
  exit 0
fi

echo "== installing to the inactive slot =="
rauc install "$BUNDLE"

echo "== rauc status (after: target slot now primary for next boot) =="
rauc status

echo
echo "Update staged on the inactive slot. Reboot the device to boot it."
echo "If it boots bad, RAUC falls back to the current slot automatically."
