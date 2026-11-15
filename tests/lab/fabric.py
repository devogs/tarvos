#!/usr/bin/env python3
"""NETCONF configuration of the lab fabric (tests/lab/lab.sh), one edit-config per node.

* spine: iBGP route reflector (AS 65000) for IPv4 unicast and EVPN, BFD towards the
  leaves; it advertises its connected subnets so that the leaves reach each other's
  VTEP next hops (the README "underlay reachability" rule).
* leafN: loopback 10.255.0.N/32 (VTEP), VRF RED with red1 10.10.N.1/24, L2VNI 100 on
  eth2 with the anycast gateway 10.1.100.1/24, L3VNI 5000; advertises its loopback only.

Usage: fabric.py set|clear HOST ROLE [USER PASSWORD]   (ROLE: spine, leaf1, leaf2)
Importable: config(role) returns the XML parts, edit(host, parts) sends them.
"""

from __future__ import annotations

import sys

from ncclient import manager
from ncclient.operations import RPCError

NC = "urn:ietf:params:xml:ns:netconf:base:1.0"
T = "urn:tarvos:params:xml:ns:yang:"
FRR = "http://frrouting.org/yang/"
ASN = 65000


def _loopback_policy() -> list[str]:
    return [
        f"""<lib xmlns="{FRR}filter">
  <prefix-list><type>ipv4</type><name>LOOPBACKS</name>
    <entry><sequence>5</sequence><action>permit</action><ipv4-prefix>10.255.0.0/16</ipv4-prefix>
      <ipv4-prefix-length-greater-or-equal>32</ipv4-prefix-length-greater-or-equal></entry>
  </prefix-list>
</lib>""",
        f"""<lib xmlns="{FRR}route-map" xmlns:rm="{FRR}route-map">
  <route-map><name>LOOPBACKS</name>
    <entry><sequence>10</sequence><action>permit</action>
      <match-condition><condition>rm:ipv4-prefix-list</condition>
        <rmap-match-condition><list-name>LOOPBACKS</list-name></rmap-match-condition>
      </match-condition>
    </entry>
  </route-map>
</lib>""",
    ]


def _neighbor(address: str, description: str, rr_client: bool) -> str:
    rr = "<route-reflector-client>true</route-reflector-client>" if rr_client else ""
    return f"""<neighbor><address>{address}</address><remote-as>internal</remote-as>
      <description>{description}</description><bfd>true</bfd>
      <timers><keepalive>3</keepalive><hold-time>9</hold-time></timers>
      <address-family><name>ipv4-unicast</name><activate>true</activate>{rr}</address-family>
      <address-family><name>l2vpn-evpn</name><activate>true</activate>{rr}</address-family>
    </neighbor>"""


def spine() -> list[str]:
    return [
        f"""<lib xmlns="{FRR}interface">
  <interface><name>lo</name><zebra xmlns="{FRR}zebra"><ipv4-addrs>
    <ip>10.255.0.254</ip><prefix-length>32</prefix-length></ipv4-addrs></zebra></interface>
</lib>""",
        f"""<bgp xmlns="{T}tarvos-bgp">
  <instance><vrf>default</vrf><asn>{ASN}</asn><router-id>10.255.0.254</router-id>
    <ebgp-requires-policy>false</ebgp-requires-policy>
    <default-ipv4-unicast>false</default-ipv4-unicast>
    {_neighbor("10.0.1.3", "leaf1", True)}
    {_neighbor("10.0.2.3", "leaf2", True)}
    <ipv4-unicast><redistribute><protocol>connected</protocol></redistribute></ipv4-unicast>
  </instance>
</bgp>""",
    ]


