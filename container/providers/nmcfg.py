#!/usr/bin/env python3
"""DNS through NetworkManager, over the same system D-Bus socket.

Why this exists next to hostcfg.set_link_dns(): the edge has no
systemd-resolved. Probed on the device 2026-09-02 - `org.freedesktop.resolve1
was not provided by any .service file`, resolv.conf is a plain file, and
NetworkManager 1.52.1 owns it. resolved is still the right backend where it
runs (a PFC-style image may have it), so networking.py tries that first and
falls back here.

The sequence is what nmcli does, and nothing more:

  GetDeviceByIpIface(ifname) -> device
  device.ActiveConnection.Connection -> the settings object
  Connection.GetSettings() -> the whole profile
  set ipv4.dns / ipv6.dns + ignore-auto-dns, keep every other key
  Connection.Update2(settings, TO_DISK, {})   persist
  Device.Reapply(settings, 0, 0)              make it live

Reapply rather than re-activating the connection: a re-activation bounces the
link, and the link is the one the request arrived on. Reapply changes DNS on a
running connection without dropping it. If Reapply fails the profile is still
persisted, and the caller reports that honestly rather than claiming it is live.

dbus-python rather than busctl here: Update2 takes a{sa{sv}} and the settings
dict has to be round-tripped intact - one dropped key is a connection profile
missing its address. That is not something to hand-assemble on a command line.
"""
import ipaddress
import socket
import struct

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
SETTINGS_CONN = "org.freedesktop.NetworkManager.Settings.Connection"
DEVICE = "org.freedesktop.NetworkManager.Device"
NM_SETTINGS_UPDATE2_TO_DISK = 0x1


class NMError(Exception):
    pass


def _bus():
    try:
        import dbus                       # python3-dbus, from the base image
    except ImportError as e:              # pragma: no cover - packaging error
        raise NMError(f"python3-dbus missing: {e}")
    try:
        return dbus, dbus.SystemBus()
    except Exception as e:
        raise NMError(f"cannot reach the system bus: {e}")


def available():
    try:
        dbus, bus = _bus()
        bus.get_object(NM, NM_PATH).Get(NM, "Version",
                                        dbus_interface="org.freedesktop.DBus.Properties")
        return True
    except Exception:
        return False


def _encode(dbus, servers):
    """(ipv4 as au network-byte-order uint32, ipv6 as aay) - NM's own wire format."""
    v4, v6 = [], []
    for s in servers:
        ip = ipaddress.ip_address(s)
        if ip.version == 4:
            v4.append(dbus.UInt32(struct.unpack("=I", socket.inet_aton(str(ip)))[0]))
        else:
            v6.append(dbus.Array([dbus.Byte(b) for b in ip.packed], signature="y"))
    return v4, v6


def _connection(dbus, bus, ifname):
    """(device object, Settings.Connection interface, settings dict)."""
    nm = bus.get_object(NM, NM_PATH)
    dev_path = nm.GetDeviceByIpIface(ifname, dbus_interface=NM)
    dev = bus.get_object(NM, dev_path)
    props = dbus.Interface(dev, "org.freedesktop.DBus.Properties")
    ac_path = props.Get(DEVICE, "ActiveConnection")
    if ac_path == "/":
        raise NMError(f"{ifname} has no active connection")
    ac = dbus.Interface(bus.get_object(NM, ac_path), "org.freedesktop.DBus.Properties")
    conn_path = ac.Get("org.freedesktop.NetworkManager.Connection.Active", "Connection")
    conn = dbus.Interface(bus.get_object(NM, conn_path), SETTINGS_CONN)
    return dev, conn, conn.GetSettings()


def _commit(dbus, dev, conn, settings, what):
    try:
        conn.Update2(settings, NM_SETTINGS_UPDATE2_TO_DISK,
                     dbus.Dictionary({}, signature="sv"))
    except Exception as e:
        raise NMError(f"NetworkManager rejected the {what} update: {e}")
    try:
        dbus.Interface(dev, DEVICE).Reapply(settings, 0, 0)
    except Exception as e:
        return False, f"saved to the profile, not yet live: {e}"
    return True, ""


