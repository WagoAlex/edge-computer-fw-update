#!/usr/bin/env python3
"""0-0-localusers-<uid>-name | -ispasswordexpired  (read-only).

The instance id IS the uid - that is what the FW31 cassette shows (1=root,
13=www, 1001=user, 1003=admin), not a 1..n counter.

Deploy note: a container has its own /etc/passwd, so this only tells the truth
when the host's is mounted (PASSWD_FILE, see docker-compose.server.yml). The
host's /etc/shadow is deliberately NOT mounted by default - without it,
ispasswordexpired reports False rather than exposing hashes to the container.
"""
import os
import re
import time

from . import NOTFOUND, cached

PASSWD_FILE = os.environ.get("PASSWD_FILE", "/etc/passwd")
SHADOW_FILE = os.environ.get("SHADOW_FILE", "/etc/shadow")


@cached(30)
def _users():
    """{uid: {"name": str, "expired": bool}} for real login accounts."""
    users = {}
    try:
        lines = open(PASSWD_FILE).read().splitlines()
    except OSError:
        return users
    for line in lines:
        f = line.split(":")
        if len(f) < 7 or f[6].endswith(("/nologin", "/false", "/sync")):
            continue
        try:
            uid = int(f[2])
        except ValueError:
            continue
        # Cassette shows root as instance 1, not 0; every other account's
        # instance id is its uid (www=13, user=1001, admin=1003). uid 1 is
        # daemon, a nologin account, so it is already filtered out above.
        users[1 if uid == 0 else uid] = {"name": f[0], "expired": False}
    by_name = {u["name"]: u for u in users.values()}
    try:
        shadow = open(SHADOW_FILE).read().splitlines()
    except OSError:
        return users
    today = int(time.time() // 86400)
    for line in shadow:
        f = line.split(":")
        u = by_name.get(f[0]) if f else None
        if u is None:
            continue
        # expired if last-change is 0 (must change at next login) or beyond max age
        last, maxage = (f[2] if len(f) > 2 else ""), (f[4] if len(f) > 4 else "")
        if last == "0":
            u["expired"] = True
        elif last.isdigit() and maxage.isdigit():
            u["expired"] = today > int(last) + int(maxage)
    return users


PARAMS = {"0-0-localusers": lambda: [{"Classes": ["LocalUser"], "Id": uid}
                                     for uid in sorted(_users())]}

_RE = re.compile(r"^0-0-localusers-(\d+)-(name|ispasswordexpired)$")


def RESOLVE(pid):
    m = _RE.match(pid)
    if not m:
        return NOTFOUND
    u = _users().get(int(m.group(1)))
    if u is None:
        return NOTFOUND
    return u["name"] if m.group(2) == "name" else u["expired"]
