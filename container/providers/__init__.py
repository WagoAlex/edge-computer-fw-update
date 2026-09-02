"""Provider registry: WDA parameter/method IDs -> thin projections over daemons.

Each provider module exports any of:
  PARAMS  : {param-id: () -> value}          fixed IDs
  RESOLVE : (param-id) -> value | NOTFOUND   dynamic IDs (e.g. localusers-<uid>)
  WRITES  : {param-id: (value) -> effective} writable IDs; raises WriteError
  METHODS : {method-id: (inargs) -> (outArgs|None, dsc|None, detail)}
  ENUMS   : {param-id: {int: name}}          served at /parameter-definitions/../enum

A parameter is writable if and only if it is in WRITES; that same set is what
`writeable` reports on a parameter definition, so a client cannot be told one
thing and refused another.

ponytail: a dict merge, not a plugin framework. Providers are imported explicitly
below - a directory scan would buy nothing and hide import errors.
"""
import functools
import time

NOTFOUND = object()


class WriteError(Exception):
    """A write that could not be applied. `status` is the HTTP status the WDA
    spec wants for that reason: 400 invalid value, 503 backend refused/absent,
    500 applied but not persisted."""

    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


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


from . import firmwareupdate, ledstates, localusers, networking, preset, system, wds  # noqa: E402

_MODULES = (firmwareupdate, ledstates, localusers, networking, preset, system, wds)

PARAMS = {}
METHODS = {}
ENUMS = {}
WRITES = {}
for _m in _MODULES:
    PARAMS.update(getattr(_m, "PARAMS", {}))
    METHODS.update(getattr(_m, "METHODS", {}))
    ENUMS.update(getattr(_m, "ENUMS", {}))
    WRITES.update(getattr(_m, "WRITES", {}))
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


def writable(pid):
    return pid in WRITES


def set_param(pid, value):
    """Apply a value. Returns the EFFECTIVE value - a provider may normalise
    what it was given, and WDA distinguishes that case (200 with the effective
    value) from a verbatim write (204). Raises WriteError; KeyError if the id is
    not writable, which the transport turns into a 404 like any unknown id."""
    return WRITES[pid](value)
