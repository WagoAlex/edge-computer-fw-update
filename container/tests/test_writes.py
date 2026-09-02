"""Phase 3 slice 1: writable hostname and DNS.

The D-Bus backend is mocked at the subprocess edge - the same seam the rauc
tests use - so these run anywhere, and the assertions are about what we would
have asked systemd to do, not about having systemd.
"""
import json
import os

import pytest

import providers
from providers import WriteError, hostcfg, networking


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "network-custom.json"
    monkeypatch.setattr(networking, "CUSTOM_STORE", str(path))
    return path


@pytest.fixture
def bus(monkeypatch):
    """Record every busctl invocation instead of making one."""
    calls = []

    def fake(*args):
        calls.append(list(args))
        return True, ""
    monkeypatch.setattr(hostcfg, "_busctl", fake)
    return calls


@pytest.fixture
def link(monkeypatch):
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    # resolved present by default; the NetworkManager path has its own tests
    monkeypatch.setattr(hostcfg, "resolved_available", lambda: True)


# ---- validation: nothing reaches the bus -----------------------------------

@pytest.mark.parametrize("bad", ["-nope", "nope-", "a" * 64, "has space",
                                 "under_score", "dot.separated", 42, None, ["x"]])
def test_bad_hostname_rejected_without_touching_the_system(bad, store, bus):
    with pytest.raises(WriteError) as e:
        networking.w_hostname(bad)
    assert e.value.status == 400
    assert bus == [], "a rejected value must not reach the backend"
    assert not os.path.exists(store), "a rejected value must not be persisted"


@pytest.mark.parametrize("good", ["edge", "edge-lab-01", "a", "A1", "x" * 63])
def test_good_hostname_applied_and_stored(good, store, bus):
    assert networking.w_hostname(good) == good
    assert bus[0][:2] == ["call", "org.freedesktop.hostname1"]
    assert bus[0][-3:] == ["sb", good, "false"]
    assert json.load(open(store))["0-0-networking-hostname-customname"] == good


def test_empty_hostname_is_no_override_not_an_error(store, bus):
    assert networking.w_hostname("") == ""
    assert bus == [], "clearing the override must not rename the running host"
    assert json.load(open(store))["0-0-networking-hostname-customname"] == ""


@pytest.mark.parametrize("bad", ["1.2.3", "not-an-ip", "192.168.2.1/24", 7, None])
def test_bad_dns_entry_rejected(bad, store, bus, link):
    with pytest.raises(WriteError) as e:
        networking.w_dns([bad])
    assert e.value.status == 400
    assert bus == []


def test_dns_rejects_duplicates_and_overlong_lists(store, bus, link):
    with pytest.raises(WriteError):
        networking.w_dns(["9.9.9.9", "9.9.9.9"])
    with pytest.raises(WriteError):
        networking.w_dns([f"10.0.0.{i}" for i in range(networking.MAX_DNS_SERVERS + 1)])
    assert bus == []


def test_dns_rejects_a_scalar_where_an_array_belongs(store, bus, link):
    with pytest.raises(WriteError) as e:
        networking.w_dns("9.9.9.9")
    assert e.value.status == 400


# ---- the call we actually make ---------------------------------------------

def test_dns_encodes_v4_and_v6_the_way_setlinkdns_wants(store, bus, link):
    networking.w_dns(["9.9.9.9", "2620:fe::fe"])
    call = bus[0]
    assert call[:2] == ["call", "org.freedesktop.resolve1"]
    assert "SetLinkDNS" in call and "ia(iay)" in call
    args = call[call.index("ia(iay)") + 1:]
    # ifindex, count, then (family, length, bytes...) per server
    assert args[0] == "2" and args[1] == "2"
    assert args[2] == "2" and args[3] == "4"          # AF_INET, 4 bytes
    assert args[4:8] == ["9", "9", "9", "9"]
    assert args[8] == "10" and args[9] == "16"        # AF_INET6, 16 bytes
    assert json.load(open(store))["0-0-networking-dns-customdnsservers"] == \
        ["9.9.9.9", "2620:fe::fe"]


