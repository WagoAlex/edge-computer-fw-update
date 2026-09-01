#!/usr/bin/env python3
"""Device identity, version, system time, A/B systems and memory card.

`0-0-systems-{1,2}-*` is the WAGO-native projection of the RAUC A/B slots - there
is no `storage` namespace in WDA and none is invented here:
  active     = the slot we booted from        (rauc "booted")
  configured = the slot is marked bootable    (rauc boot-status "good")
  available  = the slot exists in system.conf
Order/serial fall back to DMI so a fresh edge reports its real numbers without
being told them; ORDER_NUMBER/SERIAL_NUMBER env override for catalog work.
"""
import json
import os
import subprocess
import time

from . import cached

ORDER = os.environ.get("ORDER_NUMBER", "0752-9xxx")
DESCRIPTION = os.environ.get("DEVICE_DESCRIPTION", "WAGO Edge Computer")
FW_VERSION = os.environ.get("FIRMWARE_VERSION", "04.01.00")
HW_INDEX = os.environ.get("HARDWARE_RELEASE_INDEX", "")
SW_INDEX = os.environ.get("SOFTWARE_RELEASE_INDEX", "")
DMI = os.environ.get("DMI_DIR", "/sys/class/dmi/id")
SYS_BLOCK = os.environ.get("SYS_BLOCK", "/sys/block")
# RAUC slot name -> WDA system instance id. Edge slots are rootfs.1 / rootfs.2.
SLOTS = {"rootfs.1": 1, "rootfs.2": 2}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _serial():
    return os.environ.get("SERIAL_NUMBER") or _read(os.path.join(DMI, "product_serial"))


@cached(10)
def _rauc_status():
    """rauc status as {slot-name: {"booted": bool, "state": str, "good": bool}}."""
    try:
        r = subprocess.run(["rauc", "status", "--output-format=json"],
                           capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    out = {}
    for entry in data.get("slots", []):
        for name, info in entry.items():
            out[name] = {"booted": info.get("state") == "booted",
                         "good": info.get("boot_status") == "good",
                         "mounted": bool(info.get("mountpoint")) or "device" in info}
    return out


def _system(idx, attr):
    slot = next((n for n, i in SLOTS.items() if i == idx), None)
    info = _rauc_status().get(slot)
    if info is None:
        return False
    return {"active": info["booted"], "configured": info["good"],
            "available": info["mounted"]}[attr]


@cached(5)
def _memorycard():
    """SD/MMC presence from /sys/block - the edge normally has none."""
    try:
        cards = [d for d in os.listdir(SYS_BLOCK) if d.startswith("mmcblk")]
    except OSError:
        cards = []
    if not cards:
        return {"isavailable": False, "iswriteprotected": False, "volumename": ""}
    dev = sorted(cards)[0]
    ro = _read(os.path.join(SYS_BLOCK, dev, "ro")) == "1"
    return {"isavailable": True, "iswriteprotected": ro, "volumename": dev}


PARAMS = {
    "0-0-identity-ordernumber": lambda: ORDER,
    "0-0-identity-description": lambda: DESCRIPTION,
    "0-0-identity-serialnumber": _serial,
    "0-0-version-firmwareversion": lambda: FW_VERSION,
    "0-0-version-hardwarereleaseindex": lambda: HW_INDEX,
    "0-0-version-softwarereleaseindex": lambda: SW_INDEX,
    "0-0-systemtime-now": lambda: int(time.time()),
    "0-0-systemtime-local-now": lambda: time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    "0-0-systems": lambda: [{"Classes": ["SystemRecoverySystem"], "Id": i}
                            for i in sorted(SLOTS.values())],
    "0-0-memorycard-isavailable": lambda: _memorycard()["isavailable"],
    "0-0-memorycard-iswriteprotected": lambda: _memorycard()["iswriteprotected"],
    "0-0-memorycard-volumename": lambda: _memorycard()["volumename"],
}
for _i in sorted(SLOTS.values()):
    for _a in ("active", "configured", "available"):
        PARAMS[f"0-0-systems-{_i}-{_a}"] = (lambda i, a: lambda: _system(i, a))(_i, _a)
