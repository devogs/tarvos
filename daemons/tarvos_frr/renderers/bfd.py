"""FRR native frr-bfdd rendered through vtysh.

bfdd implements frr-bfdd but is not an mgmtd backend in FRR 10.7, so the model is
rendered into the ``bfd`` block (the CLI forms follow bfdd/bfdd_cli.c). YANG intervals
are in microseconds, the CLI uses milliseconds.
"""

from __future__ import annotations

from typing import Any

from tarvos_frr.frrconf import Block, Rendered
from tarvos_frr.renderers.base import ConfigError, Renderer, bool_line, items, root

XPATH = "/frr-bfdd:bfdd"


def _ms(where: str, value: Any) -> int:
    micro = int(value)
    if micro % 1000:
        raise ConfigError(f"{where}: intervals must be whole milliseconds (got {micro} us)")
    return micro // 1000


def _settings(where: str, node: dict[str, Any], multihop: bool) -> list[str]:
    lines: list[str] = []
    if node.get("profile"):
        lines.append(f"profile {node['profile']}")
    if node.get("detection-multiplier") is not None:
        lines.append(f"detect-multiplier {node['detection-multiplier']}")
    if node.get("required-receive-interval") is not None:
        lines.append(f"receive-interval {_ms(where, node['required-receive-interval'])}")
    if node.get("desired-transmission-interval") is not None:
        lines.append(f"transmit-interval {_ms(where, node['desired-transmission-interval'])}")
    lines += bool_line(node.get("administrative-down"), "shutdown")
    lines += bool_line(node.get("passive-mode"), "passive-mode")
    lines += bool_line(node.get("log-session-changes"), "log-session-changes")
    if not multihop:
        lines += bool_line(node.get("echo-mode"), "echo-mode")
        if node.get("desired-echo-transmission-interval") is not None:
            interval = _ms(where, node["desired-echo-transmission-interval"])
            lines.append(f"echo transmit-interval {interval}")
        if node.get("required-echo-receive-interval") is not None:
            interval = _ms(where, node["required-echo-receive-interval"])
            lines.append(f"echo receive-interval {interval or 'disabled'}")
    if node.get("minimum-ttl") is not None:
        lines.append(f"minimum-ttl {node['minimum-ttl']}")
    for unsupported in ("authentication-key-chain", "profile-authentication"):
        if node.get(unsupported):
            raise ConfigError(f"{where}: BFD authentication is not supported by Tarvos yet")
    return lines


class BfdRenderer(Renderer):
    key = "frr-bfdd"
    modules = ("frr-bfdd",)
    xpaths = (XPATH,)
    block_prefixes = ("bfd",)

    def render(self, data: dict[str, Any]) -> Rendered:
        bfdd = root(data, XPATH, "bfdd")
        out = Rendered()
        if "bfd" not in bfdd:
            return out
        bfd = bfdd.get("bfd") or {}
        lines: list[str] = []
        for profile in items(bfd, "profile"):
            where = f"bfd profile {profile['name']}"
            lines.append(f" profile {profile['name']}")
            lines += ["  " + line for line in _settings(where, profile, multihop=False)]
            lines.append(" exit")
        sessions = bfd.get("sessions") or {}
        for peer in items(sessions, "single-hop"):
            header = f"peer {peer['dest-addr']}"
            if peer.get("source-addr"):
                header += f" local-address {peer['source-addr']}"
            if peer.get("vrf", "default") != "default":
                header += f" vrf {peer['vrf']}"
            if peer.get("interface", "*") != "*":
                header += f" interface {peer['interface']}"
            lines.append(" " + header)
            lines += ["  " + line for line in _settings(header, peer, multihop=False)]
            lines.append(" exit")
        for peer in items(sessions, "multi-hop"):
            header = f"peer {peer['dest-addr']} multihop local-address {peer['source-addr']}"
            if peer.get("vrf", "default") != "default":
                header += f" vrf {peer['vrf']}"
            lines.append(" " + header)
            lines += ["  " + line for line in _settings(header, peer, multihop=True)]
            lines.append(" exit")
        out.blocks.append(Block("bfd", lines, "exit"))
        out.daemons.add("bfdd")
        return out
