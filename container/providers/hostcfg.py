#!/usr/bin/env python3
"""Host configuration over the system D-Bus socket this container already mounts.

The same transport, trust model and failure mode as the rauc calls: the container
stays unprivileged, systemd does the privileged part, and polkit decides whether
we may. No new mount, no new capability, no host-side helper to install.

  hostname   org.freedesktop.hostname1.SetStaticHostname   (systemd-hostnamed)
  DNS        org.freedesktop.resolve1.Manager.SetLinkDNS   (systemd-resolved)

`busctl` rather than a D-Bus library: it ships with systemd, which is already in
the image (rauc pulls it in), it speaks the `a(iay)` signature SetLinkDNS needs,
and driving it with subprocess is exactly how `rauc` is driven two modules over.

ponytail: no bus-connection caching, no async. A write is operator-paced - one
call every few minutes at most - so a 10 ms fork is not the cost that matters.
"""
import ipaddress
import os
import re
import socket
import subprocess

TIMEOUT = int(os.environ.get("DBUS_TIMEOUT", "10"))
SYS = os.environ.get("SYSFS_NET", "/sys/class/net")

HOSTNAME1 = ("org.freedesktop.hostname1", "/org/freedesktop/hostname1",
             "org.freedesktop.hostname1")
RESOLVE1 = ("org.freedesktop.resolve1", "/org/freedesktop/resolve1",
            "org.freedesktop.resolve1.Manager")
LOGIN1 = ("org.freedesktop.login1", "/org/freedesktop/login1",
          "org.freedesktop.login1.Manager")

_FAMILY = {4: socket.AF_INET, 6: socket.AF_INET6}


def _busctl(*args):
    """(ok, output). Never raises: a missing socket or a polkit refusal is a
    result to report, not a traceback that kills the request."""
    try:
        r = subprocess.run(["busctl", *args], capture_output=True, text=True,
                           timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"busctl failed: {e}"
    out = (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()
    return r.returncode == 0, out


def hostname():
    """The HOST's live hostname, or None if hostnamed is not reachable.

    Not socket.gethostname(): with `network_mode: host` Docker seeds the
    container's UTS namespace from the host at start, so that call returns a
    snapshot that never changes - it would still report the old name after a
    successful SetStaticHostname, which is precisely when it is read.
    """
    ok, out = _busctl("get-property", *HOSTNAME1, "Hostname")
    if not ok:
        return None
    m = re.match(r'^s\s+"(.*)"$', out)
    return m.group(1) if m else None


def set_static_hostname(name):
    """Persistent hostname (writes /etc/hostname on the host). (ok, detail)."""
    return _busctl("call", *HOSTNAME1, "SetStaticHostname", "sb", name, "false")


def reboot():
    """Ask logind to restart the host. (ok, detail).

    Reachable only from 0-0-firmwareupdate-reboot, which requires Confirm=true:
    a staged firmware slot goes live on the next boot and nothing in this API
    may decide when that is.
    """
    return _busctl("call", *LOGIN1, "Reboot", "b", "true")


def ifindex(ifname):
    try:
        with open(os.path.join(SYS, ifname, "ifindex")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def set_link_dns(idx, servers):
    """SetLinkDNS(i ifindex, a(iay) addresses) - resolved's per-link DNS.

    resolved has no global D-Bus setter (global DNS lives in resolved.conf), so
    the servers go on one link. This is runtime state, not persisted by resolved
    across a reboot; the custom value is stored on our side and re-applied at
    start, which is what makes it behave like a configured parameter.
    """
    args = [str(idx), str(len(servers))]
    for s in servers:
        ip = ipaddress.ip_address(s)
        packed = ip.packed
        args += [str(_FAMILY[ip.version]), str(len(packed))] + [str(b) for b in packed]
    return _busctl("call", *RESOLVE1, "SetLinkDNS", "ia(iay)", *args)


def resolved_available():
    """Is systemd-resolved actually on this bus? The edge is not: resolv.conf is
    a plain file and NetworkManager owns DNS, so SetLinkDNS would fail with a
    name-not-provided error that says nothing useful to an operator."""
    ok, _ = _busctl("get-property", *RESOLVE1, "DNS")
    return ok


def probe():
    """One line per backend, for the startup log: what can this container do?"""
    from . import nmcfg
    hn = hostname()
    ok_dns, out = _busctl("get-property", *RESOLVE1, "DNS")
    return {"hostname1": hn if hn is not None else "unreachable",
            "resolve1": "reachable" if ok_dns else "absent",
            "networkmanager": "reachable" if nmcfg.available() else "absent",
            "dns_backend": "systemd-resolved" if ok_dns else
                           "NetworkManager" if nmcfg.available() else "none"}


def systemd_state():
    """org.freedesktop.systemd1.Manager.SystemState, or None if unreachable.

    One of: initializing, starting, running, degraded, maintenance, stopping.
    Used as the RUN-LED source: it is the closest thing an x86 edge has to the
    "is the runtime healthy" signal a PFC's RUN LED carries.
    """
    ok, out = _busctl("get-property", "org.freedesktop.systemd1",
                      "/org/freedesktop/systemd1",
                      "org.freedesktop.systemd1.Manager", "SystemState")
    if not ok:
        return None
    m = re.match(r'^s\s+"(.*)"$', out)
    return m.group(1) if m else None
