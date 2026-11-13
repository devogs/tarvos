import pytest
from tarvos_frr.frrconf import Block, parse, to_text
from tarvos_frr.renderers.base import ConfigError
from tarvos_frr.renderers.bgp import XPATH, BgpRenderer

LEAF = {
    "bgp": {
        "instance": [
            {
                "vrf": "RED",
                "asn": 65000,
                "ipv4-unicast": {"redistribute": [{"protocol": "connected"}]},
                "l2vpn-evpn": {
                    "advertise": {"ipv4-unicast": True},
                    "route-target-import": ["65000:5000"],
                    "route-target-export": ["65000:5000"],
                },
            },
            {
                "vrf": "default",
                "asn": 65000,
                "router-id": "10.255.0.2",
                "ebgp-requires-policy": False,
                "default-ipv4-unicast": False,
                "neighbor": [
                    {
                        "address": "172.30.0.11",
                        "remote-as": "internal",
                        "description": "leaf1",
                        "address-family": [
                            {"name": "ipv4-unicast", "activate": True},
                            {"name": "l2vpn-evpn", "activate": True},
                        ],
                    }
                ],
                "ipv4-unicast": {
                    "redistribute": [{"protocol": "connected", "route-map": "LOOPBACKS"}]
                },
                "l2vpn-evpn": {
                    "advertise-all-vni": True,
                    "vni": [
                        {
                            "vni": 100,
                            "route-target-import": ["65000:100"],
                            "route-target-export": ["65000:100"],
                        }
                    ],
                },
            },
        ]
    }
}

# FRR 10.7.1 "show running-config" for the configuration above (captured from the lab).
FRR_RUNNING = """\
router bgp 65000
 bgp router-id 10.255.0.2
 no bgp ebgp-requires-policy
 no bgp default ipv4-unicast
 neighbor 172.30.0.11 remote-as internal
 neighbor 172.30.0.11 description leaf1
 !
 address-family ipv4 unicast
  redistribute connected route-map LOOPBACKS
  neighbor 172.30.0.11 activate
 exit-address-family
 !
 address-family l2vpn evpn
  neighbor 172.30.0.11 activate
  advertise-all-vni
  vni 100
   route-target import 65000:100
   route-target export 65000:100
  exit-vni
 exit-address-family
exit
!
router bgp 65000 vrf RED
 !
 address-family ipv4 unicast
  redistribute connected
 exit-address-family
 !
 address-family l2vpn evpn
  advertise ipv4 unicast
  route-target import 65000:5000
  route-target export 65000:5000
 exit-address-family
exit
!
end
"""


def render(data):
    return BgpRenderer().render({XPATH: data})


def test_evpn_leaf_matches_frr_running_config():
    out = render(LEAF)
    # Same blocks and lines as FRR prints them, so frr-reload finds nothing to change.
    assert to_text(out.blocks) == to_text(parse(FRR_RUNNING))
    assert out.daemons == {"bgpd"}


def test_unnumbered_peer_group_and_bfd():
    out = render(
        {
            "bgp": {
                "instance": [
                    {
                        "vrf": "default",
                        "asn": 65001,
                        "peer-group": [
                            {"name": "fabric", "remote-as": "external", "bfd-profile": "fast"}
                        ],
                        "neighbor": [{"address": "eth1", "peer-group": "fabric"}],
                    }
                ]
            }
        }
    )
    assert out.blocks[0].lines == [
        " neighbor fabric peer-group",
        " neighbor fabric remote-as external",
        " neighbor fabric bfd",
        " neighbor fabric bfd profile fast",
        " neighbor eth1 interface peer-group fabric",
    ]


def test_community_and_as_path_lists_are_single_line_blocks():
    out = render(
        {
            "bgp": {
                "community-list": [
                    {
                        "name": "CL",
                        "entry": [
                            {"sequence": 10, "action": "permit", "value": "65000:2"},
                            {"sequence": 5, "action": "deny", "value": "65000:1"},
                        ],
                    }
                ],
                "as-path-list": [
                    {
                        "name": "AP",
                        "entry": [{"sequence": 5, "action": "permit", "regex": "^65000_"}],
                    }
                ],
            }
        }
    )
    assert [b.header for b in out.blocks] == [
        "bgp community-list standard CL seq 5 deny 65000:1",
        "bgp community-list standard CL seq 10 permit 65000:2",
        "bgp as-path access-list AP seq 5 permit ^65000_",
    ]
    assert all(b.lines == [] and b.closer is None for b in out.blocks)


def test_wrong_family_is_refused():
    data = {
        "bgp": {
            "instance": [{"vrf": "default", "asn": 1, "ipv6-unicast": {"network": ["10.0.0.0/8"]}}]
        }
    }
    with pytest.raises(ConfigError, match="wrong family"):
        render(data)
    data = {
        "bgp": {
            "instance": [
                {
                    "vrf": "default",
                    "asn": 1,
                    "ipv4-unicast": {"redistribute": [{"protocol": "ospf6"}]},
                }
            ]
        }
    }
    with pytest.raises(ConfigError, match="cannot redistribute ospf6"):
        render(data)


def test_ownership():
    renderer = BgpRenderer()
    assert renderer.owns_block("router bgp 65000 vrf RED")
    assert renderer.owns_block("bgp community-list standard X seq 5 permit 1:1")
    assert not renderer.owns_block("router bgpx")
    assert not renderer.owns_interface_line(" ip ospf area 0.0.0.0")


def test_empty_configuration_owns_nothing():
    out = render({})
    assert out.empty and out.daemons == set()
    assert out.blocks == [] and isinstance(Block("x"), Block)