def test_backend_refusal_is_reported_and_not_persisted(store, monkeypatch, link):
    monkeypatch.setattr(hostcfg, "_busctl",
                        lambda *a: (False, "Interactive authentication required."))
    with pytest.raises(WriteError) as e:
        networking.w_hostname("edge-lab-01")
    assert e.value.status == 503
    assert "Interactive authentication required" in e.value.detail
    assert not os.path.exists(store), "a refused write must not look applied"


def test_no_carrier_means_no_dns_write(store, bus, monkeypatch):
    monkeypatch.setattr(networking, "ports", lambda: {})
    monkeypatch.setattr(networking, "_routes", lambda: [])
    with pytest.raises(WriteError) as e:
        networking.w_dns(["9.9.9.9"])
    assert e.value.status == 503
    assert bus == []


# ---- resolver backend selection ---------------------------------------------
# The edge has no systemd-resolved (probed 2026-09-02); NetworkManager owns DNS
# there. Both paths have to work, and the absence of both has to be an error the
# operator can read - never a silent no-op.

def test_networkmanager_is_used_when_resolved_is_absent(store, monkeypatch):
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    monkeypatch.setattr(hostcfg, "resolved_available", lambda: False)
    monkeypatch.setattr(hostcfg, "set_link_dns",
                        lambda *a: pytest.fail("resolved must not be called"))
    seen = {}
    monkeypatch.setattr(networking.nmcfg, "available", lambda: True)
    monkeypatch.setattr(networking.nmcfg, "set_dns",
                        lambda dev, servers: (seen.update(dev=dev, servers=servers), (True, ""))[1])
    networking.w_dns(["9.9.9.9"])
    assert seen == {"dev": "X1", "servers": ["9.9.9.9"]}
    assert json.load(open(store))["0-0-networking-dns-customdnsservers"] == ["9.9.9.9"]


def test_no_resolver_backend_is_an_error_not_a_no_op(store, monkeypatch):
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    monkeypatch.setattr(hostcfg, "resolved_available", lambda: False)
    monkeypatch.setattr(networking.nmcfg, "available", lambda: False)
    with pytest.raises(WriteError) as e:
        networking.w_dns(["9.9.9.9"])
    assert e.value.status == 503
    assert "neither" in e.value.detail
    assert not os.path.exists(store)


def test_networkmanager_failure_is_reported(store, monkeypatch):
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    monkeypatch.setattr(hostcfg, "resolved_available", lambda: False)
    monkeypatch.setattr(networking.nmcfg, "available", lambda: True)

    def boom(dev, servers):
        raise networking.nmcfg.NMError("Not authorized")
    monkeypatch.setattr(networking.nmcfg, "set_dns", boom)
    with pytest.raises(WriteError) as e:
        networking.w_dns(["9.9.9.9"])
    assert e.value.status == 503 and "Not authorized" in e.value.detail
    assert not os.path.exists(store)


def test_nm_encodes_v4_as_u32_and_v6_as_bytes():
    """NM's settings dict wants ipv4.dns as network-byte-order uint32 and
    ipv6.dns as byte arrays. Getting this wrong writes a plausible-looking
    profile that resolves nothing."""
    from providers import nmcfg

    class FakeDbus:                       # only what _encode touches
        UInt32 = staticmethod(lambda v: ("u32", v))
        Byte = staticmethod(lambda v: v)
        Array = staticmethod(lambda v, signature=None: ("arr", list(v)))
    v4, v6 = nmcfg._encode(FakeDbus, ["9.9.9.9", "2620:fe::fe"])
    assert v4 == [("u32", 0x09090909)]
    import ipaddress
    assert v6 == [("arr", list(ipaddress.ip_address("2620:fe::fe").packed))]
    assert len(v6[0][1]) == 16


# ---- domain + ip forwarding --------------------------------------------------

@pytest.mark.parametrize("bad", ["-bad.lan", "bad-.lan", "a..b", "x" * 300,
                                 "space domain", 5, None, ["lan"]])
