"""The IP-configuration surface ported off the sibling's :8080 WDA server.

Two things are pinned here.

1. The BRIDGE FALLBACK. WDA puts every address under Bridges/N/IPConfiguration,
   and a stock edge bridges X1/X2 - but the edge as deployed has no WAGO bridge
   at all: NetworkManager manages X1 directly and the only bridge devices are
   Docker's. Without the fallback this API would report no bridges and therefore
   no IP address anywhere, which is the gap the sibling was covering with its own
   ethernetports-N-currentIpaddr.

2. THE WRITER IS NetworkManager. Measured on 192.168.2.17 on 2026-09-02:
   systemd-networkd inactive, NetworkManager active. The sibling's networkd
   drop-in writer would have written a file nothing reads and reported success,
   so it is not ported and there is no second writer to pick between.
"""
import importlib

import pytest

import providers
import providers.networking as net
from providers import WriteError, nmcfg


def sysfs(root, ifaces):
    for name, files in ifaces.items():
        d = root / name
        d.mkdir(parents=True)
        for fname, content in files.items():
            if fname == "brif":
                (d / "bridge").mkdir()
                (d / "brif").mkdir()
                for member in content:
                    (d / "brif" / member).mkdir()
            else:
                (d / fname).write_text(content)
    return str(root)


@pytest.fixture
def edge(tmp_path, monkeypatch):
    """The edge as actually deployed: X1 and X2, no WAGO bridge, Docker's
    bridges present, X1 carrying the address."""
    root = sysfs(tmp_path / "net", {
        "X1": {"operstate": "up", "carrier": "1", "speed": "1000",
               "duplex": "full", "address": "00:30:de:56:4a:41"},
        "X2": {"operstate": "down", "carrier": "0", "speed": "-1",
               "duplex": "unknown", "address": "00:30:de:56:4a:42"},
        "docker0": {"address": "02:42:1a:2b:3c:4d", "brif": []},
        "br-e99ac2610385": {"address": "02:42:aa:bb:cc:dd", "brif": []},
        "lo": {"operstate": "unknown", "address": "00:00:00:00:00:00"},
    })
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "X1\t00000000\t0102A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n")
    monkeypatch.setenv("SYSFS_NET", root)
    monkeypatch.setenv("PROC_NET_ROUTE", str(route))
    monkeypatch.delenv("PORT_MAP", raising=False)
    importlib.reload(net)
    # only X1 has an address, exactly like the real device
    monkeypatch.setattr(net, "_addresses",
                        lambda ifn: ["192.168.2.17/24"] if ifn == "X1" else [])
    return net


@pytest.fixture
def nm(monkeypatch):
    """A recording stand-in for NetworkManager."""
    profile = {"X1": {"addresses": ["192.168.2.17/24"], "gateway": "192.168.2.1"}}
    calls = []

    def static_ipv4(ifn):
        p = profile.get(ifn, {})
        return list(p.get("addresses", [])), p.get("gateway", "")

    def set_static_ipv4(ifn, addresses, gateway):
        calls.append((ifn, list(addresses), gateway))
        profile.setdefault(ifn, {})["addresses"] = list(addresses)
        profile[ifn]["gateway"] = gateway
        return True, ""

    monkeypatch.setattr(net.nmcfg, "available", lambda: True)
    monkeypatch.setattr(net.nmcfg, "static_ipv4", static_ipv4)
    monkeypatch.setattr(net.nmcfg, "set_static_ipv4", set_static_ipv4)
    return calls, profile


# ---- the bridge fallback ---------------------------------------------------

def test_a_device_with_a_real_bridge_still_uses_it(tmp_path, monkeypatch):
    root = sysfs(tmp_path / "net", {
        "X1": {"operstate": "up", "carrier": "1", "address": "00:30:de:56:4a:41"},
        "br0": {"address": "00:30:de:56:4a:40", "brif": ["X1"]},
        "docker0": {"address": "02:42:1a:2b:3c:4d", "brif": []},
    })
    monkeypatch.setenv("SYSFS_NET", root)
    monkeypatch.delenv("PORT_MAP", raising=False)
    importlib.reload(net)
    assert net.bridges() == {1: "br0"}
    assert net.RESOLVE("0-0-networking-bridges-1-connectedethernetports") == [1]


def test_docker_bridges_never_become_wago_bridges(edge):
    assert "docker0" not in edge.bridges().values()
    assert not any(b.startswith("br-") for b in edge.bridges().values())


def test_an_addressed_port_is_its_own_bridge_instance(edge):
    """X1 -> Bridge 1: the port IS the L3 interface, and the instance keeps the
    port's own number rather than being renumbered."""
    assert edge.bridges() == {1: "X1"}
    assert edge.PARAMS["0-0-networking-bridges"]() == [
        {"Classes": ["Bridge"], "Id": 1}]
    assert edge.RESOLVE("0-0-networking-bridges-1-name") == "X1"
    assert edge.RESOLVE("0-0-networking-bridges-1-connectedethernetports") == [1]


def test_the_address_is_reachable_at_all(edge):
    """The whole point of the fallback - without it this answers nothing."""
    assert edge.RESOLVE("0-0-networking-bridges-1-ipconfiguration-currentaddresses") \
        == ["192.168.2.17/24"]
    assert edge.RESOLVE("0-0-networking-bridges-1-ipconfiguration-currentdefaultgateway") \
        == "192.168.2.1"


def test_an_unaddressed_port_is_not_a_bridge(edge):
    assert 2 not in edge.bridges()
    assert edge.RESOLVE("0-0-networking-bridges-2-name") is providers.NOTFOUND


