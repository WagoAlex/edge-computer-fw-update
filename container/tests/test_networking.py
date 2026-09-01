"""networking provider against a fake /sys/class/net + /proc/net/route."""
import importlib

import pytest

import providers.networking as net


def fake_sysfs(root, ifaces):
    """ifaces: {name: {file: content}} - a stand-in for /sys/class/net."""
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
    """An expansion-model edge as its udev rules leave it: NICs already named
    X1/X2/X11, X1 up, X2 down, and a bridge spanning X1+X11."""
    sysfs = fake_sysfs(tmp_path / "net", {
        "X1": {"operstate": "up", "carrier": "1", "speed": "1000",
               "duplex": "full", "address": "00:30:de:56:4a:41"},
        "X2": {"operstate": "down", "carrier": "0", "speed": "-1",
               "duplex": "unknown", "address": "00:30:de:56:4a:42"},
        "X11": {"operstate": "up", "carrier": "1", "speed": "100",
                "duplex": "full", "address": "00:30:de:56:4a:4b"},
        "lo": {"operstate": "unknown", "address": "00:00:00:00:00:00"},
        "br0": {"address": "00:30:de:56:4a:40", "brif": ["X1", "X11"]},
    })
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "br0\t00000000\t0202A8C0\t0003\t0\t0\t20\t00000000\t0\t0\t0\n"
        "br0\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n")
    monkeypatch.setenv("SYSFS_NET", sysfs)
    monkeypatch.setenv("PROC_NET_ROUTE", str(route))
    monkeypatch.delenv("PORT_MAP", raising=False)
    importlib.reload(net)
    return net


def test_ports_discovered_from_udev_names(edge):
    """The udev rules already name the NICs X1/X2/X11 - no mapping table, and the
    instance id is the number in the name, so X11 is instance 11."""
    assert edge.PARAMS["0-0-networking-ethernetports"]() == [
        {"Classes": ["EthernetPort"], "Id": i} for i in (1, 2, 11)]
    assert edge.RESOLVE("0-0-networking-ethernetports-11-name") == "X11"
    assert edge.RESOLVE("0-0-networking-ethernetports-11-haslink") is True


def test_non_wago_interfaces_are_not_ports(edge):
    assert 0 not in dict(edge.PARAMS["0-0-networking-ethernetports"]())
    assert edge.RESOLVE("0-0-networking-ethernetports-3-name") is edge.NOTFOUND
    assert edge.RESOLVE("0-0-networking-ethernetports-1-nosuchattr") is edge.NOTFOUND


def test_port_link_state(edge):
    assert edge.RESOLVE("0-0-networking-ethernetports-1-enabled") is True
    assert edge.RESOLVE("0-0-networking-ethernetports-1-haslink") is True
    assert edge.RESOLVE("0-0-networking-ethernetports-2-haslink") is False
    # 1000/full -> enum member 5; 100/full -> 4; no link -> 0
    assert edge.RESOLVE("0-0-networking-ethernetports-1-currentspeedduplex") == 5
    assert edge.RESOLVE("0-0-networking-ethernetports-11-currentspeedduplex") == 4
    assert edge.RESOLVE("0-0-networking-ethernetports-2-currentspeedduplex") == 0


def test_mac_is_uppercased_like_wda(edge):
    assert edge.RESOLVE("0-0-networking-ethernetports-1-macaddress") == "00:30:DE:56:4A:41"


def test_no_host_netns_means_no_ports_not_wrong_ports(tmp_path, monkeypatch):
    """A container's own netns has no X* interfaces - report nothing, never
    dress up the container's eth0 as X1."""
    empty = fake_sysfs(tmp_path / "empty", {"eth0": {"operstate": "up"}})
    monkeypatch.setenv("SYSFS_NET", empty)
    monkeypatch.delenv("PORT_MAP", raising=False)
    importlib.reload(net)
    assert net.PARAMS["0-0-networking-ethernetports"]() == []
    assert net.RESOLVE("0-0-networking-ethernetports-1-name") is net.NOTFOUND


