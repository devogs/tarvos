"""tarvos-ospf rendered into FRR ospfd (OSPFv2) and ospf6d (OSPFv3) configuration."""

from __future__ import annotations

from typing import Any

from tarvos_frr.frrconf import Block, Rendered
from tarvos_frr.renderers.base import Renderer, bool_line, flag, items, root

V2_XPATH = "/tarvos-ospf:ospf"
V3_XPATH = "/tarvos-ospf:ospfv3"


def _redistribute(node: dict[str, Any]) -> list[str]:
    lines = []
    for redist in items(node, "redistribute"):
        line = f"redistribute {redist['protocol']}"
        if redist.get("metric") is not None:
            line += f" metric {redist['metric']}"
        if redist.get("metric-type") is not None:
            line += f" metric-type {redist['metric-type']}"
        if redist.get("route-map"):
            line += f" route-map {redist['route-map']}"
        lines.append(line)
    return lines


def _area_types(node: dict[str, Any]) -> list[str]:
    lines = []
    for area in items(node, "area"):
        kind = area.get("type", "normal")
        if kind != "normal":
            line = f"area {area['area-id']} {kind}"
            if area.get("no-summary"):
                line += " no-summary"
            lines.append(line)
    return lines


def _common(node: dict[str, Any]) -> list[str]:
    lines = bool_line(node.get("log-adjacency-changes"), "log-adjacency-changes")
    if node.get("reference-bandwidth") is not None:
        lines.append(f"auto-cost reference-bandwidth {node['reference-bandwidth']}")
    return lines


def _interface(prefix: str, iface: dict[str, Any]) -> list[str]:
    lines = []
    if iface.get("area"):
        lines.append(f"{prefix} area {iface['area']}")
    for leaf in ("cost", "hello-interval", "dead-interval", "priority"):
        if iface.get(leaf) is not None:
            lines.append(f"{prefix} {leaf} {iface[leaf]}")
    if iface.get("network-type"):
        lines.append(f"{prefix} network {iface['network-type']}")
    lines += bool_line(iface.get("passive"), f"{prefix} passive")
    if iface.get("bfd-profile"):
        lines += [f"{prefix} bfd", f"{prefix} bfd profile {iface['bfd-profile']}"]
    else:
        lines += flag(iface.get("bfd"), f"{prefix} bfd")
    return [" " + line for line in lines]


def _needs_bfd(interfaces: list[dict[str, Any]]) -> bool:
    return any(i.get("bfd") or i.get("bfd-profile") for i in interfaces)


class OspfRenderer(Renderer):
    key = "tarvos-ospf:v2"
    modules = ("tarvos-ospf",)
    xpaths = (V2_XPATH,)
    block_prefixes = ("router ospf",)
    interface_prefixes = ("ip ospf",)

    def render(self, data: dict[str, Any]) -> Rendered:
        ospf = root(data, V2_XPATH, "ospf")
        out = Rendered()
        for inst in items(ospf, "instance"):
            header = "router ospf" + ("" if inst["vrf"] == "default" else f" vrf {inst['vrf']}")
            body = [f"ospf router-id {inst['router-id']}"] if inst.get("router-id") else []
            body += _common(inst)
            body += _redistribute(inst)
            body += flag(inst.get("passive-default"), "passive-interface default")
            for area in items(inst, "area"):
                body += [
                    f"network {net} area {area['area-id']}" for net in area.get("network") or []
                ]
            body += _area_types(inst)
            default = inst.get("default-information")
            if default is not None:
                line = "default-information originate"
                if default.get("always"):
                    line += " always"
                if default.get("metric") is not None:
                    line += f" metric {default['metric']}"
                if default.get("metric-type") is not None:
                    line += f" metric-type {default['metric-type']}"
                if default.get("route-map"):
                    line += f" route-map {default['route-map']}"
                body.append(line)
            if inst.get("maximum-paths") is not None:
                body.append(f"maximum-paths {inst['maximum-paths']}")
            out.blocks.append(Block(header, [" " + line for line in body], "exit"))
        interfaces = items(ospf, "interface")
        for iface in interfaces:
            for line in _interface("ip ospf", iface):
                out.add_interface_line(iface["name"], line)
        if not out.empty:
            out.daemons.add("ospfd")
            if _needs_bfd(interfaces):
                out.daemons.add("bfdd")
        return out


class Ospf6Renderer(Renderer):
    key = "tarvos-ospf:v3"
    modules = ("tarvos-ospf",)
    xpaths = (V3_XPATH,)
    block_prefixes = ("router ospf6",)
    interface_prefixes = ("ipv6 ospf6",)

    def render(self, data: dict[str, Any]) -> Rendered:
        ospf = root(data, V3_XPATH, "ospfv3")
        out = Rendered()
        for inst in items(ospf, "instance"):
            header = "router ospf6" + ("" if inst["vrf"] == "default" else f" vrf {inst['vrf']}")
            body = [f"ospf6 router-id {inst['router-id']}"] if inst.get("router-id") else []
            body += _common(inst)
            body += _redistribute(inst)
            body += _area_types(inst)
            if inst.get("maximum-paths") is not None:
                body.append(f"maximum-paths {inst['maximum-paths']}")
            out.blocks.append(Block(header, [" " + line for line in body], "exit"))
        interfaces = items(ospf, "interface")
        for iface in interfaces:
            for line in _interface("ipv6 ospf6", iface):
                out.add_interface_line(iface["name"], line)
        if not out.empty:
            out.daemons.add("ospf6d")
            if _needs_bfd(interfaces):
                out.daemons.add("bfdd")
        return out
