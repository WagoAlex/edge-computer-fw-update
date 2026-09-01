#!/usr/bin/env python3
"""Named desired-state fragments ("presets") - store only.

A preset is `{"name":…, "description":…, "parameters": {param-id: value}}`, i.e.
a WDA-parameter fragment, not a config format of its own. Predefined presets ship
read-only inside the image; custom ones live on the mounted data volume so they
survive a redeploy.

APPLY IS NOT IMPLEMENTED. Applying means writing `custom*`/`static*` parameters,
which is Phase 3 and gated on the watchdog-reboot issue. Nothing here touches the
device - it is a typed key/value store with a directory behind it.

`0-0-presets-*` is the one namespace in this API with no cassette entry behind
it - WDA has none. The name was chosen deliberately (2026-09-01) rather than
invented in passing; see providers/preset.py and CLAUDE.md.
"""
import json
import os
import re

PREDEFINED_DIR = os.environ.get("PRESET_DIR", os.path.join(os.path.dirname(__file__), "presets"))
CUSTOM_DIR = os.environ.get("CUSTOM_PRESET_DIR", "/app/data/presets")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class PresetError(ValueError):
    pass


def _path(directory, name):
    """Join under `directory`, rejecting anything that is not a bare safe name -
    a preset name reaches this from an HTTP body, so ../ must never resolve."""
    if not NAME_RE.match(name or ""):
        raise PresetError(f"invalid preset name: {name!r}")
    return os.path.join(directory, name + ".json")


def _load(path, predefined):
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("parameters"), dict):
        raise PresetError(f"malformed preset: {path}")
    data["predefined"] = predefined
    return data


def list_presets():
    """All presets, custom shadowing predefined of the same name."""
    out = {}
    for directory, predefined in ((PREDEFINED_DIR, True), (CUSTOM_DIR, False)):
        for fname in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
            if not fname.endswith(".json"):
                continue
            name = fname[:-5]
            try:
                out[name] = _load(os.path.join(directory, fname), predefined)
            except (OSError, ValueError):
                continue          # a corrupt file must not blank the whole list
            out[name]["name"] = name
    return [out[n] for n in sorted(out)]


def get(name):
    for directory, predefined in ((CUSTOM_DIR, False), (PREDEFINED_DIR, True)):
        path = _path(directory, name)
        if os.path.isfile(path):
            p = _load(path, predefined)
            p["name"] = name
            return p
    return None


def save(name, description, parameters):
    """Write a custom preset. Predefined presets are never overwritten in place -
    a custom one of the same name shadows them."""
    if not isinstance(parameters, dict):
        raise PresetError("parameters must be an object of param-id -> value")
    bad = [k for k in parameters if not str(k).startswith("0-0-")]
    if bad:
        # A preset is a WDA-parameter fragment. Anything else would be dead
        # weight the Phase 3 apply could never write.
        raise PresetError(f"not WDA parameter ids: {bad}")
    path = _path(CUSTOM_DIR, name)
    os.makedirs(CUSTOM_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"name": name, "description": description or "",
                   "parameters": parameters}, f, indent=1, sort_keys=True)
    os.replace(tmp, path)         # atomic: a yanked power cord leaves no half file
    return get(name)


def delete(name):
    """Delete a custom preset. Returns False if there was no custom preset to
    delete (predefined ones are in the image and cannot be removed)."""
    path = _path(CUSTOM_DIR, name)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
