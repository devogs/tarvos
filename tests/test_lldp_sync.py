from lldp_configure import load_settings
from lldp_sync import build_commands, desired_state, parse_neighbors

BASE = load_settings({"LLDP_TX_INTERVAL": "20"}, hostname="r1")


def test_unconfigured_keeps_environment_values():
    desired = desired_state(BASE, None)
    commands = build_commands(desired, set())
    assert ["configure", "lldp", "tx-interval", "20"] in commands
    assert ["configure", "system", "hostname", "r1"] in commands
    assert commands[-1] == ["resume"]


def test_running_config_overrides_and_pauses():
    desired = desired_state(
        BASE,
        {
            "enabled": False,
            "tx-interval": 5,
            "interface-pattern": "eth*,!eth0",
            "interface": [{"name": "eth1", "admin-status": "rx-only"}, {"name": "eth2"}],
        },
    )
    commands = build_commands(desired, set())
    assert ["configure", "lldp", "tx-interval", "5"] in commands
    assert ["configure", "system", "interface", "pattern", "eth*,!eth0"] in commands
    assert ["configure", "ports", "eth1", "lldp", "status", "rx-only"] in commands
    assert ["configure", "ports", "eth2", "lldp", "status", "rx-and-tx"] in commands
    assert commands[-1] == ["pause"]


def test_removed_interface_is_reset():
    desired = desired_state(BASE, {"interface": [{"name": "eth1", "admin-status": "disabled"}]})
    commands = build_commands(desired, {"eth1", "eth3"})
    assert ["configure", "ports", "eth3", "lldp", "status", "rx-and-tx"] in commands
    assert ["configure", "ports", "eth1", "lldp", "status", "disabled"] in commands


def _neighbor(descr="Peer OS"):
    return {
        "via": "LLDP",
        "chassis": {
            "peer": {
                "id": {"type": "mac", "value": "aa:bb:cc:dd:ee:01"},
                "descr": descr,
                "mgmt-ip": ["10.0.0.2", "fe80::1"],
                "capability": [
                    {"type": "Bridge", "enabled": False},
                    {"type": "Router", "enabled": True},
                ],
            }
        },
        "port": {"id": {"type": "ifname", "value": "eth1"}, "descr": "uplink", "ttl": "120"},
    }


def test_parse_single_interface():
    (entry,) = parse_neighbors({"lldp": {"interface": {"eth1": _neighbor()}}})
    assert entry == {
        "interface": "eth1",
        "chassis-id": "aa:bb:cc:dd:ee:01",
        "port-id": "eth1",
        "port-description": "uplink",
        "system-name": "peer",
        "system-description": "Peer OS",
        "management-address": ["10.0.0.2", "fe80::1"],
        "capability": ["router"],
        "ttl": 120,
    }


def test_parse_several_interfaces_and_nameless_chassis():
    nameless = _neighbor()
    nameless["chassis"] = nameless["chassis"]["peer"]
    data = {"lldp": {"interface": [{"eth1": _neighbor()}, {"eth2": nameless}]}}
    entries = parse_neighbors(data)
    assert [e["interface"] for e in entries] == ["eth1", "eth2"]
    assert "system-name" not in entries[1]


def test_parse_empty():
    assert parse_neighbors({"lldp": {}}) == []
    assert parse_neighbors({}) == []