def test_bad_domain_rejected(bad, store, monkeypatch):
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    monkeypatch.setattr(networking.nmcfg, "available", lambda: True)
    monkeypatch.setattr(networking.nmcfg, "set_search_domain",
                        lambda *a: pytest.fail("must not reach NetworkManager"))
    with pytest.raises(WriteError) as e:
        networking.w_domain(bad)
    assert e.value.status == 400
    assert not os.path.exists(store)


def test_domain_goes_to_the_profile_behind_the_link(store, monkeypatch):
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    monkeypatch.setattr(networking.nmcfg, "available", lambda: True)
    seen = {}
    monkeypatch.setattr(networking.nmcfg, "set_search_domain",
                        lambda dev, dom: (seen.update(dev=dev, dom=dom), (True, ""))[1])
    # the value a real PFC300 carries, pushed there by the Device Sphere twin
    assert networking.w_domain("localdomain.lan") == "localdomain.lan"
    assert seen == {"dev": "X1", "dom": "localdomain.lan"}
    assert json.load(open(store))["0-0-networking-domain-customdomain"] == "localdomain.lan"


def test_clearing_the_domain_actually_clears_it_on_the_device(store, monkeypatch):
    """"" means no override, and for a search domain that is a real state: the
    override has to come off the profile so DHCP supplies one again. Found on
    the device - storing "" while NetworkManager kept localdomain.lan left the
    parameter and the system disagreeing."""
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    monkeypatch.setattr(networking.nmcfg, "available", lambda: True)
    seen = []
    monkeypatch.setattr(networking.nmcfg, "set_search_domain",
                        lambda dev, dom: (seen.append(dom), (True, ""))[1])
    networking.w_domain("localdomain.lan")
    networking.w_domain("")
    assert seen == ["localdomain.lan", ""], "the clear must reach NetworkManager"
    assert json.load(open(store))["0-0-networking-domain-customdomain"] == ""


def test_clearing_a_domain_that_was_never_set_touches_nothing(store, monkeypatch):
    monkeypatch.setattr(networking.nmcfg, "set_search_domain",
                        lambda *a: pytest.fail("nothing to clear, nothing to call"))
    assert networking.w_domain("") == ""


def test_clearing_dns_hands_it_back_to_dhcp(store, monkeypatch):
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    monkeypatch.setattr(hostcfg, "resolved_available", lambda: False)
    monkeypatch.setattr(networking.nmcfg, "available", lambda: True)
    seen = []
    monkeypatch.setattr(networking.nmcfg, "set_dns",
                        lambda dev, servers: (seen.append(list(servers)), (True, ""))[1])
    networking.w_dns(["9.9.9.9"])
    networking.w_dns([])
    assert seen == [["9.9.9.9"], []]


def test_ipforwarding_needs_a_boolean(store):
    for bad in ("true", 1, None, []):
        with pytest.raises(WriteError) as e:
            networking.w_ipforwarding(bad)
        assert e.value.status == 400


def test_ipforwarding_without_the_mount_says_so_and_changes_nothing(store, tmp_path,
                                                                    monkeypatch):
    """The mount is opt-in. Absent it, the write must fail loudly - never a
    silent no-op that reports success while the kernel is untouched."""
    from providers import sysctl
    monkeypatch.setattr(sysctl, "SYSCTL_D", str(tmp_path / "nope"))
    with pytest.raises(WriteError) as e:
        networking.w_ipforwarding(True)
    assert e.value.status == 503
    assert "mount it read-write" in e.value.detail


def test_ipforwarding_writes_the_dropin_then_reloads(tmp_path, monkeypatch):
    from providers import sysctl
    monkeypatch.setattr(sysctl, "SYSCTL_D", str(tmp_path))
    monkeypatch.setattr(sysctl, "DROPIN", str(tmp_path / "99-wda-ipforwarding.conf"))
    monkeypatch.setattr(sysctl, "ip_forwarding", lambda: True)
    calls = []

    class R:
        returncode = 0
        stdout = stderr = ""
    monkeypatch.setattr(sysctl.subprocess, "run", lambda *a, **k: (calls.append(a[0]), R)[1])
    ok, detail = sysctl.set_ip_forwarding(True)
    assert ok, detail
    body = (tmp_path / "99-wda-ipforwarding.conf").read_text()
    assert "net.ipv4.ip_forward = 1" in body
    assert "net.ipv6.conf.all.forwarding = 1" in body
    assert calls[0][:4] == ["busctl", "call", "org.freedesktop.systemd1",
                            "/org/freedesktop/systemd1"]
    assert "systemd-sysctl.service" in calls[0]


