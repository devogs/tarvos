from tarvos_frr.frrconf import Block, Rendered, RenderedBlocksOwner, build_target, parse, to_text
from tarvos_frr.renderers.bgp import BgpRenderer
from tarvos_frr.renderers.ospf import OspfRenderer

RUNNING = """\
Building configuration...
frr version 10.7.1
frr defaults traditional
hostname r1
!
ip route 10.99.0.0/24 Null0
!
vrf mgmt
exit-vrf
!
interface eth1
 description uplink
 ip ospf area 0.0.0.0
 ip ospf cost 5
exit
!
router bgp 65000
 bgp router-id 1.1.1.1
 !
 address-family l2vpn evpn
  advertise-all-vni
 exit-address-family
exit
!
router ospf
 network 10.0.0.0/8 area 0
exit
!
end
"""


def test_parse_blocks():
    blocks = parse(RUNNING)
    headers = [b.header for b in blocks]
    assert headers == [
        "frr version 10.7.1",
        "frr defaults traditional",
        "hostname r1",
        "ip route 10.99.0.0/24 Null0",
        "vrf mgmt",
        "interface eth1",
        "router bgp 65000",
        "router ospf",
    ]
    vrf = blocks[4]
    assert vrf.lines == [] and vrf.closer == "exit-vrf"
    bgp = blocks[6]
    assert bgp.lines[-1] == " exit-address-family" and bgp.closer == "exit"
    assert to_text(parse(to_text(blocks))) == to_text(blocks)


def test_build_target_replaces_only_owned_parts():
    bgp = Rendered(blocks=[Block("router bgp 65001", [" bgp router-id 2.2.2.2"], "exit")])
    ospf = Rendered(interfaces={"eth1": [" ip ospf area 0.0.0.1"], "eth2": [" ip ospf cost 9"]})
    target = build_target(parse(RUNNING), [BgpRenderer(), OspfRenderer()], [bgp, ospf])
    text = to_text(target)
    assert "router bgp 65000" not in text  # owned by tarvos-bgp, replaced
    assert "router bgp 65001\n bgp router-id 2.2.2.2\nexit" in text
    assert "router ospf\n" not in text  # owned by tarvos-ospf (rendered no instance)
    assert "interface eth1\n description uplink\n ip ospf area 0.0.0.1\nexit" in text
    assert "interface eth2\n ip ospf cost 9\nexit" in text
    assert "ip route 10.99.0.0/24 Null0" in text  # mgmtd/CLI configuration untouched
    assert "vrf mgmt\nexit-vrf" in text


def test_inactive_renderer_leaves_cli_configuration_alone():
    target = build_target(parse(RUNNING), [], [])
    assert to_text(target) == to_text(parse(RUNNING))


def test_rendered_blocks_owner_owns_only_listed_headers():
    owner = RenderedBlocksOwner(OspfRenderer(), {"router ospf"})
    assert owner.owns_block("router ospf")
    assert not owner.owns_block("router ospf vrf RED")
    assert owner.owns_interface_line(" ip ospf cost 5")


def test_rendered_key_ignores_empty_interfaces():
    assert Rendered(interfaces={"eth1": []}).empty
    assert Rendered(interfaces={"eth1": []}).key() == Rendered().key()