def test_port_map_overrides_discovery(edge, monkeypatch):
    """Escape hatch for a box whose udev rules did not run."""
    monkeypatch.setenv("PORT_MAP", "X1=X2")
    importlib.reload(net)
    assert net.PARAMS["0-0-networking-ethernetports"]() == [{"Classes": ["EthernetPort"], "Id": 1}]
    assert net.RESOLVE("0-0-networking-ethernetports-1-macaddress") == "00:30:DE:56:4A:42"


def test_bridge_membership_is_wago_port_indices(edge):
    assert edge.PARAMS["0-0-networking-bridges"]() == [{"Classes": ["Bridge"], "Id": 1}]
    assert edge.RESOLVE("0-0-networking-bridges-1-connectedethernetports") == [1, 11]
    assert edge.RESOLVE("0-0-networking-bridges-1-label") == "Bridge 1"
    assert edge.RESOLVE("0-0-networking-bridges-1-name") == "br0"
    assert edge.RESOLVE("0-0-networking-bridges-2-name") is edge.NOTFOUND


def test_routes_and_default_gateway(edge):
    edge._routes.cache_clear()
    routes = edge._routes()
    assert routes[0] == {"address": "0.0.0.0/0", "gatewayaddress": "192.168.2.2",
                         "gatewaymetric": 20, "source": 0, "interface": "br0"}
    assert routes[1]["address"] == "192.168.0.0/24"
    assert edge.RESOLVE("0-0-networking-bridges-1-ipconfiguration-currentdefaultgateway") \
        == "192.168.2.2"
    assert edge.PARAMS["0-0-networking-routing-currentroutes"]() == [
        {"Classes": ["CurrentRoute"], "Id": 1}, {"Classes": ["CurrentRoute"], "Id": 2}]


def test_route_instances_resolve_dynamically(edge):
    edge._routes.cache_clear()
    assert edge.RESOLVE("0-0-networking-routing-currentroutes-1-gatewayaddress") \
        == "192.168.2.2"
    assert edge.RESOLVE("0-0-networking-routing-currentroutes-9-address") is edge.NOTFOUND
    assert edge.RESOLVE("0-0-networking-nonsense") is edge.NOTFOUND


def test_dns_from_resolv_conf(tmp_path, monkeypatch):
    rc = tmp_path / "resolv.conf"
    rc.write_text("search lan\nnameserver 192.168.2.1\nnameserver 9.9.9.9\n")
    monkeypatch.setenv("RESOLV_CONF", str(rc))
    importlib.reload(net)
    assert net.PARAMS["0-0-networking-dns-utilizeddnsservers"]() == ["192.168.2.1", "9.9.9.9"]


def test_docker_bridges_are_not_wago_bridges(tmp_path, monkeypatch):
    """An edge running containers has docker0 and br-<netid>; they must not take
    instance ids from the real bridges."""
    sysfs = fake_sysfs(tmp_path / "net", {
        "X1": {"address": "00:30:de:56:aa:44"},
        "docker0": {"address": "22:49:90:94:71:a8", "brif": []},
        "br-e99ac2610385": {"address": "2a:3a:06:a3:03:29", "brif": []},
        "br0": {"address": "00:30:de:56:aa:40", "brif": ["X1"]},
    })
    monkeypatch.setenv("SYSFS_NET", sysfs)
    monkeypatch.delenv("PORT_MAP", raising=False)
    importlib.reload(net)
    assert net.bridges() == {1: "br0"}
    assert net.RESOLVE("0-0-networking-bridges-1-connectedethernetports") == [1]


def test_docker_routes_are_not_device_routes(tmp_path, monkeypatch):
    """Container routes must not swamp the list or shift instance ids."""
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "br-e99ac2610385\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "X1\t0002A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n")
    monkeypatch.setenv("PROC_NET_ROUTE", str(route))
    importlib.reload(net)
    net._routes.cache_clear()
    assert [r["interface"] for r in net._routes()] == ["X1"]
    assert net.RESOLVE("0-0-networking-routing-currentroutes-1-address") == "192.168.2.0/24"
