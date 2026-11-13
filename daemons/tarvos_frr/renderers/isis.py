"""tarvos-isis rendered into FRR isisd configuration."""

from __future__ import annotations

from typing import Any

from tarvos_frr.frrconf import Block, Rendered
from tarvos_frr.renderers.base import Renderer, bool_line, flag, items, root

XPATH = "/tarvos-isis:isis"


class IsisRenderer(Renderer):
    key = "tarvos-isis"
    modules = ("tarvos-isis",)
    xpaths = (XPATH,)
    block_prefixes = ("router isis",)
    interface_prefixes = ("ip router isis", "ipv6 router isis", "isis")

    def render(self, data: dict[str, Any]) -> Rendered:
        isis = root(data, XPATH, "isis")
        out = Rendered()
        for inst in items(isis, "instance"):
            vrf = inst.get("vrf", "default")
            header = f"router isis {inst['area-tag']}" + ("" if vrf == "default" else f" vrf {vrf}")
            body = [f"is-type {inst['is-type']}"] if inst.get("is-type") else []
            body += [f"net {net}" for net in inst.get("net") or []]
            if inst.get("metric-style"):
                body.append(f"metric-style {inst['metric-style']}")
            body += bool_line(inst.get("log-adjacency-changes"), "log-adjacency-changes")
            for redist in items(inst, "redistribute"):
                line = f"redistribute {redist['family']} {redist['protocol']} {redist['level']}"
                if redist.get("metric") is not None:
                    line += f" metric {redist['metric']}"
                if redist.get("route-map"):
                    line += f" route-map {redist['route-map']}"
                body.append(line)
            out.blocks.append(Block(header, [" " + line for line in body], "exit"))
        bfd = False
        for iface in items(isis, "interface"):
            tag = iface["instance"]
            lines = flag(iface.get("ipv4", True), f"ip router isis {tag}")
            lines += flag(iface.get("ipv6"), f"ipv6 router isis {tag}")
            if iface.get("circuit-type"):
                lines.append(f"isis circuit-type {iface['circuit-type']}")
            if iface.get("network-type") == "point-to-point":
                lines.append("isis network point-to-point")
            for leaf in ("metric", "hello-interval"):
                if iface.get(leaf) is not None:
                    lines.append(f"isis {leaf} {iface[leaf]}")
            lines += bool_line(iface.get("passive"), "isis passive")
            if iface.get("bfd-profile"):
                lines += ["isis bfd", f"isis bfd profile {iface['bfd-profile']}"]
                bfd = True
            elif iface.get("bfd"):
                lines.append("isis bfd")
                bfd = True
            for line in lines:
                out.add_interface_line(iface["name"], " " + line)
        if not out.empty:
            out.daemons.add("isisd")
            if bfd:
                out.daemons.add("bfdd")
        return out