def test_ipforwarding_reports_a_kernel_that_did_not_follow(tmp_path, monkeypatch):
    from providers import sysctl
    monkeypatch.setattr(sysctl, "SYSCTL_D", str(tmp_path))
    monkeypatch.setattr(sysctl, "DROPIN", str(tmp_path / "f.conf"))
    monkeypatch.setattr(sysctl, "ip_forwarding", lambda: False)   # unchanged

    class R:
        returncode = 0
        stdout = stderr = ""
    monkeypatch.setattr(sysctl.subprocess, "run", lambda *a, **k: R)
    ok, detail = sysctl.set_ip_forwarding(True)
    assert not ok and "did not change" in detail


def test_currentdomain_asks_networkmanager_not_the_container_resolv_conf(monkeypatch):
    """Docker hands the container its own resolv.conf copy at start, so reading
    it back would report the pre-write search domain - the same class of bug as
    the frozen UTS hostname. Caught on the device 2026-09-02."""
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    monkeypatch.setattr(networking.nmcfg, "searches", lambda dev: ["localdomain.lan"])
    monkeypatch.setattr(networking.socket, "getfqdn", lambda: "edge.stale.example")
    assert networking._domain() == "localdomain.lan"


def test_currentdomain_falls_back_when_networkmanager_says_nothing(monkeypatch):
    monkeypatch.setattr(networking, "_dns_link", lambda: ("X1", 2))
    monkeypatch.setattr(networking.nmcfg, "searches", lambda dev: [])
    monkeypatch.setattr(networking.socket, "getfqdn", lambda: "edge.fallback.lan")
    assert networking._domain() == "fallback.lan"


# ---- registry and semantics -------------------------------------------------

def test_only_the_intended_ids_are_writable():
    assert sorted(providers.WRITES) == [
        "0-0-networking-dns-customdnsservers",
        "0-0-networking-domain-customdomain",
        "0-0-networking-hostname-customname",
        "0-0-networking-routing-ipforwarding-enabled"]


def test_no_ip_carrying_parameter_is_writable():
    """Standing rule: this API never sets an address. Guard it in code."""
    forbidden = ("address", "gateway", "routes", "ipconfiguration")
    assert not [p for p in providers.WRITES if any(w in p for w in forbidden)]


def test_writable_ids_are_readable_too(store):
    for pid in providers.WRITES:
        assert providers.param_value(pid) is not None, f"{pid} must read back"


def test_custom_defaults_before_anything_is_written(store):
    assert providers.param_value("0-0-networking-hostname-customname") == ""
    assert providers.param_value("0-0-networking-domain-customdomain") == ""
    assert providers.param_value("0-0-networking-dns-customdnsservers") == []
    assert isinstance(
        providers.param_value("0-0-networking-routing-ipforwarding-enabled"), bool)


def test_custom_does_not_fabricate_current(store, bus):
    """The CC100 has customname "" while currentname is CC100-592E6C. Writing
    the custom value must not make current* claim the new name by itself."""
    networking.w_hostname("edge-lab-01")
    assert providers.param_value("0-0-networking-hostname-customname") == "edge-lab-01"
    current = providers.param_value("0-0-networking-hostname-currentname")
    assert isinstance(current, str) and current != ""   # comes from the live system


def test_reapply_pushes_stored_values_back(store, bus, link):
    networking.w_hostname("edge-lab-01")
    networking.w_dns(["9.9.9.9"])
    bus.clear()
    networking.reapply()
    kinds = [c[1] for c in bus]
    assert "org.freedesktop.hostname1" in kinds
    assert "org.freedesktop.resolve1" in kinds


def test_set_param_rejects_a_read_only_id():
    with pytest.raises(KeyError):
        providers.set_param("0-0-networking-hostname-currentname", "nope")
