"""tarvos-frr: CLI blocks for the daemons without a structured model (one renderer each).

A daemon only owns the block types and interface lines listed in :data:`OWNERSHIP`, so
a raw block can never take over another daemon's configuration.
"""

from __future__ import annotations

from typing import Any

from tarvos_frr.frrconf import Block, Rendered
from tarvos_frr.renderers.base import ConfigError, Renderer, items, root

XPATH = "/tarvos-frr:frr"

# daemon -> (top-level block prefixes, interface line prefixes)
OWNERSHIP: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "pimd": (("router pim",), ("ip pim", "ip igmp", "ip mroute")),
    "pim6d": (("router pim6",), ("ipv6 pim", "ipv6 mld", "ipv6 mroute")),
    "ldpd": (("mpls ldp", "l2vpn"), ()),
    "nhrpd": ((), ("ip nhrp", "ipv6 nhrp")),
    "eigrpd": (("router eigrp",), ()),
    "babeld": (("router babel",), ("babel",)),
    "pbrd": (("pbr-map",), ("pbr-policy",)),
    "vrrpd": ((), ("vrrp",)),
    "fabricd": (
        ("router openfabric",),
        ("ip router openfabric", "ipv6 router openfabric", "openfabric"),
    ),
}
RAW_DAEMONS = tuple(OWNERSHIP)


class RawRenderer(Renderer):
    modules = ("tarvos-frr",)
    xpaths = (XPATH,)
    owns_rendered_blocks_only = True

    def __init__(self, daemon: str) -> None:
        self.daemon = daemon
        self.key = f"tarvos-frr:{daemon}"
        self.block_prefixes, self.interface_prefixes = OWNERSHIP[daemon]

    def owns_interface_line(self, line: str) -> bool:
        if super().owns_interface_line(line):
            return True
        # eigrpd interface commands end with the protocol: "ip hello-interval eigrp 5"
        return (
            self.daemon == "eigrpd"
            and " eigrp" in f" {line.strip()}"
            and (line.strip().startswith(("ip ", "no ip ")))
        )

    def render(self, data: dict[str, Any]) -> Rendered:
        out = Rendered()
        entry = next(
            (d for d in items(root(data, XPATH, "frr"), "daemon") if d["name"] == self.daemon), None
        )
        if entry is None:
            return out
        for block in items(entry, "block"):
            header = block["header"]
            if not self.owns_block(header):
                raise ConfigError(
                    f"{self.daemon}: block '{header}' is not a {self.daemon} block "
                    f"(expected: {', '.join(self.block_prefixes) or 'none, interface lines only'})"
                )
            lines = [" " + line for line in block.get("line") or []]
            out.blocks.append(Block(header, lines, "exit"))
        for iface in items(entry, "interface"):
            for line in iface.get("line") or []:
                if not self.owns_interface_line(line):
                    raise ConfigError(
                        f"{self.daemon}: interface {iface['name']} line '{line}' is not a "
                        f"{self.daemon} line"
                    )
                out.add_interface_line(iface["name"], " " + line)
        if not out.empty:
            out.daemons.add(self.daemon)
        return out
