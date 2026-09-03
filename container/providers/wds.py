#!/usr/bin/env python3
"""0-0-wds* : the models WAGO's `pp_wds` serves, for a device that cannot run it.

`pp_wds` is the WDA parameter provider inside
`wds-agents-ptxdist-FW31-native_1.3.1_arm64.ipk`. It is arm64/ptxdist and the
edge is x86-64 Debian with no opkg, so that provider can never run here - and
without it the Device Sphere twin has no management surface on the device.

The ids are not guessed. They are the strings WDS's own binary carries
(`Wago.Wdj.Application.dll`, UTF-16), read on 2026-09-02:

    0-0-wds-monitoringinterval / -heartbeatinterval / -schedule-configuration
    0-0-wdsdeployment-applicationinstancepackage / -applicationtemplateid
        / -applicationtemplateversion / -applicationtemplatecreationdate
        / -ipks / -bacnetconfiguration / -txtrecipes
    0-0-wdsbackup-interval / -enabled ; 0-0-wdsrestore-fileinfo

Defaults come from WAGO's own model files as recorded in the sibling project's
POST-ONBOARDING.md: MonitoringInterval 120, HeartbeatInterval 30.

Read/write semantics, explicitly - the ids fall into two kinds and nothing here
blurs them:

  LIVE DEVICE STATE - none. There is no wds daemon on this device to measure.

  STORED INTENT - all seventeen. A write is persisted verbatim to STORE and read
  back verbatim. The value means "this is what the server asked for", never
  "this is in effect". `applicationinstancepackage` is the sharp case: it is the
  server's install-this-application trigger, and this API neither installs
  applications nor claims to.

Who acts on the intent: the sibling `edge-commissioning-service`, by polling
these ids over this same REST API on its own schedule. There is no callback, no
message bus and no notification from here - the sibling polls, and that is
enough. Nothing in this module knows the sibling exists.
"""
import json
import os

import wdalog

from . import WriteError
from . import meta

STORE = os.environ.get("WDS_MODEL_STORE", "/app/data/wds-model.json")

# Defaults, from WAGO's pp_wds.wdm.json as documented in POST-ONBOARDING.md.
DEFAULTS = {
    "0-0-wds-monitoringinterval": 120,
    "0-0-wds-heartbeatinterval": 30,
    "0-0-wds-schedule-configuration": "",
    "0-0-wdsdeployment-applicationinstancepackage": "",
    "0-0-wdsdeployment-applicationtemplateid": "",
    "0-0-wdsdeployment-applicationtemplateversion": "",
    "0-0-wdsdeployment-applicationtemplatecreationdate": "",
    "0-0-wdsdeployment-ipks": [],
    "0-0-wdsdeployment-bacnetconfiguration": "",
    "0-0-wdsdeployment-txtrecipes": [],
    "0-0-wdsbackup-interval": 0,
    "0-0-wdsbackup-enabled": False,
    "0-0-wdsrestore-fileinfo": "",
}

# Instance lists, the way every other namespace here reports its members.
CLASSES = {"0-0-wds": "WDS", "0-0-wdsdeployment": "WDSDeployment",
           "0-0-wdsbackup": "WDSBackup", "0-0-wdsrestore": "WDSRestore"}


def _read():
    try:
        with open(STORE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(pid, value):
    data = _read()
    data[pid] = value
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STORE)
    except OSError as e:
        raise WriteError(500, f"value accepted but not persisted: {e}")


def _get(pid):
    return _read().get(pid, DEFAULTS[pid])


def _setter(pid, kind):
    def write(value):
        if kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise WriteError(400, "expected an integer")
            if not 0 <= value <= 86400:
                raise WriteError(400, "expected 0..86400 seconds")
        elif kind is bool and not isinstance(value, bool):
            raise WriteError(400, "expected a boolean")
        elif kind is str and not isinstance(value, str):
            raise WriteError(400, "expected a string")
        _write(pid, value)
        wdalog.write.info("%s set", pid)
        return value
    return write


