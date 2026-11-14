"""tarvos-bgp operational state from real FRR 10.7.1 output (tests/fixtures/frr, leaf1 of
tests/integration/evpn.sh)."""

import json
from pathlib import Path

import pytest
from tarvos_frr import state

FIXTURES = Path(__file__).parent / "fixtures" / "frr"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def test_decode_many_splits_concatenated_documents():
    assert state.decode_many('{"a":1}\n[2]\n  {}\n') == [{"a": 1}, [2], {}]
    assert state.decode_many("") == []


def test_instances_and_established_neighbor():
    instances = state.bgp_instances(load("bgp_vrfs.json"), load("bgp_vrf_all_neighbors.json"))
    by_vrf = {i["vrf"]: i for i in instances}
    assert set(by_vrf) == {"default", "RED"}
    red = by_vrf["RED"]
    assert red["l3vni"] == 5000 and red["router-mac"] == "a6:f1:5b:5b:ef:24"
    assert "neighbor" not in red
    default = by_vrf["default"]
    assert default["asn"] == 65000 and default["router-id"] == "10.255.0.1"
    assert "l3vni" not in default and "router-mac" not in default
    assert default["established-neighbors"] == 1
    (peer,) = default["neighbor"]
    assert peer["address"] == "172.30.0.12"
    assert peer["session-state"] == "established"
    assert peer["type"] == "internal"
    assert peer["remote-router-id"] == "10.255.0.2"
    assert peer["uptime"] == 28
    assert peer["hold-time"] == 180 and peer["keepalive"] == 60
    assert peer["local-address"] == "172.30.0.11" and peer["remote-port"] == 179
    assert peer["messages"]["received"] == 9 and peer["messages"]["updates-sent"] == 8
    families = {f["name"]: f for f in peer["address-family"]}
    assert families["l2vpn-evpn"] == {
        "name": "l2vpn-evpn",
        "prefixes-received": 6,
        "prefixes-sent": 6,
    }
    assert set(families) == {"ipv4-unicast", "l2vpn-evpn"}


def test_neighbor_that_is_not_established():
    instances = state.bgp_instances(
        load("bgp_vrfs.json"), load("bgp_vrf_all_neighbors_down.json")
    )
    default = next(i for i in instances if i["vrf"] == "default")
    peer = next(n for n in default["neighbor"] if n["address"] == "192.0.2.99")
    assert peer["session-state"] == "active"
    assert peer["type"] == "external" and peer["remote-as"] == 65099
    # No session: no uptime, no TCP endpoints, no (0.0.0.0) router ID, no hostname.
    for leaf in ("uptime", "local-address", "local-port", "remote-router-id", "hostname"):
        assert leaf not in peer
    assert peer["address-family"] == [
        {"name": "l2vpn-evpn", "prefixes-received": 0, "prefixes-sent": 0}
    ]


def test_evpn_vnis():
    vnis = {v["vni"]: v for v in state.evpn_vnis(load("evpn_vni_detail.json"))}
    l2, l3 = vnis[100], vnis[5000]
    assert l2["type"] == "l2" and l2["vxlan-interface"] == "vx100"
    assert l2["local-vtep"] == "10.255.0.1" and l2["remote-vtep"] == ["10.255.0.2"]
    assert l2["tenant-vrf"] == "RED" and l2["mac-count"] == 6
    assert "oper-status" not in l2
    assert l3["type"] == "l3" and l3["oper-status"] == "up"
    assert l3["vxlan-interface"] == "vx5000" and l3["local-vtep"] == "10.255.0.1"
    assert l3["l2vni"] == [100] and l3["router-mac"] == "a6:f1:5b:5b:ef:24"
    assert state.evpn_vnis({}) == []


def test_evpn_routes_and_counts():
    routes = state.evpn_routes(load("bgp_l2vpn_evpn.json"))
    assert len(routes) == 12
    counts = {c["route-type"]: c["count"] for c in state.evpn_route_counts(routes)}
    assert counts == {1: 0, 2: 6, 3: 2, 4: 0, 5: 4}

    by_key = {(r["route-distinguisher"], r["prefix"]): r for r in routes}
    imet = by_key[("10.255.0.2:2", "[3]:[0]:[32]:[10.255.0.2]")]
    assert imet["route-type"] == 3 and imet["ip-address"] == "10.255.0.2"
    assert imet["best-path"] == {
        "source": "internal",
        "peer": "172.30.0.12",
        "next-hop": ["10.255.0.2"],
        "route-target": ["65000:100"],
    }
    mac = by_key[("10.255.0.2:2", "[2]:[0]:[48]:[4e:95:dd:7a:0b:c2]")]
    assert mac["mac"] == "4e:95:dd:7a:0b:c2" and "ip-address" not in mac
    prefix = by_key[("10.10.2.1:3", "[5]:[0]:[24]:[10.10.2.0]")]
    assert prefix["ip-address"] == "10.10.2.0" and prefix["ip-prefix-length"] == 24
    assert prefix["best-path"]["router-mac"] == "6a:27:fc:3a:5f:50"
    assert prefix["best-path"]["route-target"] == ["65000:5000"]
    local = by_key[("10.10.1.1:4", "[5]:[0]:[24]:[10.10.1.0]")]
    assert local["best-path"]["source"] == "local" and "peer" not in local["best-path"]


