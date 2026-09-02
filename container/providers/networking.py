#!/usr/bin/env python3
"""0-0-networking-* : live network state, plus the writable hostname/DNS twins.

Reads are the kernel's own views - /sys/class/net, /proc/net/route,
/etc/resolv.conf - so there is no NetworkManager-vs-networkd variance and no
subprocess per parameter.

Writes (Phase 3) cover four ids, none of which can cut the connection they
arrive on:

    0-0-networking-hostname-customname   -> hostname1.SetStaticHostname
    0-0-networking-domain-customdomain   -> NM ipv4/ipv6.dns-search on the profile
    0-0-networking-dns-customdnsservers  -> resolve1.SetLinkDNS, else NM ipv4/ipv6.dns
    0-0-networking-routing-ipforwarding-enabled -> sysctl drop-in (see sysctl.py)

The first three go through the system D-Bus socket the container already has, so
nothing is granted. Forwarding is the exception and is opt-in: it needs
/etc/sysctl.d mounted read-write, and without that mount it fails loudly.

`custom*` is the operator's override and is stored here;
`current*`/`utilized*` keep reading the live system and only change once the
system agrees - on a real CC100 `customname` is "" while `currentname` is
`CC100-592E6C`, and this build must not fabricate that relationship either way.

NOT writable here, deliberately: anything carrying an IP address (bridge
addresses, gateways, static routes). That is the one class of change that can
strand the device, and it is out of scope by standing rule.

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
import ipaddress
import json
import os
import re
import socket
import struct

import wdalog

from . import NOTFOUND, WriteError, cached
from . import hostcfg, nmcfg, sysctl

SYS = os.environ.get("SYSFS_NET", "/sys/class/net")
PROC_ROUTE = os.environ.get("PROC_NET_ROUTE", "/proc/net/route")
RESOLV_CONF = os.environ.get("RESOLV_CONF", "/etc/resolv.conf")
# Where custom* values live. Same volume as the presets, so they survive a
# redeploy - resolved's SetLinkDNS is runtime-only, so this file is what makes
# a custom value behave like configuration rather than a one-shot command.
CUSTOM_STORE = os.environ.get("NETWORK_CUSTOM_STORE", "/app/data/network-custom.json")
MAX_DNS_SERVERS = 8
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
    # hostnamed first: with network_mode host the container's UTS namespace is
    # seeded from the host at start and then frozen, so socket.gethostname()
    # would keep reporting the old name after a successful write.
    live = hostcfg.hostname()
    if live:
        return live.split(".", 1)[0]
    return socket.gethostname().split(".", 1)[0]


def _domain():
    # NetworkManager first, for the same reason the hostname asks hostnamed:
    # the container's /etc/resolv.conf is Docker's copy from container start and
    # never follows a change made on the host.
    try:
        dev, _idx = _dns_link()
    except WriteError:
        dev = None
    if dev:
        live = nmcfg.searches(dev)
        if live:
            return live[0]
    fqdn = socket.getfqdn()
    return fqdn.split(".", 1)[1] if "." in fqdn else ""


@cached(30)
def _dns():
    try:
        text = open(RESOLV_CONF).read()
    except OSError:
        return []
    return re.findall(r"^\s*nameserver\s+(\S+)", text, re.M)


# ---- custom* : the writable twins -----------------------------------------

_DEFAULTS = {"0-0-networking-hostname-customname": "",
             "0-0-networking-domain-customdomain": "",
             "0-0-networking-dns-customdnsservers": []}
# A domain is dot-separated RFC 1123 labels, 255 chars total - the rule the
# Device Sphere twin states for Custom Domain ("localdomain.lan" on the PFC300).
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                        r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
# RFC 1123 label - exactly the rule the Device Sphere twin states for Custom Name
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _store_read():
    try:
        with open(CUSTOM_STORE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _store_write(pid, value):
    data = _store_read()
    data[pid] = value
    try:
        os.makedirs(os.path.dirname(CUSTOM_STORE), exist_ok=True)
        tmp = CUSTOM_STORE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, CUSTOM_STORE)     # never a half-written store
    except OSError as e:
        raise WriteError(500, f"value applied but not persisted: {e}")


def _custom(pid):
    return _store_read().get(pid, _DEFAULTS[pid])


def _check_hostname(value):
    if not isinstance(value, str):
        raise WriteError(400, "expected a string")
    if value == "":
        return ""                    # "" means no override - allowed, applies nothing
    if len(value) > 63 or not _HOSTNAME_RE.match(value):
        raise WriteError(400, "not a valid hostname: 1-63 chars of A-Z a-z 0-9 and "
                              "hyphen, no leading or trailing hyphen")
    return value


def _check_domain(value):
    if not isinstance(value, str):
        raise WriteError(400, "expected a string")
    if value == "":
        return ""                    # no override
    if len(value) > 255 or not _DOMAIN_RE.match(value):
        raise WriteError(400, "not a valid domain: dot-separated labels of A-Z "
                              "a-z 0-9 and hyphen, 63 chars per label, 255 total")
    return value


def _check_dns(value):
    if not isinstance(value, list):
        raise WriteError(400, "expected an array of IP addresses")
    if len(value) > MAX_DNS_SERVERS:
        raise WriteError(400, f"at most {MAX_DNS_SERVERS} DNS servers")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise WriteError(400, "DNS servers must be strings")
        try:
            ipaddress.ip_address(item)
        except ValueError:
            raise WriteError(400, f"not an IP address: {item}")
        if item in out:
            raise WriteError(400, f"duplicate DNS server: {item}")
        out.append(item)
    return out


def _dns_link():
    """The link resolved gets the servers on: the port carrying the default
    route, else the first port with carrier. Returns (ifname, ifindex)."""
    devs = [dev for _i, (_name, dev) in sorted(ports().items()) if dev]
    default = next((r["interface"] for r in _routes() if r["address"] == "0.0.0.0/0"), None)
    if default:
        # _routes() reports the WAGO name; map it back to the kernel device
        for i, (name, dev) in ports().items():
            if name == default and dev:
                devs.insert(0, dev)
                break
    for dev in devs:
        if _read(SYS, dev, "carrier") == "1":
            idx = hostcfg.ifindex(dev)
            if idx:
                return dev, idx
    raise WriteError(503, "no link with carrier to apply DNS servers to")


def w_hostname(value):
    value = _check_hostname(value)
    if value:
        ok, detail = hostcfg.set_static_hostname(value)
        if not ok:
            raise WriteError(503, f"hostnamed refused the change: {detail}")
    _store_write("0-0-networking-hostname-customname", value)
    return value


def w_dns(value):
    """Apply DNS servers through whichever resolver this device actually runs.

    systemd-resolved first, because SetLinkDNS is the narrower change; the edge
    has no resolved (probed 2026-09-02: no such bus name, resolv.conf is a plain
    file, NetworkManager 1.52.1 owns it), so NetworkManager is the real path
    there. A device with neither gets a 503 that says so, not a silent no-op.
    """
    value = _check_dns(value)
    # As with the domain: an empty list is applied when we had set servers, so
    # clearing the override really hands DNS back to DHCP.
    if value or _custom("0-0-networking-dns-customdnsservers"):
        dev, idx = _dns_link()
        applied = None
        if hostcfg.resolved_available():
            ok, detail = hostcfg.set_link_dns(idx, value)
            if not ok:
                raise WriteError(503, f"systemd-resolved refused the change on {dev}: {detail}")
            applied = "systemd-resolved"
        elif nmcfg.available():
            try:
                live, detail = nmcfg.set_dns(dev, value)
            except nmcfg.NMError as e:
                raise WriteError(503, f"NetworkManager refused the change on {dev}: {e}")
            applied = "NetworkManager" + ("" if live else f" ({detail})")
        else:
            raise WriteError(503, "no resolver backend on this device: neither "
                                  "systemd-resolved nor NetworkManager is on the bus")
        wdalog.write.info("DNS applied on %s via %s", dev, applied)
    _store_write("0-0-networking-dns-customdnsservers", value)
    _dns.cache_clear()                   # utilized* must not serve a stale read
    return value


def w_domain(value):
    """The resolver search domain. resolved has no D-Bus setter for the global
    search domain either, so this is NetworkManager's dns-search on the profile
    behind the link - the same round-trip as the DNS servers."""
    value = _check_domain(value)
    # An empty value is applied, not skipped: for a search domain "no override"
    # is a well-defined state - remove ours and let DHCP supply one. (Hostname
    # is the opposite case: there is no meaningful factory name to revert to, so
    # clearing it stores the empty override and leaves the running name alone.)
    if value or _custom("0-0-networking-domain-customdomain"):
        dev, _idx = _dns_link()
        if not nmcfg.available():
            raise WriteError(503, "NetworkManager is not on the bus; no way to set "
                                  "the search domain on this device")
        try:
            live, detail = nmcfg.set_search_domain(dev, value)
        except nmcfg.NMError as e:
            raise WriteError(503, f"NetworkManager refused the change on {dev}: {e}")
        wdalog.write.info("search domain %s on %s%s",
                          "cleared" if not value else "applied", dev,
                          "" if live else f" ({detail})")
    _store_write("0-0-networking-domain-customdomain", value)
    return value


def w_ipforwarding(value):
    """0-0-networking-routing-ipforwarding-enabled. Unlike the other three this
    one needs a grant the container does not have by default - see
    providers/sysctl.py. Without the mount it fails loudly and changes nothing."""
    if not isinstance(value, bool):
        raise WriteError(400, "expected a boolean")
    ok, detail = sysctl.set_ip_forwarding(value)
    if not ok:
        raise WriteError(503, detail)
    wdalog.write.info("ip forwarding set to %s", value)
    return value


def reapply():
    """Push stored custom values back at start. resolved's SetLinkDNS is runtime
    state, so without this a container restart silently drops the configured
    servers while the parameter still claims them."""
    for pid, fn in (("0-0-networking-hostname-customname", w_hostname),
                    ("0-0-networking-domain-customdomain", w_domain),
                    ("0-0-networking-dns-customdnsservers", w_dns)):
        value = _custom(pid)
        if not value:
            continue
        try:
            fn(value)
            wdalog.write.info("reapplied %s", pid)
        except WriteError as e:
            wdalog.write.warning("could not reapply %s: %s", pid, e.detail)


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
    "0-0-networking-hostname-customname":
        lambda: _custom("0-0-networking-hostname-customname"),
    "0-0-networking-dns-customdnsservers":
        lambda: _custom("0-0-networking-dns-customdnsservers"),
    "0-0-networking-domain-customdomain":
        lambda: _custom("0-0-networking-domain-customdomain"),
    "0-0-networking-routing-ipforwarding-enabled": sysctl.ip_forwarding,
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


WRITES = {"0-0-networking-hostname-customname": w_hostname,
          "0-0-networking-domain-customdomain": w_domain,
          "0-0-networking-dns-customdnsservers": w_dns,
          "0-0-networking-routing-ipforwarding-enabled": w_ipforwarding}
