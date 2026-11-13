import pytest
from tarvos_frr.renderers import all_renderers
from tarvos_frr.renderers.base import ConfigError
from tarvos_frr.renderers.bfd import XPATH as BFD
from tarvos_frr.renderers.bfd import BfdRenderer
from tarvos_frr.renderers.isis import XPATH as ISIS
from tarvos_frr.renderers.isis import IsisRenderer
from tarvos_frr.renderers.ospf import V2_XPATH, V3_XPATH, Ospf6Renderer, OspfRenderer
from tarvos_frr.renderers.policy import (
    FILTER_XPATH,
    ROUTE_MAP_XPATH,
    FilterRenderer,
    RouteMapRenderer,
)
from tarvos_frr.renderers.raw import XPATH as RAW
from tarvos_frr.renderers.raw import RawRenderer


def test_prefix_and_access_lists():
    out = FilterRenderer().render(
        {
            FILTER_XPATH: {
                "lib": {
                    "prefix-list": [
                        {
                            "type": "ipv4",
                            "name": "LO",
                            "remark": "loopbacks",
                            "entry": [
                                {
                                    "sequence": 5,
                                    "action": "permit",
                                    "ipv4-prefix": "10.255.0.0/16",
                                    "ipv4-prefix-length-greater-or-equal": 32,
                                },
                                {"sequence": 10, "action": "deny", "any": None},
                            ],
                        }
                    ],
                    "access-list": [
                        {
                            "type": "ipv6",
                            "name": "A6",
                            "entry": [
                                {
                                    "sequence": 1,
                                    "action": "permit",
                                    "ipv6-prefix": "2001:db8::/32",
                                    "ipv6-exact-match": True,
                                }
                            ],
                        }
                    ],
                }
            }
        }
    )
    assert [b.header for b in out.blocks] == [
        "ip prefix-list LO description loopbacks",
        "ip prefix-list LO seq 5 permit 10.255.0.0/16 ge 32",
        "ip prefix-list LO seq 10 deny any",
        "ipv6 access-list A6 seq 1 permit 2001:db8::/32 exact-match",
    ]


def test_cisco_style_access_list_is_refused():
    data = {
        FILTER_XPATH: {
            "lib": {
                "access-list": [
                    {
                        "type": "ipv4",
                        "name": "X",
                        "entry": [{"sequence": 1, "action": "permit", "host": "1.1.1.1"}],
                    }
                ]
            }
        }
    }
    with pytest.raises(ConfigError, match="zebra-style"):
        FilterRenderer().render(data)


def _route_map(entry):
    return {ROUTE_MAP_XPATH: {"lib": {"route-map": [{"name": "RM", "entry": [entry]}]}}}


def test_route_map_with_bgp_actions():
    out = RouteMapRenderer().render(
        _route_map(
            {
                "sequence": 10,
                "action": "permit",
                "description": "to spines",
                "match-condition": [
                    {
                        "condition": "frr-route-map:ipv4-prefix-list",
                        "rmap-match-condition": {"list-name": "LO"},
                    },
                    {
                        "condition": "frr-bgp-route-map:match-community",
                        "rmap-match-condition": {
                            "comm-list": {
                                "comm-list-name": "CL",
                                "comm-list-name-exact-match": True,
                            }
                        },
                    },
                ],
                "set-action": [
                    {
                        "action": "frr-bgp-route-map:set-local-preference",
                        "rmap-set-action": {"local-pref": "200"},
                    },
                    {
                        "action": "frr-bgp-route-map:as-path-prepend",
                        "rmap-set-action": {"prepend-as-path": "65000 65000"},
                    },
                    {"action": "frr-route-map:set-metric", "rmap-set-action": {"add-metric": 5}},
                ],
                "exit-policy": "next",
            }
        )
    )
    (block,) = out.blocks
    assert block.header == "route-map RM permit 10"
    assert block.lines == [
        " description to spines",
        " match ip address prefix-list LO",
        " match community CL exact-match",
        " set local-preference 200",
        " set as-path prepend 65000 65000",
        " set metric +5",
        " on-match next",
    ]


def test_route_map_unsupported_identity_is_refused():
    entry = {
        "sequence": 1,
        "action": "deny",
        "set-action": [{"action": "frr-bgp-route-map:aggregator", "rmap-set-action": {}}],
    }
    with pytest.raises(ConfigError, match="'aggregator' is not supported"):
        RouteMapRenderer().render(_route_map(entry))


def test_route_map_missing_value_is_refused():
    entry = {
        "sequence": 1,
        "action": "permit",
        "match-condition": [{"condition": "frr-route-map:match-tag", "rmap-match-condition": {}}],
    }
    with pytest.raises(ConfigError, match="missing its value"):
        RouteMapRenderer().render(_route_map(entry))


def test_bfd_profiles_and_peers_in_milliseconds():
    out = BfdRenderer().render(
        {
            BFD: {
                "bfdd": {
                    "bfd": {
                        "profile": [
                            {
                                "name": "fast",
                                "detection-multiplier": 5,
                                "required-receive-interval": 100000,
                            }
                        ],
                        "sessions": {
                            "single-hop": [
                                {
                                    "dest-addr": "10.0.0.2",
                                    "interface": "eth1",
                                    "vrf": "default",
                                    "profile": "fast",
                                }
                            ],
                            "multi-hop": [
                                {
                                    "source-addr": "10.255.0.1",
                                    "dest-addr": "10.255.0.2",
                                    "vrf": "RED",
                                }
                            ],
                        },
                    }
                }
            }
        }
    )
    (block,) = out.blocks
    assert block.render() == [
        "bfd",
        " profile fast",
        "  detect-multiplier 5",
        "  receive-interval 100",
        " exit",
        " peer 10.0.0.2 interface eth1",
        "  profile fast",
        " exit",
        " peer 10.255.0.2 multihop local-address 10.255.0.1 vrf RED",
        " exit",
        "exit",
    ]
    assert out.daemons == {"bfdd"}