class FakeFrr:
    def __init__(self, daemons=("mgmtd", "zebra", "bgpd")):
        self.daemons = set(daemons)
        self.calls = []
        self.outputs = {
            "show bgp vrfs json": load("bgp_vrfs.json"),
            "show bgp vrf all neighbors json": load("bgp_vrf_all_neighbors.json"),
            "show evpn vni detail json": load("evpn_vni_detail.json"),
            "show bgp l2vpn evpn json": load("bgp_l2vpn_evpn.json"),
        }

    def running_daemons(self):
        return self.daemons

    def show_json(self, *commands):
        self.calls.append(commands)
        return [self.outputs[c] for c in commands]


@pytest.mark.parametrize("xpath", state.BGP_OPER_XPATHS)
def test_provider_returns_only_the_requested_subtree(xpath):
    frr = FakeFrr()
    data = state.BgpState(frr).get(xpath)
    leaf = xpath.rsplit("/", 1)[-1]
    bgp_state = data["bgp"]["state"]
    subtree = bgp_state if leaf == "instance" else bgp_state["evpn"]
    assert list(subtree) == [leaf] and subtree[leaf]
    assert len(frr.calls) == 1


def test_provider_without_bgpd():
    frr = FakeFrr(daemons=("mgmtd", "zebra"))
    provider = state.BgpState(frr)
    assert provider.get(f"{state.BGP_STATE}/instance") == {}
    assert provider.get(f"{state.BGP_STATE}/evpn/route") == {}
    assert frr.calls == []
    # VNIs are zebra's: still served.
    assert provider.get(f"{state.BGP_STATE}/evpn/vni")["bgp"]["state"]["evpn"]["vni"]


# -- tarvos-frr (fixtures: two nodes running pimd, ldpd, vrrpd and bfdd) ---------------


@pytest.mark.parametrize(
    "text, seconds",
    [("00:01:19", 79), ("123:00:01", 442801), ("1d02h03m", 93780), ("2w3d04h", 1483200),
     (42, 42), ("never", None), ("", None), ("1d02hx", None)],
)  # fmt: skip
def test_duration(text, seconds):
    assert state.duration(text) == seconds


def test_pim_ldp_bfd():
    (pim,) = state.pim_neighbors(load("pim_neighbor.json"))
    assert pim["interface"] == "eth1" and pim["address"] == "172.29.9.12"
    assert pim["dr-priority"] == 1 and isinstance(pim["uptime"], int)
    (ldp,) = state.ldp_neighbors(load("ldp_neighbor.json"))
    assert ldp["lsr-id"] == "10.9.9.2" and ldp["state"] == "operational"
    assert ldp["transport-address"] == "172.29.9.12" and ldp["address-family"] == "ipv4"
    (bfd,) = state.bfd_peers(load("bfd_peers.json"))
    assert bfd["peer-address"] == "172.29.9.12" and bfd["status"] == "up"
    assert bfd["vrf"] == "default" and bfd["multihop"] is False
    assert bfd["detect-multiplier"] == 3 and "local-address" not in bfd


def test_vrrp_master_and_backup():
    (master,) = state.vrrp_routers(load("vrrp_master.json"))
    (backup,) = state.vrrp_routers(load("vrrp_backup.json"))
    assert (master["interface"], master["vrid"]) == ("eth1", 10)
    (v4,) = master["address-family"]  # IPv6 not configured: not reported
    assert v4["name"] == "ipv4" and v4["state"] == "master"
    assert v4["virtual-address"] == ["172.29.9.100"] and v4["macvlan-interface"] == "vrrp4-10"
    assert v4["effective-priority"] == 120 and v4["advertisements-sent"] > 0
    assert backup["address-family"][0]["state"] == "backup"


class FakeDaemons:
    def __init__(self, daemons):
        self.daemons = set(daemons)
        self.outputs = {
            "show ip pim neighbor json": load("pim_neighbor.json"),
            "show vrrp json": load("vrrp_master.json"),
            "show bfd peers json": load("bfd_peers.json"),
            "show mpls ldp neighbor json": load("ldp_neighbor.json"),
        }

    def running_daemons(self):
        return self.daemons

    def show_json(self, *commands):
        return [self.outputs[c] for c in commands]


def test_frr_daemon_state_provider():
    provider = state.FrrDaemonState(FakeDaemons({"zebra", "pimd", "vrrpd"}))
    pim = provider.get(f"{state.FRR_STATE}/pim")
    assert pim["frr"]["state"]["pim"]["neighbor"][0]["address"] == "172.29.9.12"
    assert provider.get(f"{state.FRR_STATE}/vrrp")["frr"]["state"]["vrrp"]["router"]
    assert provider.get(f"{state.FRR_STATE}/bfd") == {}  # bfdd not running
