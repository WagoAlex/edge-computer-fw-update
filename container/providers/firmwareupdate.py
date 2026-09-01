#!/usr/bin/env python3
"""0-0-firmwareupdate-* : the WDA update state machine, backed by rauc.

Moved verbatim out of api.py (Phase 0 is behaviour-preserving). State machine:
  Inactive(0) --activate--> Prepared(2) --getuploadids--> (upload /files)
    --start--> Started(3) --rauc install--> Unconfirmed(4)
    --finish--> Finished(8) (rauc mark-good) --clear--> Inactive(0)
  any failure -> Error(7) with a numbered errorcause.
"""
import os
import re
import shutil
import subprocess
import threading
import uuid

import wdalog

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
    return subprocess.run(cmd, capture_output=True, text=True)


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
    run(["rauc", "status", "mark-good"])
    with _lock:
        st["status"] = 8                                  # Finished
    logline("finish -> mark-good -> Finished")
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

ENUMS = {"0-0-firmwareupdate-status": STATUS_NAMES,
         "0-0-firmwareupdate-errorcause": ERROR_CAUSES}
