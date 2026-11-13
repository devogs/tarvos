"""tarvos-bgp rendered into FRR bgpd configuration (BGP, MP-BGP, BGP EVPN).

The output mirrors ``show running-config`` of FRR 10.7 ('frr defaults traditional') so
that frr-reload sees no difference once a configuration has been applied.
"""

from __future__ import annotations

from typing import Any

from tarvos_frr.frrconf import Block, Rendered
from tarvos_frr.renderers.base import (
    ConfigError,
    Renderer,
    bool_line,
    flag,
    is_ip,
    is_ipv6_prefix,
    items,
    root,
)

XPATH = "/tarvos-bgp:bgp"
V4_ONLY = ("ospf", "rip")
V6_ONLY = ("ospf6", "ripng")
AF_CLI = {
    "ipv4-unicast": "ipv4 unicast",
    "ipv6-unicast": "ipv6 unicast",
    "l2vpn-evpn": "l2vpn evpn",
}


def _peer_lines(peer_id: str, cfg: dict[str, Any], with_remote_as: bool) -> list[str]:
    n = f"neighbor {peer_id}"
    lines: list[str] = []
    if with_remote_as and cfg.get("remote-as") is not None:
        lines.append(f"{n} remote-as {cfg['remote-as']}")
    if cfg.get("description"):
        lines.append(f"{n} description {cfg['description']}")
    if cfg.get("update-source"):
        lines.append(f"{n} update-source {cfg['update-source']}")
    if cfg.get("ebgp-multihop") is not None:
        lines.append(f"{n} ebgp-multihop {cfg['ebgp-multihop']}")
    if cfg.get("password"):
        lines.append(f"{n} password {cfg['password']}")
    if cfg.get("bfd-profile"):
        lines += [f"{n} bfd", f"{n} bfd profile {cfg['bfd-profile']}"]
    else:
        lines += flag(cfg.get("bfd"), f"{n} bfd")
    lines += flag(cfg.get("shutdown"), f"{n} shutdown")
    lines += bool_line(cfg.get("capability-extended-nexthop"), f"{n} capability extended-nexthop")
    timers = cfg.get("timers")
    if timers:
        lines.append(f"{n} timers {timers['keepalive']} {timers['hold-time']}")
    return lines


def _peer_af_lines(peer_id: str, af: dict[str, Any]) -> list[str]:
    n = f"neighbor {peer_id}"
    lines = bool_line(af.get("activate"), f"{n} activate")
    for leaf, word in (("route-map-in", "in"), ("route-map-out", "out")):
        if af.get(leaf):
            lines.append(f"{n} route-map {af[leaf]} {word}")
    for leaf, word in (("prefix-list-in", "in"), ("prefix-list-out", "out")):
        if af.get(leaf):
            lines.append(f"{n} prefix-list {af[leaf]} {word}")
    lines += flag(af.get("next-hop-self"), f"{n} next-hop-self")
    lines += flag(af.get("route-reflector-client"), f"{n} route-reflector-client")
    lines += flag(af.get("soft-reconfiguration-inbound"), f"{n} soft-reconfiguration inbound")
    if af.get("allowas-in") is not None:
        lines.append(f"{n} allowas-in {af['allowas-in']}")
    if af.get("maximum-prefix") is not None:
        lines.append(f"{n} maximum-prefix {af['maximum-prefix']}")
    lines += flag(af.get("default-originate"), f"{n} default-originate")
    return lines


def _unicast_lines(where: str, af_name: str, af: dict[str, Any]) -> list[str]:
    want_v6 = af_name == "ipv6-unicast"
    lines: list[str] = []
    for prefix in af.get("network") or []:
        if is_ipv6_prefix(prefix) != want_v6:
            raise ConfigError(f"{where} {af_name}: network {prefix} has the wrong family")
        lines.append(f"network {prefix}")
    for agg in items(af, "aggregate-address"):
        if is_ipv6_prefix(agg["prefix"]) != want_v6:
            raise ConfigError(f"{where} {af_name}: aggregate {agg['prefix']} has the wrong family")
        line = f"aggregate-address {agg['prefix']}"
        if agg.get("as-set"):
            line += " as-set"
        if agg.get("summary-only"):
            line += " summary-only"
        lines.append(line)
    for redist in items(af, "redistribute"):
        protocol = redist["protocol"]
        if protocol in (V4_ONLY if want_v6 else V6_ONLY):
            raise ConfigError(f"{where} {af_name}: cannot redistribute {protocol}")
        line = f"redistribute {protocol}"
        if redist.get("metric") is not None:
            line += f" metric {redist['metric']}"
        if redist.get("route-map"):
            line += f" route-map {redist['route-map']}"
        lines.append(line)
    if af.get("maximum-paths") is not None:
        lines.append(f"maximum-paths {af['maximum-paths']}")
    if af.get("maximum-paths-ibgp") is not None:
        lines.append(f"maximum-paths ibgp {af['maximum-paths-ibgp']}")
    for vrf in af.get("import-vrf") or []:
        lines.append(f"import vrf {vrf}")
    return lines


def _rd_rt(node: dict[str, Any]) -> list[str]:
    lines = [f"rd {node['rd']}"] if node.get("rd") else []
    lines += [f"route-target import {rt}" for rt in node.get("route-target-import") or []]
    lines += [f"route-target export {rt}" for rt in node.get("route-target-export") or []]
    return lines


