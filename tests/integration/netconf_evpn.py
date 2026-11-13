#!/usr/bin/env python3
"""Configure (or remove) a BGP EVPN leaf entirely over NETCONF, as a controller would.

Leaf N (1 or 2) of a two-leaf lab, iBGP AS 65000 over eth1 (172.30.0.1N):

* FRR native YANG through mgmtd: loopback 10.255.0.N/32 and red1 10.10.N.1/24
  (frr-interface + frr-zebra), L3VNI 5000 of VRF RED (frr-vrf + frr-zebra);
* FRR native YANG rendered by Tarvos: prefix-list and route-map (frr-filter,
  frr-route-map);
* Tarvos YANG: VRF RED (tarvos-vrf), VTEP, L2VNI 100 with anycast gateway 10.1.100.1/24
  on eth2 and L3VNI 5000 (tarvos-vxlan), BGP with EVPN type-2/type-3 (advertise-all-vni)
  and type-5 (VRF RED advertise ipv4 unicast) (tarvos-bgp).

Usage: netconf_evpn.py set|clear HOST PORT USER PASSWORD N
"""

import sys

from ncclient import manager
from ncclient.operations import RPCError

NC = "urn:ietf:params:xml:ns:netconf:base:1.0"
T = "urn:tarvos:params:xml:ns:yang:"
FRR = "http://frrouting.org/yang/"


def leaf_config(n: int) -> list[str]:
    peer = 3 - n
    return [
        f"""<lib xmlns="{FRR}interface">
  <interface><name>lo</name>
    <zebra xmlns="{FRR}zebra"><ipv4-addrs>
      <ip>10.255.0.{n}</ip><prefix-length>32</prefix-length>
    </ipv4-addrs></zebra>
  </interface>
  <interface><name>red1</name>
    <zebra xmlns="{FRR}zebra"><ipv4-addrs>
      <ip>10.10.{n}.1</ip><prefix-length>24</prefix-length>
    </ipv4-addrs></zebra>
  </interface>
</lib>""",
        f"""<lib xmlns="{FRR}vrf">
  <vrf><name>RED</name><zebra xmlns="{FRR}zebra"><l3vni-id>5000</l3vni-id></zebra></vrf>
</lib>""",
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
  <instance><vrf>default</vrf><asn>65000</asn><router-id>10.255.0.{n}</router-id>
    <ebgp-requires-policy>false</ebgp-requires-policy>
    <default-ipv4-unicast>false</default-ipv4-unicast>
    <neighbor><address>172.30.0.1{peer}</address><remote-as>internal</remote-as>
      <description>leaf{peer}</description>
      <address-family><name>ipv4-unicast</name><activate>true</activate></address-family>
      <address-family><name>l2vpn-evpn</name><activate>true</activate></address-family>
    </neighbor>
    <ipv4-unicast><redistribute><protocol>connected</protocol><route-map>LOOPBACKS</route-map></redistribute></ipv4-unicast>
    <l2vpn-evpn><advertise-all-vni>true</advertise-all-vni>
      <vni><vni>100</vni><route-target-import>65000:100</route-target-import>
        <route-target-export>65000:100</route-target-export></vni>
    </l2vpn-evpn>
  </instance>
  <instance><vrf>RED</vrf><asn>65000</asn>
    <ipv4-unicast><redistribute><protocol>connected</protocol></redistribute></ipv4-unicast>
    <l2vpn-evpn><advertise><ipv4-unicast>true</ipv4-unicast></advertise>
      <route-target-import>65000:5000</route-target-import>
      <route-target-export>65000:5000</route-target-export>
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


def main() -> int:
    action, host, port, user, password, n = sys.argv[1:7]
    parts = leaf_config(int(n)) if action == "set" else CLEAR
    with manager.connect(
        host=host,
        port=int(port),
        username=user,
        password=password,
        hostkey_verify=False,
        look_for_keys=False,
        allow_agent=False,
        timeout=60,
    ) as session:
        try:
            # One edit-config: a single transaction across every module.
            config = f'<config xmlns="{NC}">{"".join(parts)}</config>'
            session.edit_config(target="running", config=config)
        except RPCError as exc:
            print(f"edit-config refused: {exc}", file=sys.stderr)
            return 1
    print(f"leaf{n}: {action} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