def _deployment_trigger(value):
    """The server's install-this-application trigger: stored intent, never a
    live state. A client reads back exactly what it asked for, and the log says
    a deployment was REQUESTED - so no reader can mistake the stored value for
    an installed application."""
    # The server sends an object, not a string: a live WDS 1.3.1 target reads
    # {"startTime": null, "fileReference": "7176bcfd-…"}. Both shapes are taken
    # and stored verbatim, because the agent needs the fileReference and we must
    # not lose fields we do not yet understand.
    if not isinstance(value, (str, dict)):
        raise WriteError(400, "expected a string or an object")
    _write("0-0-wdsdeployment-applicationinstancepackage", value)
    ref = value.get("fileReference") if isinstance(value, dict) else value
    wdalog.write.warning("deployment requested (%s) - recorded, NOT installed: "
                         "no application agent on this device", ref or "<cleared>")
    return value


# The cassette has none of these - it is a dump of an edge, which has no pp_wds.
# Paths follow WAGO's own WDX model names as recorded in POST-ONBOARDING.md
# (WDS, WDSDeployment, WDSBackup, WDSRestore), the same CamelCase convention
# every other WDA path uses.
_PATHS = {
    "0-0-wds": "WDS",
    "0-0-wds-monitoringinterval": "WDS/MonitoringInterval",
    "0-0-wds-heartbeatinterval": "WDS/HeartbeatInterval",
    "0-0-wds-schedule-configuration": "WDS/Schedule/Configuration",
    "0-0-wdsdeployment": "WDSDeployment",
    "0-0-wdsdeployment-applicationinstancepackage": "WDSDeployment/ApplicationInstancePackage",
    "0-0-wdsdeployment-applicationtemplateid": "WDSDeployment/ApplicationTemplateId",
    "0-0-wdsdeployment-applicationtemplateversion": "WDSDeployment/ApplicationTemplateVersion",
    "0-0-wdsdeployment-applicationtemplatecreationdate": "WDSDeployment/ApplicationTemplateCreationDate",
    "0-0-wdsdeployment-ipks": "WDSDeployment/IPKs",
    "0-0-wdsdeployment-bacnetconfiguration": "WDSDeployment/BacnetConfiguration",
    "0-0-wdsdeployment-txtrecipes": "WDSDeployment/TxtRecipes",
    "0-0-wdsbackup": "WDSBackup",
    "0-0-wdsbackup-interval": "WDSBackup/Interval",
    "0-0-wdsbackup-enabled": "WDSBackup/Enabled",
    "0-0-wdsrestore": "WDSRestore",
    "0-0-wdsrestore-fileinfo": "WDSRestore/FileInfo",
}


def _meta(pid):
    if pid in CLASSES:
        return {"dataType": "instantiations", "dataRank": "array", "path": _PATHS[pid]}
    v = DEFAULTS[pid]
    if isinstance(v, bool):
        t, r = "boolean", "scalar"
    elif isinstance(v, int):
        t, r = "uint32", "scalar"
    elif isinstance(v, list):
        t, r = "string", "array"
    else:
        t, r = "string", "scalar"
    return {"dataType": t, "dataRank": r, "path": _PATHS[pid]}


META = {pid: _meta(pid) for pid in _PATHS}
meta.register(META)

PARAMS = {pid: (lambda p: lambda: _get(p))(pid) for pid in DEFAULTS}
PARAMS.update({base: (lambda c: lambda: [{"Classes": [c], "Id": 1}])(cls)
               for base, cls in CLASSES.items()})

WRITES = {
    "0-0-wds-monitoringinterval": _setter("0-0-wds-monitoringinterval", int),
    "0-0-wds-heartbeatinterval": _setter("0-0-wds-heartbeatinterval", int),
    "0-0-wds-schedule-configuration": _setter("0-0-wds-schedule-configuration", str),
    "0-0-wdsdeployment-applicationinstancepackage": _deployment_trigger,
    "0-0-wdsbackup-interval": _setter("0-0-wdsbackup-interval", int),
    "0-0-wdsbackup-enabled": _setter("0-0-wdsbackup-enabled", bool),
}
