#!/usr/bin/env python3
"""0-0-presets-* : named network-configuration fragments.

  list   -> {"Presets": [...]}                       every preset, custom first
  get    <- {"Name"}            -> {"Preset": {...}}
  save   <- {"Name","Description","Parameters"}      writes a custom preset
  delete <- {"Name"}            -> {"Deleted": bool} custom only
  apply  <- {"Name"}            -> error: Phase 3

A preset holds `custom*`/`static*` parameters - IP addresses per port
(X1/X2/X11/X12 and expansion ports), DNS servers, routes - so one call can put a
known network configuration on a box. APPLY IS NOT IMPLEMENTED: it writes device
config, which is Phase 3 and gated on the watchdog-reboot issue. It returns an
explicit WDA error rather than a 404, so a caller learns the method exists and is
not available yet instead of guessing the URL is wrong.

NAMING: `presets` is NOT in the FW31 cassette - it is the one deliberate
exception to "every resource uses WAGO nomenclature", chosen by the maintainer on
2026-09-01 because WDA has no equivalent. Do not treat it as licence for more.
"""
import presets

DSC_NOT_IMPLEMENTED = "1"


def _err(detail):
    return None, DSC_NOT_IMPLEMENTED, detail


def _name(inargs):
    return inargs.get("Name", {}).get("value")


def m_list(inargs):
    return {"Presets": {"value": presets.list_presets()}}, None, None


def m_get(inargs):
    try:
        p = presets.get(_name(inargs))
    except presets.PresetError as e:
        return _err(str(e))
    if p is None:
        return _err(f"no such preset: {_name(inargs)}")
    return {"Preset": {"value": p}}, None, None


def m_save(inargs):
    try:
        p = presets.save(_name(inargs),
                         inargs.get("Description", {}).get("value", ""),
                         inargs.get("Parameters", {}).get("value"))
    except presets.PresetError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"cannot write preset: {e}")
    return {"Preset": {"value": p}}, None, None


def m_delete(inargs):
    try:
        deleted = presets.delete(_name(inargs))
    except presets.PresetError as e:
        return _err(str(e))
    return {"Deleted": {"value": deleted}}, None, None


def m_apply(inargs):
    return _err("preset apply is not implemented: it writes custom* parameters, "
                "which is Phase 3")


METHODS = {"0-0-presets-list": m_list, "0-0-presets-get": m_get,
           "0-0-presets-save": m_save, "0-0-presets-delete": m_delete,
           "0-0-presets-apply": m_apply}