def leaf(n: int) -> list[str]:
    return [
        f"""<lib xmlns="{FRR}interface">
  <interface><name>lo</name><zebra xmlns="{FRR}zebra"><ipv4-addrs>
    <ip>10.255.0.{n}</ip><prefix-length>32</prefix-length></ipv4-addrs></zebra></interface>
  <interface><name>red1</name><zebra xmlns="{FRR}zebra"><ipv4-addrs>
    <ip>10.10.{n}.1</ip><prefix-length>24</prefix-length></ipv4-addrs></zebra></interface>
</lib>""",
        f"""<lib xmlns="{FRR}vrf">
  <vrf><name>RED</name><zebra xmlns="{FRR}zebra"><l3vni-id>5000</l3vni-id></zebra></vrf>
</lib>""",
        *_loopback_policy(),
        f"""<vrfs xmlns="{T}tarvos-vrf">
  <vrf><name>RED</name><table>1010</table><interface>red1</interface></vrf>
</vrfs>""",
        f"""<vxlan xmlns="{T}tarvos-vxlan">
  <source-address>10.255.0.{n}</source-address>
  <l2vni><vni>100</vni><vrf>RED</vrf><access-interface>eth2</access-interface>
    <gateway-address>10.1.100.1/24</gateway-address><gateway-mac>00:00:5e:00:01:01</gateway-mac>
  </l2vni>
  <l3vni><vni>5000</vni><vrf>RED</vrf></l3vni>
</vxlan>""",
        f"""<bgp xmlns="{T}tarvos-bgp">
  <instance><vrf>default</vrf><asn>{ASN}</asn><router-id>10.255.0.{n}</router-id>
    <ebgp-requires-policy>false</ebgp-requires-policy>
    <default-ipv4-unicast>false</default-ipv4-unicast>
    {_neighbor(f"10.0.{n}.2", "spine", False)}
    <ipv4-unicast><redistribute><protocol>connected</protocol><route-map>LOOPBACKS</route-map></redistribute></ipv4-unicast>
    <l2vpn-evpn><advertise-all-vni>true</advertise-all-vni>
      <vni><vni>100</vni><route-target-import>{ASN}:100</route-target-import>
        <route-target-export>{ASN}:100</route-target-export></vni>
    </l2vpn-evpn>
  </instance>
  <instance><vrf>RED</vrf><asn>{ASN}</asn>
    <ipv4-unicast><redistribute><protocol>connected</protocol></redistribute></ipv4-unicast>
    <l2vpn-evpn><advertise><ipv4-unicast>true</ipv4-unicast></advertise>
      <route-target-import>{ASN}:5000</route-target-import>
      <route-target-export>{ASN}:5000</route-target-export>
    </l2vpn-evpn>
  </instance>
</bgp>""",
    ]


CLEAR = [
    f'<bgp xmlns="{T}tarvos-bgp" xmlns:nc="{NC}" nc:operation="remove"/>',
    f'<vxlan xmlns="{T}tarvos-vxlan" xmlns:nc="{NC}" nc:operation="remove"/>',
    f'<vrfs xmlns="{T}tarvos-vrf" xmlns:nc="{NC}" nc:operation="remove"/>',
    f'<lib xmlns="{FRR}route-map" xmlns:nc="{NC}" nc:operation="remove"/>',
    f'<lib xmlns="{FRR}filter" xmlns:nc="{NC}" nc:operation="remove"/>',
    f'<lib xmlns="{FRR}vrf" xmlns:nc="{NC}" nc:operation="remove"/>',
    f'<lib xmlns="{FRR}interface" xmlns:nc="{NC}" nc:operation="remove"/>',
]


def config(role: str) -> list[str]:
    if role == "spine":
        return spine()
    if role in ("leaf1", "leaf2"):
        return leaf(int(role[-1]))
    raise ValueError(f"unknown role {role}")


def connect(host: str, user: str = "admin", password: str = "admin", port: int = 830):
    return manager.connect(host=host, port=port, username=user, password=password,
                           hostkey_verify=False, look_for_keys=False, allow_agent=False,
                           timeout=120)  # fmt: skip


def edit(host: str, parts: list[str], user: str = "admin", password: str = "admin") -> None:
    with connect(host, user, password) as session:
        session.edit_config(target="running", config=f'<config xmlns="{NC}">{"".join(parts)}</config>')


def main() -> int:
    action, host, role = sys.argv[1:4]
    user, password = (sys.argv[4:6] + ["admin", "admin"][len(sys.argv[4:6]) :])[:2]
    try:
        edit(host, config(role) if action == "set" else CLEAR, user, password)
    except RPCError as exc:
        print(f"{role}: edit-config refused: {exc}", file=sys.stderr)
        return 1
    print(f"{role}: {action} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