# ---- configured vs live ----------------------------------------------------

def test_addresses_is_the_profile_not_the_lease(edge, nm):
    _calls, profile = nm
    assert edge.RESOLVE("0-0-networking-bridges-1-ipconfiguration-addresses") \
        == ["192.168.2.17/24"]
    profile["X1"]["addresses"] = []                 # switched to DHCP
    assert edge.RESOLVE("0-0-networking-bridges-1-ipconfiguration-addresses") == []
    # the live address is unchanged - a lease is still a lease
    assert edge.RESOLVE("0-0-networking-bridges-1-ipconfiguration-currentaddresses") \
        == ["192.168.2.17/24"]


# ---- writability -----------------------------------------------------------

def test_the_instance_ids_are_writable_and_advertised(edge, nm):
    for pid in ("0-0-networking-bridges-1-ipconfiguration-addresses",
                "0-0-networking-bridges-1-ipconfiguration-staticdefaultgateway"):
        assert edge.WRITE_RESOLVE(pid) is not None
        assert pid in edge.WRITABLE_NOW()


def test_a_bridge_that_does_not_exist_is_not_writable(edge, nm):
    assert edge.WRITE_RESOLVE(
        "0-0-networking-bridges-7-ipconfiguration-addresses") is None


def test_nothing_else_under_ipconfiguration_became_writable(edge, nm):
    for attr in ("currentaddresses", "currentdefaultgateway", "sources"):
        assert edge.WRITE_RESOLVE(
            f"0-0-networking-bridges-1-ipconfiguration-{attr}") is None


@pytest.mark.parametrize("bad", ["nope", ["192.168.2.17"], ["192.168.2.300/24"],
                                 ["fd00::1/64"], [42], "192.168.2.17/24",
                                 ["10.0.0.1/24", "10.0.0.1/24"]])
def test_bad_addresses_never_reach_networkmanager(edge, nm, bad):
    calls, _profile = nm
    with pytest.raises(WriteError) as e:
        edge.WRITE_RESOLVE("0-0-networking-bridges-1-ipconfiguration-addresses")(bad)
    assert e.value.status == 400
    assert calls == []


def test_a_valid_address_is_normalised_and_applied(edge, nm):
    calls, _profile = nm
    write = edge.WRITE_RESOLVE("0-0-networking-bridges-1-ipconfiguration-addresses")
    assert write(["192.168.2.50/255.255.255.0".replace("/255.255.255.0", "/24")]) \
        == ["192.168.2.50/24"]
    assert calls == [("X1", ["192.168.2.50/24"], "192.168.2.1")]


def test_setting_addresses_keeps_the_gateway(edge, nm):
    calls, _profile = nm
    edge.WRITE_RESOLVE("0-0-networking-bridges-1-ipconfiguration-addresses")(
        ["10.0.0.5/8"])
    assert calls[-1][2] == "192.168.2.1", "the gateway has its own id; do not clear it"


def test_clearing_the_addresses_hands_the_link_back_to_dhcp(edge, nm):
    calls, _profile = nm
    assert edge.WRITE_RESOLVE(
        "0-0-networking-bridges-1-ipconfiguration-addresses")([]) == []
    assert calls == [("X1", [], "192.168.2.1")]


@pytest.mark.parametrize("bad", ["nope", "192.168.2.1/24", 42, ["192.168.2.1"],
                                 "fd00::1"])
def test_bad_gateways_are_refused(edge, nm, bad):
    calls, _profile = nm
    with pytest.raises(WriteError) as e:
        edge.WRITE_RESOLVE(
            "0-0-networking-bridges-1-ipconfiguration-staticdefaultgateway")(bad)
    assert e.value.status == 400
    assert calls == []


def test_a_gateway_without_a_static_address_is_refused(edge, nm):
    _calls, profile = nm
    profile["X1"]["addresses"] = []                  # DHCP
    with pytest.raises(WriteError) as e:
        edge.WRITE_RESOLVE(
            "0-0-networking-bridges-1-ipconfiguration-staticdefaultgateway")(
                "192.168.2.1")
    assert e.value.status == 400 and "static address" in e.value.detail


def test_clearing_the_gateway_is_allowed_without_an_address(edge, nm):
    calls, profile = nm
    profile["X1"]["addresses"] = []
    assert edge.WRITE_RESOLVE(
        "0-0-networking-bridges-1-ipconfiguration-staticdefaultgateway")("") == ""
    assert calls == [("X1", [], "")]


# ---- no NetworkManager, no writer ------------------------------------------

def test_without_networkmanager_the_write_fails_loudly(edge, monkeypatch):
    monkeypatch.setattr(net.nmcfg, "available", lambda: False)
    with pytest.raises(WriteError) as e:
        edge.WRITE_RESOLVE("0-0-networking-bridges-1-ipconfiguration-addresses")(
            ["10.0.0.5/8"])
    assert e.value.status == 503
    # and it says why there is no second writer to fall back to
    assert "systemd-networkd" in e.value.detail


def test_a_networkmanager_refusal_is_503_not_a_traceback(edge, nm, monkeypatch):
    def boom(ifn, addresses, gateway):
        raise nmcfg.NMError("ipv4.method: 'manual' requires an address")
    monkeypatch.setattr(net.nmcfg, "set_static_ipv4", boom)
    with pytest.raises(WriteError) as e:
        edge.WRITE_RESOLVE("0-0-networking-bridges-1-ipconfiguration-addresses")(
            ["10.0.0.5/8"])
    assert e.value.status == 503 and "requires an address" in e.value.detail