def searches(ifname):
    """The search domains actually in effect on a link, or [].

    Not socket.getfqdn() and not our own /etc/resolv.conf: Docker gives the
    container its own copy of resolv.conf, generated at start, so the container
    view never reflects a change made on the host - it would report the old
    search domain exactly when it is read back after a write.
    """
    try:
        dbus, bus = _bus()
        nm = bus.get_object(NM, NM_PATH)
        dev_path = nm.GetDeviceByIpIface(ifname, dbus_interface=NM)
        props = dbus.Interface(bus.get_object(NM, dev_path),
                               "org.freedesktop.DBus.Properties")
        out = []
        for family, iface in (("Ip4Config", "org.freedesktop.NetworkManager.IP4Config"),
                              ("Ip6Config", "org.freedesktop.NetworkManager.IP6Config")):
            path = props.Get(DEVICE, family)
            if path == "/":
                continue
            cfg = dbus.Interface(bus.get_object(NM, path),
                                 "org.freedesktop.DBus.Properties")
            for s in cfg.Get(iface, "Searches"):
                if str(s) not in out:
                    out.append(str(s))
        return out
    except Exception:
        return []


def set_search_domain(ifname, domain):
    """WDA's Networking/Domain/CustomDomain is the resolver search domain, which
    on an NM-managed device is ipv4.dns-search / ipv6.dns-search on the profile.
    An empty domain clears it. (applied_live, detail)."""
    dbus, bus = _bus()
    try:
        dev, conn, settings = _connection(dbus, bus, ifname)
    except NMError:
        raise
    except Exception as e:
        raise NMError(f"NetworkManager refused to describe {ifname}: {e}")
    values = [domain] if domain else []
    for family in ("ipv4", "ipv6"):
        section = settings.setdefault(family, dbus.Dictionary({}, signature="sv"))
        section["dns-search"] = dbus.Array(values, signature="s")
    return _commit(dbus, dev, conn, settings, "search domain")


def set_dns(ifname, servers):
    """Set DNS on the profile behind `ifname`. (applied_live, detail).

    applied_live False means: persisted to the profile, not yet in effect - the
    caller must not claim otherwise.
    """
    dbus, bus = _bus()
    try:
        nm = bus.get_object(NM, NM_PATH)
        dev_path = nm.GetDeviceByIpIface(ifname, dbus_interface=NM)
        dev = bus.get_object(NM, dev_path)
        props = dbus.Interface(dev, "org.freedesktop.DBus.Properties")
        ac_path = props.Get(DEVICE, "ActiveConnection")
        if ac_path == "/":
            raise NMError(f"{ifname} has no active connection")
        ac = dbus.Interface(bus.get_object(NM, ac_path), "org.freedesktop.DBus.Properties")
        conn_path = ac.Get("org.freedesktop.NetworkManager.Connection.Active", "Connection")
        conn = dbus.Interface(bus.get_object(NM, conn_path), SETTINGS_CONN)
        settings = conn.GetSettings()
    except NMError:
        raise
    except Exception as e:
        raise NMError(f"NetworkManager refused to describe {ifname}: {e}")

    v4, v6 = _encode(dbus, servers)
    # Mutate only the DNS keys. Everything else in the profile - addresses,
    # method, routes - is written back exactly as it was read.
    for family, values in (("ipv4", v4), ("ipv6", v6)):
        section = settings.setdefault(family, dbus.Dictionary({}, signature="sv"))
        section["dns"] = dbus.Array(values, signature="u" if family == "ipv4" else "ay")
        # Without this, DHCP-supplied servers are appended to ours.
        section["ignore-auto-dns"] = dbus.Boolean(bool(values))

    try:
        conn.Update2(settings, NM_SETTINGS_UPDATE2_TO_DISK, dbus.Dictionary({}, signature="sv"))
    except Exception as e:
        raise NMError(f"NetworkManager rejected the profile update: {e}")
    try:
        dbus.Interface(dev, DEVICE).Reapply(settings, 0, 0)
    except Exception as e:
        return False, f"saved to the profile, not yet live: {e}"
    return True, ""