def test_bfd_absent_and_sub_millisecond():
    assert BfdRenderer().render({BFD: {"bfdd": {}}}).empty
    data = {
        BFD: {"bfdd": {"bfd": {"profile": [{"name": "p", "desired-transmission-interval": 1500}]}}}
    }
    with pytest.raises(ConfigError, match="whole milliseconds"):
        BfdRenderer().render(data)


def test_ospf_instance_and_interfaces():
    out = OspfRenderer().render(
        {
            V2_XPATH: {
                "ospf": {
                    "instance": [
                        {
                            "vrf": "default",
                            "router-id": "10.255.0.1",
                            "passive-default": True,
                            "area": [
                                {"area-id": "0.0.0.0", "network": ["10.0.0.0/24"]},
                                {"area-id": "0.0.0.1", "type": "stub", "no-summary": True},
                            ],
                            "default-information": {"always": True},
                        }
                    ],
                    "interface": [
                        {
                            "name": "eth1",
                            "area": "0.0.0.0",
                            "passive": False,
                            "bfd": True,
                            "cost": 10,
                        }
                    ],
                }
            }
        }
    )
    assert out.blocks[0].lines == [
        " ospf router-id 10.255.0.1",
        " passive-interface default",
        " network 10.0.0.0/24 area 0.0.0.0",
        " area 0.0.0.1 stub no-summary",
        " default-information originate always",
    ]
    assert out.interfaces == {
        "eth1": [" ip ospf area 0.0.0.0", " ip ospf cost 10", " no ip ospf passive", " ip ospf bfd"]
    }
    assert out.daemons == {"ospfd", "bfdd"}


def test_ospfv3_is_separate_from_ospfv2():
    v3, v2 = Ospf6Renderer(), OspfRenderer()
    out = v3.render({V3_XPATH: {"ospfv3": {"interface": [{"name": "eth1", "area": "0.0.0.0"}]}}})
    assert out.interfaces == {"eth1": [" ipv6 ospf6 area 0.0.0.0"]} and out.daemons == {"ospf6d"}
    assert v3.owns_block("router ospf6 vrf RED") and not v2.owns_block("router ospf6")
    assert v2.owns_interface_line(" no ip ospf passive")
    assert not v2.owns_interface_line(" ipv6 ospf6 area 0.0.0.0")


def test_isis():
    out = IsisRenderer().render(
        {
            ISIS: {
                "isis": {
                    "instance": [
                        {
                            "area-tag": "CORE",
                            "vrf": "default",
                            "is-type": "level-2-only",
                            "net": ["49.0001.0000.0000.0001.00"],
                            "redistribute": [
                                {"family": "ipv4", "protocol": "connected", "level": "level-2"}
                            ],
                        }
                    ],
                    "interface": [
                        {
                            "name": "eth1",
                            "instance": "CORE",
                            "ipv6": True,
                            "network-type": "point-to-point",
                        }
                    ],
                }
            }
        }
    )
    assert out.blocks[0].render() == [
        "router isis CORE",
        " is-type level-2-only",
        " net 49.0001.0000.0000.0001.00",
        " redistribute ipv4 connected level-2",
        "exit",
    ]
    assert out.interfaces["eth1"] == [
        " ip router isis CORE",
        " ipv6 router isis CORE",
        " isis network point-to-point",
    ]


def test_raw_daemon_blocks_and_interface_lines():
    data = {
        RAW: {
            "frr": {
                "daemon": [
                    {
                        "name": "eigrpd",
                        "block": [{"header": "router eigrp 10", "line": ["network 10.0.0.0/8"]}],
                        "interface": [{"name": "eth1", "line": ["ip hello-interval eigrp 5"]}],
                    }
                ]
            }
        }
    }
    out = RawRenderer("eigrpd").render(data)
    assert out.blocks[0].render() == ["router eigrp 10", " network 10.0.0.0/8", "exit"]
    assert out.interfaces == {"eth1": [" ip hello-interval eigrp 5"]}
    assert out.daemons == {"eigrpd"}
    assert RawRenderer("pimd").render(data).empty


def test_raw_cannot_take_over_other_daemons():
    data = {RAW: {"frr": {"daemon": [{"name": "pimd", "block": [{"header": "router bgp 1"}]}]}}}
    with pytest.raises(ConfigError, match="not a pimd block"):
        RawRenderer("pimd").render(data)
    data = {
        RAW: {
            "frr": {
                "daemon": [
                    {"name": "vrrpd", "interface": [{"name": "e", "line": ["ip ospf cost 1"]}]}
                ]
            }
        }
    }
    with pytest.raises(ConfigError, match="not a vrrpd line"):
        RawRenderer("vrrpd").render(data)
    assert not RawRenderer("pimd").owns_block("router pim6")


def test_renderer_keys_are_unique():
    keys = [r.key for r in all_renderers()]
    assert len(keys) == len(set(keys))
