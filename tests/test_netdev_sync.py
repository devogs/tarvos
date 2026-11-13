import pytest
from netdev_sync import ALIAS, NetdevError, desired_state, plan

VRFS = {"vrfs": {"vrf": [{"name": "RED", "table": 1010, "interface": ["red1"]}]}}
VXLAN = {
    "vxlan": {
        "source-address": "10.255.0.1",
        "l2vni": [
            {
                "vni": 100,
                "vrf": "RED",
                "access-interface": ["eth2"],
                "gateway-address": ["10.1.100.1/24"],
                "gateway-mac": "00:00:5E:00:01:01",
            }
        ],
        "l3vni": [{"vni": 5000, "vrf": "RED"}],
    }
}


def _desired():
    return desired_state(VRFS, VXLAN, "mgmt", 1001)


def test_desired_devices():
    d = _desired()
    assert set(d.devices) == {"RED", "br100", "vx100", "br5000", "vx5000"}
    assert d.devices["vx100"].create_args() == [
        "ip", "link", "add", "vx100", "type", "vxlan", "id", "100",
        "local", "10.255.0.1", "dstport", "4789", "nolearning",
    ]  # fmt: skip
    assert d.devices["br100"].master == "RED"
    assert d.devices["br100"].addresses == ("10.1.100.1/24",)
    assert d.devices["br100"].mac == "00:00:5e:00:01:01"
    assert d.devices["br5000"].addresses == ()
    assert d.members == {"red1": "RED", "eth2": "br100"}


def test_management_vrf_is_reserved():
    with pytest.raises(NetdevError, match="management VRF"):
        desired_state({"vrfs": {"vrf": [{"name": "mgmt", "table": 5}]}}, {}, "mgmt", 1001)
    with pytest.raises(NetdevError, match="management VRF"):
        desired_state({"vrfs": {"vrf": [{"name": "X", "table": 1001}]}}, {}, "mgmt", 1001)


def test_port_in_two_masters_is_refused():
    vrfs = {"vrfs": {"vrf": [{"name": "RED", "table": 10, "interface": ["eth2"]}]}}
    with pytest.raises(NetdevError, match="both"):
        desired_state(vrfs, VXLAN, "mgmt", 1001)


def _link(name, kind=None, alias=None, master=None, **info):
    link = {"ifname": name, "mtu": 1500, "address": "aa:aa:aa:aa:aa:aa"}
    if kind:
        link["linkinfo"] = {"info_kind": kind, "info_data": info}
    if alias:
        link["ifalias"] = alias
    if master:
        link["master"] = master
    return link


def test_plan_from_scratch_creates_in_dependency_order():
    links = [_link("eth2"), _link("red1", "dummy")]
    commands, warnings = plan(_desired(), links, {})
    adds = [c[3] for c in commands if c[:3] == ["ip", "link", "add"]]
    assert adds == ["RED", "br100", "br5000", "vx100", "vx5000"]
    assert ["ip", "link", "set", "dev", "vx100", "alias", ALIAS] in commands
    assert ["ip", "address", "add", "10.1.100.1/24", "dev", "br100"] in commands
    assert ["ip", "link", "set", "dev", "eth2", "master", "br100"] in commands
    assert ["ip", "link", "set", "dev", "red1", "master", "RED"] in commands
    assert warnings == []


def test_plan_converged_only_refreshes():
    links = [
        _link("RED", "vrf", ALIAS, table=1010),
        {**_link("br100", "bridge", ALIAS, "RED"), "address": "00:00:5e:00:01:01"},
        _link("vx100", "vxlan", ALIAS, "br100", id=100, local="10.255.0.1", port=4789),
        _link("br5000", "bridge", ALIAS, "RED"),
        _link("vx5000", "vxlan", ALIAS, "br5000", id=5000, local="10.255.0.1", port=4789),
        _link("eth2", master="br100"),
        _link("red1", "dummy", master="RED"),
    ]
    commands, _ = plan(_desired(), links, {"br100": {"10.1.100.1/24"}})
    changes = [c for c in commands if c[-1] != "up" and c[0] != "bridge"]
    assert changes == []


def test_plan_recreates_changed_vxlan_and_removes_stale_devices():
    links = [
        _link("RED", "vrf", ALIAS, table=1010),
        _link("vx100", "vxlan", ALIAS, "br100", id=100, local="10.9.9.9", port=4789),
        _link("br100", "bridge", ALIAS, "RED"),
        _link("br300", "bridge", ALIAS),
        _link("eth3", master="br300"),
        _link("eth2"),
        _link("red1"),
    ]
    commands, _ = plan(_desired(), links, {})
    assert commands.index(["ip", "link", "del", "vx100"]) < commands.index(
        ["ip", "link", "del", "br300"]
    )
    assert any(c[:4] == ["ip", "link", "add", "vx100"] for c in commands)
    assert ["ip", "link", "set", "dev", "eth3", "nomaster"] not in commands  # bridge deleted


def test_plan_refuses_foreign_device_and_waits_for_missing_ports():
    with pytest.raises(NetdevError, match="not managed"):
        plan(_desired(), [_link("br100", "bridge")], {})
    _, warnings = plan(_desired(), [], {})
    assert any("eth2 does not exist yet" in w for w in warnings)
