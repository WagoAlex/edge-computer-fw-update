#!/usr/bin/env python3
"""0-0-networking-* : read-only projection of the live network state.

Backends are the kernel's own views - /sys/class/net, /proc/net/route,
/etc/resolv.conf - so there is no NetworkManager-vs-networkd variance and no
subprocess per parameter. Everything here is `current*`/read-only; the writable
`custom*`/`static*` twins are Phase 3.

CAVEAT (deploy shape): a container has its own network namespace, so X1/X2 are
only visible with `network_mode: host`. Without it these read as absent
(enabled=false, empty addresses) rather than lying - see docker-compose.server.yml.

Port naming needs no mapping table: the edge's own
/etc/udev/rules.d/20-network-names.rules already renames every NIC to its WAGO
name (X1, X2, and X11/X12 on the expansion models), so the kernel interface name
IS the WAGO port name. Ports are discovered by that name and the WDA instance id
is the number in it - X11 is `ethernetports-11`, not "the third one found".
Addon-card ports (LAN_A/LAN_B in the udev rules) have no number, so they get no
instance until WAGO's numbering for them is known - they are not renumbered here.
"""
import os
import re
import socket
import struct

from . import NOTFOUND, cached

SYS = os.environ.get("SYSFS_NET", "/sys/class/net")
PROC_ROUTE = os.environ.get("PROC_NET_ROUTE", "/proc/net/route")
RESOLV_CONF = os.environ.get("RESOLV_CONF", "/etc/resolv.conf")
# Escape hatch for a box whose udev rules did not run (or a non-WAGO test host):
# PORT_MAP="X1=enp1s0,X2=enp2s0" forces the mapping. Empty = discover.
PORT_MAP = os.environ.get("PORT_MAP", "")

_XNAME = re.compile(r"^X(\d+)$")

# speed+duplex -> WDA SpeedDuplex enum member. Derived from the FW31 cassette
# (X1 link-up reads 4, X2 link-down reads 0); the full table is not published, so
# we do NOT serve an enum definition for it - only the member value.
SPEED_DUPLEX = {(10, "half"): 1, (10, "full"): 2,
                (100, "half"): 3, (100, "full"): 4,
                (1000, "full"): 5, (2500, "full"): 6, (10000, "full"): 7}


def _read(*parts):
    try:
        with open(os.path.join(*parts)) as f:
            return f.read().strip()
    except OSError:
        return None


def _ifaces():
    try:
        return set(os.listdir(SYS))
    except OSError:
        return set()


def ports():
    """{instance id: (WAGO name, Linux ifname)}. The udev rules make these the
    same string; PORT_MAP overrides for a host where they are not."""
    if PORT_MAP:
        out = {}
        for kv in PORT_MAP.split(","):
            name, _, ifname = kv.partition("=")
            m = _XNAME.match(name.strip())
            if m:
                out[int(m.group(1))] = (name.strip(), ifname.strip() or name.strip())
        return out
    return {int(m.group(1)): (name, name)
            for name in _ifaces() if (m := _XNAME.match(name))}


# Docker's own bridges (docker0, br-<netid>) are not WAGO bridges and must not
# occupy instance ids - on an edge running containers they outnumber the real ones.
DOCKER_IFACE = re.compile(r"^(docker\d*|br-[0-9a-f]{12}|veth[0-9a-f]+)$")


def bridges():
    """{instance id: ifname} for every WAGO bridge, sorted so ids are stable."""
    found = sorted(n for n in _ifaces()
                   if os.path.isdir(os.path.join(SYS, n, "bridge"))
                   and not DOCKER_IFACE.match(n))
    return {i + 1: n for i, n in enumerate(found)}


# ---- ethernet ports -------------------------------------------------------

def _port(idx, attr):
    name, ifname = ports().get(idx, (f"X{idx}", None))
    if attr == "name":
        return name
    if ifname is None or ifname not in _ifaces():
        return {"enabled": False, "haslink": False, "macaddress": "",
                "currentspeedduplex": 0}.get(attr)
    if attr == "enabled":
        return _read(SYS, ifname, "operstate") not in ("down", None)
    if attr == "haslink":
        return _read(SYS, ifname, "carrier") == "1"
    if attr == "macaddress":
        return (_read(SYS, ifname, "address") or "").upper()
    if attr == "currentspeedduplex":
        if _read(SYS, ifname, "carrier") != "1":
            return 0
        try:
            speed = int(_read(SYS, ifname, "speed") or -1)
        except ValueError:
            return 0
        return SPEED_DUPLEX.get((speed, _read(SYS, ifname, "duplex")), 0)
    return None


# ---- bridges --------------------------------------------------------------

