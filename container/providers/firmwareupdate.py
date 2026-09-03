#!/usr/bin/env python3
"""0-0-firmwareupdate-* : the WDA update state machine, backed by rauc.

Moved verbatim out of api.py (Phase 0 is behaviour-preserving). State machine:
  Inactive(0) --activate--> Prepared(2) --getuploadids--> (upload /files)
    --start--> Started(3) --rauc install--> Unconfirmed(4)
    --finish--> Finished(8) (staged, not yet live) --clear--> Inactive(0)
  any failure -> Error(7) with a numbered errorcause.

Activation is the second half and does NOT fit in that machine, because the
reboot that activates a slot also restarts this container. `rauc install` writes
the INACTIVE slot and marks it primary; the device keeps running the old slot
until it reboots, and the bootloader will fall back unless the new slot is
marked good once it is running. So:

  finish   -> Finished(8): flashed and staged. Nothing is confirmed yet.
  reboot   -> explicit opt-in method. Never implicit, never a side effect.
  confirm  -> `rauc status mark-good booted`, only once the staged slot IS the
              booted one. Refused before the reboot, where mark-good would
              confirm the slot being replaced.

`0-0-firmwareupdate-activationstate` reads that state back out of rauc rather
than out of this process, so it is still correct after the reboot wiped the
in-memory machine: Unconfirmed(4) while a slot is pending or the booted slot is
not marked good, Confirmed(5) once it is.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import uuid

import wdalog

from . import cached
from . import hostcfg
from . import meta

EMBEDDED = os.environ.get("BUNDLE", "/firmware/bundle.raucb")
STAGE_DIR = os.environ.get("STAGE_DIR", "/docker/rauc-stage")
KEYRING = os.environ.get("KEYRING", "/etc/rauc/keyring.pem")

# WDA enums (verified live off a TP600, WDA 1.5.2 - see fw_update.py)
STATUS_NAMES = {0: "Inactive", 1: "Init", 2: "Prepared", 3: "Started",
                4: "Unconfirmed", 5: "Confirmed", 6: "Revert", 7: "Error",
                8: "Finished", 9: "NotAvailable"}
ERROR_CAUSES = {0: "NoError", 100: "InternalError", 101: "AbortByUser",
                102: "AbortInitializationFailed", 103: "AbortCheckSystemFailed",
                200: "SignatureInvalid", 300: "NotEnoughResources",
                301: "StopRuntimeFailed", 400: "SettingsBackupFailed",
                401: "FirmwareBackupFailed", 402: "UserBackupFailed",
                403: "SaveModifiedSettingsFailed", 500: "SettingsRestoreFailed",
                600: "UpdateFailed", 601: "SignatureTooNew", 602: "SignatureTooOld",
                603: "PartitionError", 604: "ErrorRevertNotSupported",
                700: "BootloaderUpdateFailed", 800: "RestartFailed",
                900: "SelftestFailed", 1000: "ConfirmationTimeout"}
# domainSpecificStatusCode values fw_update.py checks for
DSC_NOT_ACTIVATED = "95"
DSC_ALREADY_ACTIVE = "90"

# RLock, not Lock: logline() takes this lock and is called from inside sections
# that already hold it (the terminal branches of _install_worker). With a plain
# Lock the API deadlocks permanently the moment a real install finishes - every
# parameter read blocks forever - which only shows up after a full ~5 min rauc
# install, not in any short smoke test. Found on the edge 2026-09-01.
_lock = threading.RLock()
st = {"status": 0, "progress": 0, "errorcause": 0, "debuginfo": "",
      "revertable": True, "uploads": {}, "staged": None, "confirm_timeout": None}
_log = []  # ring of recent log lines


def logline(msg):
    """One update event: into the ring that 0-0-firmwareupdate-getlastlogentries
    serves, AND onto stdout so it is in `docker logs` with a timestamp. The ring
    is 200 entries and in memory; the docker log is the one that survives."""
    wdalog.update.info("%s", msg)
    with _lock:
        _log.append(msg)
        del _log[:-200]


def upload(fid):
    """Upload slot for a /files/{id} PATCH, or None. Owned here, not by the HTTP layer."""
    with _lock:
        return st["uploads"].get(fid)


def note_upload_size(up, end):
    with _lock:
        up["size"] = max(up["size"], end)


def run(cmd):
    """Never raises: a device without rauc installed is a result to report, not
    a traceback out of a parameter read. Same posture as hostcfg._busctl."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: {e}")


