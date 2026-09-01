"""Provider registry: WDA parameter/method IDs -> thin projections over daemons.

Each provider module exports any of:
  PARAMS  : {param-id: () -> value}          fixed IDs
  RESOLVE : (param-id) -> value | NOTFOUND   dynamic IDs (e.g. localusers-<uid>)
  METHODS : {method-id: (inargs) -> (outArgs|None, dsc|None, detail)}
  ENUMS   : {param-id: {int: name}}          served at /parameter-definitions/../enum

ponytail: a dict merge, not a plugin framework. Providers are imported explicitly
below - a directory scan would buy nothing and hide import errors.
"""
import functools
import time

NOTFOUND = object()


def cached(ttl):
    """Memoize a zero-arg backend read for ttl seconds - a burst of GETs on a
    small box must not fork one subprocess per parameter."""
    def deco(fn):
        box = {"t": 0.0, "v": None}

        @functools.wraps(fn)
        def wrapper():
            now = time.monotonic()
            if now - box["t"] >= ttl:
                box["v"] = fn()
                box["t"] = now
            return box["v"]
        wrapper.cache_clear = lambda: box.update(t=0.0, v=None)
        return wrapper
    return deco


from . import firmwareupdate, localusers, networking, preset, system  # noqa: E402

_MODULES = (firmwareupdate, localusers, networking, preset, system)

PARAMS = {}
METHODS = {}
ENUMS = {}
for _m in _MODULES:
    PARAMS.update(getattr(_m, "PARAMS", {}))
    METHODS.update(getattr(_m, "METHODS", {}))
    ENUMS.update(getattr(_m, "ENUMS", {}))
_RESOLVERS = [_m.RESOLVE for _m in _MODULES if hasattr(_m, "RESOLVE")]


def param_value(pid):
    """Value for a WDA parameter id, or None if this device has no such parameter."""
    fn = PARAMS.get(pid)
    if fn is not None:
        return fn()
    for r in _RESOLVERS:
        v = r(pid)
        if v is not NOTFOUND:
            return v
    return None
