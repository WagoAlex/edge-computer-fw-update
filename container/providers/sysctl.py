#!/usr/bin/env python3
"""IP forwarding: read from /proc, write through a sysctl drop-in.

Reading is free - /proc/sys is mounted read-only in every container.

Writing is not, and this is the one parameter in the writable set that needs a
grant the container does not have by default:

  * /proc/sys is read-only, so `echo 1 > .../ip_forward` cannot work,
  * running `sysctl -w` on the host would mean systemd1.StartTransientUnit,
    i.e. arbitrary root exec from a container - the container-escape shape this
    project has refused from the start.

So: write a drop-in under /etc/sysctl.d and ask systemd to re-apply it, which is
one named unit and one directory rather than a general execution channel.

Both halves are OFF unless the operator mounts the directory read-write:

    volumes:
      - /etc/sysctl.d:/etc/sysctl.d

Without that mount a write returns 503 naming the mount, and nothing about the
container's privileges changes. Opt-in, and visibly so in the compose file.
"""
import os
import subprocess

PROC_IP_FORWARD = os.environ.get("PROC_IP_FORWARD", "/proc/sys/net/ipv4/ip_forward")
SYSCTL_D = os.environ.get("SYSCTL_D", "/etc/sysctl.d")
DROPIN = os.path.join(SYSCTL_D, "99-wda-ipforwarding.conf")
SYSCTL_UNIT = "systemd-sysctl.service"
TIMEOUT = int(os.environ.get("DBUS_TIMEOUT", "10"))


def ip_forwarding():
    """Live kernel state, not what our drop-in asked for."""
    try:
        with open(PROC_IP_FORWARD) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def writable():
    return os.access(SYSCTL_D, os.W_OK)


def set_ip_forwarding(enabled):
    """(ok, detail). Writes the drop-in, then re-applies it via systemd."""
    if not writable():
        return False, (f"{SYSCTL_D} is not writable in this container - mount it "
                       f"read-write to allow forwarding changes")
    try:
        tmp = DROPIN + ".tmp"
        with open(tmp, "w") as f:
            f.write("# Managed by the WAGO edge WDA API "
                    "(0-0-networking-routing-ipforwarding-enabled).\n"
                    f"net.ipv4.ip_forward = {1 if enabled else 0}\n"
                    f"net.ipv6.conf.all.forwarding = {1 if enabled else 0}\n")
        os.replace(tmp, DROPIN)
    except OSError as e:
        return False, f"could not write {DROPIN}: {e}"
    r = subprocess.run(["busctl", "call", "org.freedesktop.systemd1",
                        "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                        "RestartUnit", "ss", SYSCTL_UNIT, "replace"],
                       capture_output=True, text=True, timeout=TIMEOUT)
    if r.returncode != 0:
        return False, ("drop-in written but systemd refused to re-apply it: "
                       + (r.stderr or r.stdout).strip())
    if ip_forwarding() != bool(enabled):
        return False, "systemd re-applied sysctls but the kernel value did not change"
    return True, ""