def rauc_info(path):
    r = run(["rauc", "info", "--keyring", KEYRING, path])
    if r.returncode != 0:
        return False, None, None, (r.stderr or r.stdout).strip()
    out = r.stdout
    ver = re.search(r"^Version:\s*'(.*)'", out, re.M)
    comp = re.search(r"^Compatible:\s*'(.*)'", out, re.M)
    return True, (ver.group(1) if ver else None), (comp.group(1) if comp else None), None


def _install_worker(path, stage_from=None):
    # Runs in a background thread so `start` returns immediately. If stage_from is
    # set (embedded bundle inside the image), copy it to the host-visible path
    # FIRST - the copy is ~1.3 GB, must not block the HTTP request.
    if stage_from:
        logline(f"staging embedded bundle -> {path}")
        try:
            shutil.copyfile(stage_from, path)
        except OSError as e:
            with _lock:
                st.update(status=7, errorcause=600, debuginfo=f"stage failed: {e}")
            logline(f"stage FAILED: {e}")
            return
    logline(f"rauc install {path}")
    proc = subprocess.Popen(["rauc", "install", path],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in proc.stdout:
        line = line.rstrip()
        tail.append(line)
        del tail[:-40]
        m = re.match(r"\s*(\d+)%", line)
        if m:
            with _lock:
                st["progress"] = int(m.group(1))
    proc.wait()
    with _lock:
        if proc.returncode == 0:
            st["status"], st["progress"] = 4, 100        # Unconfirmed - awaiting finish
            logline("install done -> Unconfirmed (call finish)")
        else:
            blob = "\n".join(tail).lower()
            st["status"] = 7                              # Error
            st["errorcause"] = 200 if "signature" in blob else 600
            st["debuginfo"] = "\n".join(tail[-8:])
            logline(f"install FAILED -> errorcause {st['errorcause']}")


# ---- slot state: what rauc says, not what this process remembers -----------
# Cached: activationstate is polled, and one `rauc status` per GET on a small
# box is a subprocess we do not need.

@cached(5)
def rauc_slots():
    """(booted, pending, booted_is_good).

    booted  slot name currently running, e.g. "rootfs.1" ("" if rauc is absent)
    pending slot marked primary for the next boot when it is NOT the booted one,
            i.e. an installed update waiting for a reboot; "" otherwise
    booted_is_good  the booted slot is marked good - the bootloader will not
            fall back away from it

    Measured on the edge 2026-09-02: `booted` in rauc's own JSON is a device
    path (/dev/sda2), not a slot name, so the booted slot is the one whose
    state is "booted" - never that field.
    """
    r = run(["rauc", "status", "--output-format=json"])
    if r.returncode != 0:
        return "", "", True
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return "", "", True
    booted, good = "", True
    for entry in d.get("slots", []):
        for name, slot in entry.items():
            if slot.get("state") == "booted":
                booted, good = name, slot.get("boot_status") == "good"
    primary = d.get("boot_primary") or ""
    return booted, ("" if primary == booted else primary), good


def activation_state():
    """0-0-firmwareupdate-activationstate, in STATUS_NAMES numbering."""
    booted, pending, good = rauc_slots()
    if not booted:
        return 9                                  # NotAvailable - no rauc here
    if pending or not good:
        return 4                                  # Unconfirmed
    return 5                                      # Confirmed


# ---- method implementations (return (outArgs|None, err_dsc|None, detail)) ----

def m_activate(inargs):
    with _lock:
        if st["status"] not in (0, 9):
            return None, DSC_ALREADY_ACTIVE, "firmware update already active"
        try:
            os.makedirs(STAGE_DIR, exist_ok=True)
        except OSError as e:
            # STAGE_DIR is a bind mount from the host. If it is missing or not
            # writable, say so as a WDA error - an unhandled OSError here kills
            # the connection and the client sees no response at all.
            return None, "1", f"stage directory {STAGE_DIR} unusable: {e}"
        st.update(status=2, progress=0, errorcause=0, debuginfo="", uploads={}, staged=None)
    logline("activate -> Prepared")
    return {}, None, None


def m_getuploadids(inargs):
    names = inargs.get("FileNames", {}).get("value", [])
    with _lock:
        if st["status"] < 2:
            return None, DSC_NOT_ACTIVATED, "firmware update not activated"
        ids = []
        for name in names:
            fid = uuid.uuid4().hex[:16]
            p = os.path.join(STAGE_DIR, fid + ".raucb")
            try:
                open(p, "wb").close()
            except OSError as e:
                return None, "1", f"cannot create upload file in {STAGE_DIR}: {e}"
            st["uploads"][fid] = {"name": name, "path": p, "size": 0}
            ids.append(fid)
    logline(f"getuploadids {names} -> {ids}")
    return {"UploadFiles": {"value": ids}}, None, None


def m_start(inargs):
    ids = inargs.get("UploadFiles", {}).get("value", [])
    with _lock:
        if st["status"] < 2:
            return None, DSC_NOT_ACTIVATED, "firmware update not activated"
        if ids:                                          # install an uploaded bundle
            if ids[0] not in st["uploads"]:
                return None, "1", "unknown upload id"
            path = st["uploads"][ids[0]]["path"]
            stage_from = None
        else:                                            # no UploadFiles -> built-in bundle
            if not os.path.isfile(EMBEDDED):
                return None, "1", "no embedded bundle and no UploadFiles given"
            try:
                os.makedirs(STAGE_DIR, exist_ok=True)
            except OSError as e:
                return None, "1", f"stage directory {STAGE_DIR} unusable: {e}"
            path = os.path.join(STAGE_DIR, "embedded.raucb")
            stage_from = EMBEDDED                         # copied in the worker, not here
        st.update(status=3, progress=0)                  # Started
    threading.Thread(target=_install_worker, args=(path,),
                     kwargs={"stage_from": stage_from}, daemon=True).start()
    logline(f"start {'embedded' if stage_from else ids} -> Started")
    return {}, None, None


def m_finish(inargs):
    with _lock:
        if st["status"] != 4:
            return None, "1", f"not finishable in status {st['status']}"
    with _lock:
        st["status"] = 8                                  # Finished
    rauc_slots.cache_clear()
    booted, pending, _good = rauc_slots()
    # Deliberately no mark-good here: we are still running the OLD slot, and
    # `rauc status mark-good` confirms the BOOTED one - it would confirm the
    # slot being replaced and do nothing at all for the update. Confirmation is
    # 0-0-firmwareupdate-confirm, after the reboot.
    logline(f"finish -> Finished; {pending or 'no slot'} staged for the next boot "
            f"(running {booted or 'unknown'}) - reboot, then confirm")
    return {}, None, None


def m_confirm(inargs):
    """`rauc status mark-good booted` - the last step of an A/B update.

    Refused while a slot is still pending: that means the staged slot has not
    been booted yet, and confirming here would mark the outgoing slot good.
    """
    rauc_slots.cache_clear()
    booted, pending, _good = rauc_slots()
    if not booted:
        return None, "1", "rauc reports no booted slot on this device"
    if pending:
        return None, "1", (f"{pending} is staged but not running yet - reboot "
                           f"(0-0-firmwareupdate-reboot), then confirm")
    r = run(["rauc", "status", "mark-good", "booted"])
    if r.returncode != 0:
        return None, "1", (r.stderr or r.stdout).strip() or "mark-good failed"
    rauc_slots.cache_clear()
    with _lock:
        st["status"] = 5                                  # Confirmed
    logline(f"confirm -> mark-good {booted} -> Confirmed")
    return {"Slot": {"value": booted}}, None, None


def m_reboot(inargs):
    """Explicit opt-in, and only that. Nothing else in this API reboots: an
    update is staged and stays staged until a caller asks for this by name and
    passes Confirm=true, so a client that merely replays the update sequence
    can never restart the device."""
    if inargs.get("Confirm", {}).get("value") is not True:
        return None, "1", "reboot requires inArgs Confirm=true - it is never implicit"
    logline("reboot requested -> logind Reboot")
    ok, detail = hostcfg.reboot()
    if not ok:
        return None, "1", f"logind refused the reboot: {detail}"
    return {}, None, None


def m_clear(inargs):
    with _lock:
        for u in st["uploads"].values():
            try:
                os.remove(u["path"])
            except OSError:
                pass
        st.update(status=0, progress=0, errorcause=0, debuginfo="", uploads={}, staged=None)
    logline("clear -> Inactive")
    return {}, None, None


def m_cancel(inargs):
    with _lock:
        st.update(status=0, errorcause=101, uploads={}, staged=None)  # AbortByUser
    logline("cancel -> Inactive (AbortByUser)")
    return {}, None, None


def m_getlastlogentries(inargs):
    n = inargs.get("EntryCount", {}).get("value", 25)
    with _lock:
        entries = _log[-int(n):]
    return {"Entries": {"value": entries}}, None, None


def m_settimeout(inargs):
    # WDA sets the confirmation timeout (seconds) before the update auto-reverts.
    to = inargs.get("Timeout", {}).get("value")
    with _lock:
        st["confirm_timeout"] = to
    logline(f"settimeout -> {to}")
    return {}, None, None


METHODS = {
    "0-0-firmwareupdate-activate": m_activate,
    "0-0-firmwareupdate-getuploadids": m_getuploadids,
    "0-0-firmwareupdate-start": m_start,
    "0-0-firmwareupdate-finish": m_finish,
    "0-0-firmwareupdate-confirm": m_confirm,
    "0-0-firmwareupdate-reboot": m_reboot,
    "0-0-firmwareupdate-clear": m_clear,
    "0-0-firmwareupdate-cancel": m_cancel,
    "0-0-firmwareupdate-settimeout": m_settimeout,
    "0-0-firmwareupdate-getlastlogentries": m_getlastlogentries,
}


def _snap(key):
    def get():
        with _lock:
            return st[key]
    return get


PARAMS = {f"0-0-firmwareupdate-{k}": _snap(k)
          for k in ("status", "progress", "errorcause", "debuginfo", "revertable")}
# The activation half. These read rauc, not `st`, so they are still true after
# the reboot that threw the in-memory machine away.
PARAMS.update({
    "0-0-firmwareupdate-activationstate": activation_state,
    "0-0-firmwareupdate-bootedslot": lambda: rauc_slots()[0],
    "0-0-firmwareupdate-pendingslot": lambda: rauc_slots()[1],
    "0-0-firmwareupdate-confirmed": lambda: rauc_slots()[2],
})

ENUMS = {"0-0-firmwareupdate-status": STATUS_NAMES,
         "0-0-firmwareupdate-activationstate": STATUS_NAMES,
         "0-0-firmwareupdate-errorcause": ERROR_CAUSES}

# The FW31 cassette is a dump of a device whose WDA has no activation surface,
# so these four carry their own metadata under the same FirmwareUpdate/ path.
meta.register({
    "0-0-firmwareupdate-activationstate":
        {"dataType": "enum_member", "dataRank": "scalar",
         "path": "FirmwareUpdate/ActivationState"},
    "0-0-firmwareupdate-bootedslot":
        {"dataType": "string", "dataRank": "scalar", "path": "FirmwareUpdate/BootedSlot"},
    "0-0-firmwareupdate-pendingslot":
        {"dataType": "string", "dataRank": "scalar", "path": "FirmwareUpdate/PendingSlot"},
    "0-0-firmwareupdate-confirmed":
        {"dataType": "boolean", "dataRank": "scalar", "path": "FirmwareUpdate/Confirmed"},
})
