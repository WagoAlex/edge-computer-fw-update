#!/usr/bin/env python3
"""WDA parameter metadata: dataType, dataRank and the WAGO path for an id.

A real WDA returns these alongside the value:

    "attributes": {"dataRank": "scalar", "dataType": "enum_member",
                   "path": "FirmwareUpdate/Status", "value": 0}

They are not derivable from the id - `0-0-networking-dns-utilizeddnsservers` is
`Networking/DNS/UtilizedDNSServers`, and no casing rule produces DNS, MACAddress
or IPConfiguration. So they come from `wda_meta.json`, generated from the FW31
cassette, never hand-written.

Instance ids the cassette does not list (an expansion port X11 gives
`ethernetports-11-*`, a third route gives `currentroutes-3-*`) fall back to the
instance-1 entry with the number substituted into the path. Anything still
unknown gets its dataType inferred from the Python value, which is the only
honest option left - better than claiming a type we do not know.
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "wda_meta.json")) as _f:
    PARAM_META = json.load(_f)["parameters"]

# ...-<namespace>-<n>-<rest> : the <n> is an instance number
_INSTANCE = re.compile(r"^(0-0-.*?)-(\d+)-(.+)$")

_PY_TO_WDA = {bool: "boolean", int: "uint32", str: "string", float: "double"}


def _infer(value):
    if isinstance(value, list):
        inner = _PY_TO_WDA.get(type(value[0]), "string") if value else "string"
        return {"dataType": inner, "dataRank": "array"}
    return {"dataType": _PY_TO_WDA.get(type(value), "string"), "dataRank": "scalar"}


def register(mapping):
    """Metadata for ids the FW31 cassette cannot cover.

    The cassette is a dump of a real edge, so it has nothing for a namespace the
    edge does not run - `0-0-wds*` comes from pp_wds, which ships only in the
    arm64 ipk. A provider that serves such ids supplies their metadata here and
    documents where it came from; wda_meta.json stays generated-only.
    """
    for pid, m in mapping.items():
        PARAM_META.setdefault(pid, m)


def describe(pid, value):
    """{"dataType","dataRank","path"} for a parameter id."""
    m = PARAM_META.get(pid)
    if m:
        return dict(m)
    inst = _INSTANCE.match(pid)
    if inst:
        head, num, tail = inst.groups()
        base = PARAM_META.get(f"{head}-1-{tail}")
        if base:
            m = dict(base)
            # the cassette path carries the instance number; swap ours in
            m["path"] = re.sub(r"/1(?=/)", f"/{num}", m["path"], count=1)
            return m
    m = _infer(value)
    m["path"] = ""          # unknown - say so rather than fabricate a path
    return m
