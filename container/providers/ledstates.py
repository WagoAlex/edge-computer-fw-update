#!/usr/bin/env python3
"""0-0-ledstates : the one LED this device can honestly report.

A PFC300 publishes five LEDs (SYS, RUN, IO, ...) because its firmware owns them.
An x86 edge owns none of that: PWR, HDD and BTR are wired to the power rail, the
SATA activity pin and the RTC battery circuit, and nothing in software drives
them. So this serves a single instantiation - WDA's RUN LED, backed by the
device's PWR/running state - rather than inventing four more.

  LEDStates/1/Name                    "RUN"
  LEDStates/1/Colors                  [LEDColor], 2 entries = blinks between them
  LEDStates/1/DiagnosticInformation   why it is that colour

Source, in order:
  1. LED_PWR_SYSFS (or an auto-found /sys/class/leds/*pwr*) - the real LED, if
     this platform exposes one. Set it once someone checks the hardware.
  2. systemd SystemState over the host D-Bus this container already mounts.

ponytail: no LED daemon, no polling thread. A GET reads the state; a 5s cache
keeps a burst of parameter reads from forking busctl five times.
"""
import glob
import os

from . import cached
from . import hostcfg

# LEDColor, read off a real PFC300 at /wda/parameter-definitions/.../enum.
RED, GREEN, YELLOW, BLUE, CYAN, MAGENTA, WHITE, OFF = range(8)
COLORS = {RED: "LED_COLOR_RED", GREEN: "LED_COLOR_GREEN", YELLOW: "LED_COLOR_YELLOW",
          BLUE: "LED_COLOR_BLUE", CYAN: "LED_COLOR_CYAN", MAGENTA: "LED_COLOR_MAGENTA",
          WHITE: "LED_COLOR_WHITE", OFF: "LED_COLOR_OFF"}

NAME = os.environ.get("LED_RUN_NAME", "RUN")
SYSFS = os.environ.get("SYSFS_LEDS", "/sys/class/leds")
# Point this at the real node once the hardware is known, e.g. LED_PWR_SYSFS=
# /sys/class/leds/platform::power. Empty = auto-discover, then fall back.
PWR_SYSFS = os.environ.get("LED_PWR_SYSFS", "")

# systemd SystemState -> (colors, text). Two colours mean the LED blinks between
# them, which is how WAGO encodes "working but not nominal".
_BY_STATE = {
    "running":      ([GREEN], "System running"),
    "degraded":     ([GREEN, OFF], "System degraded: one or more units failed"),
    "starting":     ([GREEN, OFF], "System starting"),
    "initializing": ([GREEN, OFF], "System initializing"),
    "maintenance":  ([RED], "System in maintenance mode"),
    "stopping":     ([RED, OFF], "System shutting down"),
}


def _sysfs_node():
    if PWR_SYSFS:
        return PWR_SYSFS
    for pat in ("*pwr*", "*power*"):
        hits = sorted(glob.glob(os.path.join(SYSFS, pat)))
        if hits:
            return hits[0]
    return ""


def _from_sysfs(node):
    """(colors, text) from a real LED node, or None if it cannot be read."""
    try:
        with open(os.path.join(node, "brightness")) as f:
            lit = int((f.read().strip() or "0")) > 0
    except (OSError, ValueError):
        return None
    name = os.path.basename(node)
    return ([GREEN], f"PWR LED lit ({name})") if lit else \
           ([OFF], f"PWR LED off ({name})")


@cached(5)
def _state():
    node = _sysfs_node()
    if node:
        got = _from_sysfs(node)
        if got is not None:
            return got
    st = hostcfg.systemd_state()
    if st in _BY_STATE:
        return _BY_STATE[st]
    # The API answered, so the device is powered and running something. Saying
    # GREEN here would claim more than we checked; say so instead.
    return ([GREEN, OFF], "System state unavailable (no LED node, systemd unreachable)")


PARAMS = {
    "0-0-ledstates": lambda: [{"Classes": ["LED"], "Id": 1}],
    "0-0-ledstates-1-name": lambda: NAME,
    "0-0-ledstates-1-colors": lambda: _state()[0],
    "0-0-ledstates-1-diagnosticinformation": lambda: _state()[1],
}

ENUMS = {"0-0-ledstates-1-colors": COLORS}