def _evpn_lines(evpn: dict[str, Any]) -> list[str]:
    lines = flag(evpn.get("advertise-all-vni"), "advertise-all-vni")
    lines += flag(evpn.get("advertise-default-gw"), "advertise-default-gw")
    lines += flag(evpn.get("advertise-svi-ip"), "advertise-svi-ip")
    for vni in sorted(items(evpn, "vni"), key=lambda v: int(v["vni"])):
        lines.append(f"vni {vni['vni']}")
        body = _rd_rt(vni)
        body += flag(vni.get("advertise-default-gw"), "advertise-default-gw")
        body += flag(vni.get("advertise-svi-ip"), "advertise-svi-ip")
        lines += [" " + line for line in body]
        lines.append("exit-vni")
    advertise = evpn.get("advertise") or {}
    for family in ("ipv4", "ipv6"):
        if advertise.get(f"{family}-unicast"):
            line = f"advertise {family} unicast"
            if advertise.get(f"{family}-route-map"):
                line += f" route-map {advertise[f'{family}-route-map']}"
            lines.append(line)
    lines += _rd_rt(evpn)
    return lines


def _neighbor_header(neighbor: dict[str, Any]) -> list[str]:
    address = neighbor["address"]
    group = neighbor.get("peer-group")
    remote_as = neighbor.get("remote-as")
    if not is_ip(address):  # BGP unnumbered
        line = f"neighbor {address} interface"
        if group:
            line += f" peer-group {group}"
        elif remote_as is not None:
            line += f" remote-as {remote_as}"
        lines = [line]
        if group and remote_as is not None:
            lines.append(f"neighbor {address} remote-as {remote_as}")
        return lines
    lines = [f"neighbor {address} remote-as {remote_as}"] if remote_as is not None else []
    if group:
        lines.append(f"neighbor {address} peer-group {group}")
    return lines


def _instance(inst: dict[str, Any]) -> Block:
    vrf = inst["vrf"]
    header = f"router bgp {inst['asn']}" + ("" if vrf == "default" else f" vrf {vrf}")
    body: list[str] = []
    if inst.get("router-id"):
        body.append(f"bgp router-id {inst['router-id']}")
    body += bool_line(inst.get("log-neighbor-changes"), "bgp log-neighbor-changes")
    body += bool_line(inst.get("ebgp-requires-policy"), "bgp ebgp-requires-policy")
    body += bool_line(inst.get("default-ipv4-unicast"), "bgp default ipv4-unicast")
    body += bool_line(inst.get("bestpath-multipath-relax"), "bgp bestpath as-path multipath-relax")
    if inst.get("timers"):
        body.append(f"timers bgp {inst['timers']['keepalive']} {inst['timers']['hold-time']}")

    peers: list[tuple[str, dict[str, Any]]] = []
    for group in items(inst, "peer-group"):
        body.append(f"neighbor {group['name']} peer-group")
        body += _peer_lines(group["name"], group, with_remote_as=True)
        peers.append((group["name"], group))
    for neighbor in items(inst, "neighbor"):
        body += _neighbor_header(neighbor)
        body += _peer_lines(neighbor["address"], neighbor, with_remote_as=False)
        peers.append((neighbor["address"], neighbor))

    for af_name, af_cli in AF_CLI.items():
        peer_lines = [
            line
            for peer_id, cfg in peers
            for af in items(cfg, "address-family")
            if af["name"] == af_name
            for line in _peer_af_lines(peer_id, af)
        ]
        # FRR prints neighbors after the unicast settings but before the EVPN ones.
        if af_name == "l2vpn-evpn":
            af_lines = peer_lines + _evpn_lines(inst.get("l2vpn-evpn") or {})
        else:
            af_lines = _unicast_lines(header, af_name, inst.get(af_name) or {}) + peer_lines
        if af_lines:
            body.append(f"address-family {af_cli}")
            body += [" " + line for line in af_lines]
            body.append("exit-address-family")
    return Block(header, [" " + line for line in body], "exit")


class BgpRenderer(Renderer):
    key = "tarvos-bgp"
    modules = ("tarvos-bgp",)
    xpaths = (XPATH,)
    block_prefixes = ("router bgp", "bgp community-list", "bgp as-path access-list")

    def render(self, data: dict[str, Any]) -> Rendered:
        bgp = root(data, XPATH, "bgp")
        out = Rendered()
        for clist in items(bgp, "community-list"):
            kind = clist.get("type", "standard")
            for entry in sorted(items(clist, "entry"), key=lambda e: int(e["sequence"])):
                out.blocks.append(
                    Block(
                        f"bgp community-list {kind} {clist['name']} seq {entry['sequence']} "
                        f"{entry['action']} {entry['value']}"
                    )
                )
        for alist in items(bgp, "as-path-list"):
            for entry in sorted(items(alist, "entry"), key=lambda e: int(e["sequence"])):
                out.blocks.append(
                    Block(
                        f"bgp as-path access-list {alist['name']} seq {entry['sequence']} "
                        f"{entry['action']} {entry['regex']}"
                    )
                )
        instances = sorted(items(bgp, "instance"), key=lambda i: (i["vrf"] != "default", i["vrf"]))
        for inst in instances:
            out.blocks.append(_instance(inst))
        if out.blocks:
            out.daemons.add("bgpd")
        return out
