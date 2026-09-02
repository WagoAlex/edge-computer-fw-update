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


# ---- registry and semantics -------------------------------------------------

def test_only_the_two_intended_ids_are_writable():
    assert sorted(providers.WRITES) == ["0-0-networking-dns-customdnsservers",
                                        "0-0-networking-hostname-customname"]


def test_no_ip_carrying_parameter_is_writable():
    """Standing rule: this API never sets an address. Guard it in code."""
    forbidden = ("address", "gateway", "routes", "ipconfiguration")
    assert not [p for p in providers.WRITES if any(w in p for w in forbidden)]


def test_writable_ids_are_readable_too(store):
    for pid in providers.WRITES:
        assert providers.param_value(pid) is not None, f"{pid} must read back"


def test_custom_defaults_before_anything_is_written(store):
    assert providers.param_value("0-0-networking-hostname-customname") == ""
    assert providers.param_value("0-0-networking-dns-customdnsservers") == []


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
