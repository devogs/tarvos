#!/usr/bin/env python3
"""Verify a BGP EVPN leaf from NETCONF operational data only (no vtysh), as a controller.

Checks on the tarvos-bgp, tarvos-vxlan and tarvos-vrf state trees (<get>):

* bgp: the neighbour PEER is established with prefixes received in l2vpn-evpn;
* evpn: L2VNI 100 lists REMOTE_VTEP, L3VNI 5000 is up, and the EVPN table has
  type-2, type-3 and type-5 routes (counts and route entries agree);
* vxlan/vrf: vx100 and vx5000 are up with REMOTE_VTEP in their FDB; VRF RED is up
  with its bridges enslaved.

Usage: netconf_state.py HOST PORT USER PASSWORD PEER REMOTE_VTEP
Exit 0 when everything holds, 1 otherwise (each failed check is printed).
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET  # noqa: S405 (reply of our own test node)

from ncclient import manager

NC = "urn:ietf:params:xml:ns:netconf:base:1.0"
T = "urn:tarvos:params:xml:ns:yang:"
BGP, VXLAN, VRF = f"{T}tarvos-bgp", f"{T}tarvos-vxlan", f"{T}tarvos-vrf"
# Three top-level subtrees: ncclient's ("subtree", ...) form takes a single element.
FILTER = (
    f'<filter xmlns="{NC}" type="subtree">'
    f'<bgp xmlns="{BGP}"><state/></bgp>'
    f'<vxlan xmlns="{VXLAN}"><l2vni><vni/><state/></l2vni><l3vni><vni/><state/></l3vni></vxlan>'
    f'<vrfs xmlns="{VRF}"><vrf><name/><state/></vrf></vrfs>'
    "</filter>"
)


def q(ns: str, path: str) -> str:
    return "/".join(f"{{{ns}}}{part}" for part in path.split("/"))


def check(reply: ET.Element, peer: str, remote_vtep: str) -> list[str]:
    errors: list[str] = []

    def expect(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    state = reply.find(f".//{q(BGP, 'bgp/state')}")
    expect(state is not None, "no /tarvos-bgp:bgp/state")
    if state is None:
        return errors

    neighbor = next(
        (
            n
            for n in state.iterfind(q(BGP, "instance/neighbor"))
            if n.findtext(q(BGP, "address")) == peer
        ),
        None,
    )
    expect(neighbor is not None, f"neighbour {peer} not in bgp/state")
    if neighbor is not None:
        session = neighbor.findtext(q(BGP, "session-state"))
        expect(session == "established", f"neighbour {peer} is {session}")
        evpn = next(
            (
                af
                for af in neighbor.iterfind(q(BGP, "address-family"))
                if af.findtext(q(BGP, "name")) == "l2vpn-evpn"
            ),
            None,
        )
        received = int(evpn.findtext(q(BGP, "prefixes-received")) or 0) if evpn is not None else 0
        expect(received > 0, f"no l2vpn-evpn prefixes received from {peer}")

    vnis = {v.findtext(q(BGP, "vni")): v for v in state.iterfind(q(BGP, "evpn/vni"))}
    l2 = vnis.get("100")
    expect(l2 is not None and l2.findtext(q(BGP, "type")) == "l2", "evpn: L2VNI 100 missing")
    if l2 is not None:
        remotes = [r.text for r in l2.iterfind(q(BGP, "remote-vtep"))]
        expect(remote_vtep in remotes, f"evpn: VNI 100 remote VTEPs {remotes}")
    l3 = vnis.get("5000")
    expect(
        l3 is not None and l3.findtext(q(BGP, "oper-status")) == "up", "evpn: L3VNI 5000 not up"
    )

    counts = {
        c.findtext(q(BGP, "route-type")): int(c.findtext(q(BGP, "count")) or 0)
        for c in state.iterfind(q(BGP, "evpn/route-count"))
    }
    routes: dict[str, int] = {}
    for route in state.iterfind(q(BGP, "evpn/route")):
        kind = route.findtext(q(BGP, "route-type")) or "?"
        routes[kind] = routes.get(kind, 0) + 1
    for kind in ("2", "3", "5"):
        expect(counts.get(kind, 0) > 0, f"evpn: no type-{kind} routes counted ({counts})")
        expect(routes.get(kind, 0) == counts.get(kind, 0), f"evpn: type-{kind} count mismatch")
    remote_imet = any(
        r.findtext(q(BGP, "route-type")) == "3"
        and r.findtext(q(BGP, "ip-address")) == remote_vtep
        and r.findtext(q(BGP, "best-path/source")) != "local"
        for r in state.iterfind(q(BGP, "evpn/route"))
    )
    expect(remote_imet, f"evpn: no type-3 route from {remote_vtep}")

    for kind, vni in (("l2vni", "100"), ("l3vni", "5000")):
        entry = next(
            (
                e
                for e in reply.iterfind(f".//{q(VXLAN, 'vxlan')}/{q(VXLAN, kind)}")
                if e.findtext(q(VXLAN, "vni")) == vni
            ),
            None,
        )
        vstate = entry.find(q(VXLAN, "state")) if entry is not None else None
        expect(vstate is not None, f"vxlan: no state for {kind} {vni}")
        if vstate is not None:
            status = vstate.findtext(q(VXLAN, "oper-status"))
            expect(status == "up", f"vxlan: vx{vni} is {status}")
            remotes = [r.text for r in vstate.iterfind(q(VXLAN, "remote-vtep"))]
            expect(remote_vtep in remotes, f"vxlan: vx{vni} remote VTEPs {remotes}")

    red = next(
        (
            v
            for v in reply.iterfind(f".//{q(VRF, 'vrfs')}/{q(VRF, 'vrf')}")
            if v.findtext(q(VRF, "name")) == "RED"
        ),
        None,
    )
    rstate = red.find(q(VRF, "state")) if red is not None else None
    expect(rstate is not None, "vrf: no state for RED")
    if rstate is not None:
        expect(rstate.findtext(q(VRF, "oper-status")) == "up", "vrf: RED is not up")
        members = {m.text for m in rstate.iterfind(q(VRF, "member"))}
        expect({"br100", "br5000"} <= members, f"vrf: RED members {sorted(members)}")
    return errors


def main() -> int:
    host, port, user, password, peer, remote_vtep = sys.argv[1:7]
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
        reply = session.get(filter=FILTER).xml
    errors = check(ET.fromstring(reply.encode()), peer, remote_vtep)  # noqa: S314
    for error in errors:
        print(f"state: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