def _bridge(idx, attr):
    ifname = bridges().get(idx)
    present = ifname in _ifaces() and os.path.isdir(os.path.join(SYS, ifname, "bridge"))
    if attr == "label":
        return f"Bridge {idx}"
    if attr == "name":
        return ifname or ""
    if attr == "macaddress":
        return (_read(SYS, ifname, "address") or "").upper() if present else ""
    if attr == "connectedethernetports":
        if not present:
            return []
        try:
            members = set(os.listdir(os.path.join(SYS, ifname, "brif")))
        except OSError:
            return []
        # sorted: the cassette lists members in instance order, and an
        # unsorted listdir would make the value flap between reads.
        return sorted(i for i, (_n, dev) in ports().items() if dev in members)
    if attr == "ipconfiguration-currentaddresses":
        return _addresses(ifname) if present else []
    if attr == "ipconfiguration-currentdefaultgateway":
        if not present:
            return ""
        for r in _routes():
            if r["address"] == "0.0.0.0/0" and r["interface"] == ifname:
                return r["gatewayaddress"]
        return ""
    return None


def _addresses(ifname):
    """IPv4 CIDRs on an interface, read without iproute2 via a socket ioctl-free
    path: /proc/net/fib_trie is fragile, so shell out to `ip` only if present."""
    import subprocess
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show", "dev", ifname],
                             capture_output=True, text=True).stdout
    except OSError:
        return []
    return re.findall(r"\binet\s+(\S+/\d+)", out)


# ---- routing --------------------------------------------------------------

def _hex_ip(h):
    return socket.inet_ntoa(struct.pack("<L", int(h, 16)))


def _mask_bits(h):
    return bin(int(h, 16)).count("1")


@cached(5)
def _routes():
    """IPv4 routes from /proc/net/route, in WDA CurrentRoute shape."""
    out = []
    try:
        lines = open(PROC_ROUTE).read().splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        f = line.split()
        if len(f) < 11 or DOCKER_IFACE.match(f[0]):
            # Container plumbing is not device routing: docker0/br-<id>/veth
            # routes would swamp the list and shift every instance id whenever a
            # container starts or stops.
            continue
        out.append({"address": f"{_hex_ip(f[1])}/{_mask_bits(f[7])}",
                    "gatewayaddress": _hex_ip(f[2]),
                    "gatewaymetric": int(f[6]),
                    "source": 0,
                    "interface": f[0]})
    return out


# ---- hostname / domain / dns ----------------------------------------------

def _hostname():
    return socket.gethostname().split(".", 1)[0]


def _domain():
    fqdn = socket.getfqdn()
    return fqdn.split(".", 1)[1] if "." in fqdn else ""


@cached(30)
def _dns():
    try:
        text = open(RESOLV_CONF).read()
    except OSError:
        return []
    return re.findall(r"^\s*nameserver\s+(\S+)", text, re.M)


# ---- registry -------------------------------------------------------------
# Ports and bridges are resolved, not enumerated at import: an expansion module
# adds X11/X12 and a bridge can be created at runtime, and neither should need a
# container restart to show up.

PORT_ATTRS = ("name", "enabled", "haslink", "macaddress", "currentspeedduplex")
BRIDGE_ATTRS = ("label", "name", "macaddress", "connectedethernetports",
                "ipconfiguration-currentaddresses",
                "ipconfiguration-currentdefaultgateway")

PARAMS = {
    "0-0-networking-hostname-currentname": _hostname,
    "0-0-networking-domain-currentdomain": _domain,
    "0-0-networking-dns-utilizeddnsservers": _dns,
    "0-0-networking-routing-currentroutes":
        lambda: [{"Classes": ["CurrentRoute"], "Id": i + 1} for i in range(len(_routes()))],
    "0-0-networking-ethernetports":
        lambda: [{"Classes": ["EthernetPort"], "Id": i} for i in sorted(ports())],
    "0-0-networking-bridges":
        lambda: [{"Classes": ["Bridge"], "Id": i} for i in sorted(bridges())],
}

_INSTANCE_RE = re.compile(r"^0-0-networking-(ethernetports|bridges|routing-currentroutes)"
                          r"-(\d+)-(.+)$")


def RESOLVE(pid):
    m = _INSTANCE_RE.match(pid)
    if not m:
        return NOTFOUND
    kind, idx, attr = m.group(1), int(m.group(2)), m.group(3)
    if kind == "ethernetports":
        return _port(idx, attr) if idx in ports() and attr in PORT_ATTRS else NOTFOUND
    if kind == "bridges":
        return _bridge(idx, attr) if idx in bridges() and attr in BRIDGE_ATTRS else NOTFOUND
    routes = _routes()
    if not 1 <= idx <= len(routes) or attr not in routes[0]:
        return NOTFOUND
    return routes[idx - 1][attr]
