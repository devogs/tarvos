"""FRR native policy models rendered through vtysh: frr-filter, frr-route-map (+frr-bgp-route-map).

These are library objects that every daemon uses. mgmtd only forwards them to its own
backends (zebra, staticd, ripd, ripngd), so pushing them to mgmtd would never reach
bgpd or ospfd. vtysh sends each command to every daemon (mgmtd included).

The CLI forms follow FRR 10.7's own YANG-to-CLI printers (lib/filter_cli.c,
lib/routemap_cli.c). Nodes FRR can express but Tarvos does not render yet are rejected,
never silently dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tarvos_frr.frrconf import Block, Rendered
from tarvos_frr.renderers.base import ConfigError, Renderer, ident, items, root

FILTER_XPATH = "/frr-filter:lib"
ROUTE_MAP_XPATH = "/frr-route-map:lib"

_FAMILY = {"ipv4": "ip", "ipv6": "ipv6"}


class FilterRenderer(Renderer):
    key = "frr-filter"
    modules = ("frr-filter",)
    xpaths = (FILTER_XPATH,)
    block_prefixes = (
        "ip prefix-list",
        "ipv6 prefix-list",
        "access-list",
        "ipv6 access-list",
        "mac access-list",
    )

    def render(self, data: dict[str, Any]) -> Rendered:
        lib = root(data, FILTER_XPATH, "lib")
        out = Rendered()
        for plist in items(lib, "prefix-list"):
            out.blocks.extend(Block(line) for line in self._prefix_list(plist))
        for alist in items(lib, "access-list"):
            out.blocks.extend(Block(line) for line in self._access_list(alist))
        return out

    @staticmethod
    def _prefix_list(plist: dict[str, Any]) -> list[str]:
        family = _FAMILY.get(plist["type"])
        if family is None:
            raise ConfigError(f"prefix-list {plist['name']}: unknown type {plist['type']}")
        base = f"{family} prefix-list {plist['name']}"
        lines = [f"{base} description {plist['remark']}"] if plist.get("remark") else []
        for entry in sorted(items(plist, "entry"), key=lambda e: int(e["sequence"])):
            head = f"{base} seq {entry['sequence']} {entry['action']}"
            prefix = entry.get(f"{plist['type']}-prefix")
            if prefix:
                line = f"{head} {prefix}"
                ge = entry.get(f"{plist['type']}-prefix-length-greater-or-equal")
                le = entry.get(f"{plist['type']}-prefix-length-lesser-or-equal")
                if ge is not None:
                    line += f" ge {ge}"
                if le is not None:
                    line += f" le {le}"
            elif "any" in entry:
                line = f"{head} any"
            else:
                raise ConfigError(f"{base} seq {entry['sequence']}: no {plist['type']} prefix")
            lines.append(line)
        return lines

    @staticmethod
    def _access_list(alist: dict[str, Any]) -> list[str]:
        kind = alist["type"]
        base = {"ipv4": "access-list", "ipv6": "ipv6 access-list", "mac": "mac access-list"}.get(
            kind
        )
        if base is None:
            raise ConfigError(f"access-list {alist['name']}: unknown type {kind}")
        base = f"{base} {alist['name']}"
        lines = [f"{base} remark {alist['remark']}"] if alist.get("remark") else []
        for entry in sorted(items(alist, "entry"), key=lambda e: int(e["sequence"])):
            head = f"{base} seq {entry['sequence']} {entry['action']}"
            if kind in ("ipv4", "ipv6") and entry.get(f"{kind}-prefix"):
                line = f"{head} {entry[f'{kind}-prefix']}"
                if entry.get(f"{kind}-exact-match"):
                    line += " exact-match"
            elif kind == "mac" and entry.get("mac"):
                line = f"{head} {entry['mac']}"
            elif "any" in entry:
                line = f"{head} any"
            else:
                raise ConfigError(
                    f"{base} seq {entry['sequence']}: only zebra-style entries (prefix, mac "
                    "or any) are supported"
                )
            lines.append(line)
        return lines


def _list_name(c: dict[str, Any]) -> str:
    return c["list-name"]


def _comm_list(keyword: str) -> Callable[[dict[str, Any]], str]:
    def line(c: dict[str, Any]) -> str:
        comm = c.get("comm-list") or {}
        text = f"match {keyword} {comm['comm-list-name']}"
        if comm.get("comm-list-name-exact-match"):
            text += " exact-match"
        if comm.get("comm-list-name-any"):
            text += " any"
        return text

    return line


def _peer(c: dict[str, Any]) -> str:
    for leaf in ("peer-ipv4-address", "peer-ipv6-address", "peer-interface"):
        if c.get(leaf):
            return f"match peer {c[leaf]}"
    if c.get("peer-local"):
        return "match peer local"
    raise KeyError("peer")


MATCH: dict[str, Callable[[dict[str, Any]], str]] = {
    "interface": lambda c: f"match interface {c['interface']}",
    "ipv4-address-list": lambda c: f"match ip address {_list_name(c)}",
    "ipv4-prefix-list": lambda c: f"match ip address prefix-list {_list_name(c)}",
    "ipv4-next-hop-list": lambda c: f"match ip next-hop {_list_name(c)}",
    "ipv4-next-hop-prefix-list": lambda c: f"match ip next-hop prefix-list {_list_name(c)}",
    "ipv6-address-list": lambda c: f"match ipv6 address {_list_name(c)}",
    "ipv6-prefix-list": lambda c: f"match ipv6 address prefix-list {_list_name(c)}",
    "ipv6-next-hop-list": lambda c: f"match ipv6 next-hop {_list_name(c)}",
    "ipv6-next-hop-prefix-list": lambda c: f"match ipv6 next-hop prefix-list {_list_name(c)}",
    "match-metric": lambda c: f"match metric {c['metric']}",
    "match-tag": lambda c: f"match tag {c['tag']}",
    # frr-bgp-route-map
    "match-local-preference": lambda c: f"match local-preference {c['local-preference']}",
    "match-origin": lambda c: f"match origin {c['origin']}",
    "as-path-list": lambda c: f"match as-path {_list_name(c)}",
    "match-community": _comm_list("community"),
    "match-large-community": _comm_list("large-community"),
    "match-extcommunity": lambda c: f"match extcommunity {c['comm-list']['comm-list-name']}",
    "evpn-route-type": lambda c: f"match evpn route-type {c['evpn-route-type']}",
    "evpn-vni": lambda c: f"match evpn vni {c['evpn-vni']}",
    "evpn-rd": lambda c: f"match evpn rd {c['route-distinguisher']}",
    "evpn-default-route": lambda c: "match evpn default-route",
    "source-protocol": lambda c: f"match source-protocol {ident(c['source-protocol'])}",
    "peer": _peer,
}


def _set_metric(a: dict[str, Any]) -> str:
    for leaf, word in (
        ("use-round-trip-time", "rtt"),
        ("use-igp", "igp"),
        ("use-aigp", "aigp"),
        ("add-round-trip-time", "+rtt"),
        ("subtract-round-trip-time", "-rtt"),
    ):
        if a.get(leaf):
            return f"set metric {word}"
    if a.get("add-metric") is not None:
        return f"set metric +{a['add-metric']}"
    if a.get("subtract-metric") is not None:
        return f"set metric -{a['subtract-metric']}"
    return f"set metric {a['value']}"


def _set_community(keyword: str, leaf: str) -> Callable[[dict[str, Any]], str]:
    def line(a: dict[str, Any]) -> str:
        if a.get(f"{leaf}-string"):
            return f"set {keyword} {a[f'{leaf}-string']}"
        if a.get(f"{leaf}-none"):
            return f"set {keyword} none"
        raise KeyError(leaf)

    return line


def _as_path_prepend(a: dict[str, Any]) -> str:
    if a.get("prepend-as-path"):
        return f"set as-path prepend {a['prepend-as-path']}"
    return f"set as-path prepend last-as {a['last-as']}"


SET: dict[str, Callable[[dict[str, Any]], str]] = {
    "ipv4-next-hop": lambda a: f"set ip next-hop {a['ipv4-address']}",
    "ipv6-next-hop": lambda a: f"set ipv6 next-hop local {a['ipv6-address']}",
    "set-metric": _set_metric,
    "set-min-metric": lambda a: f"set min-metric {a['min-metric']}",
    "set-max-metric": lambda a: f"set max-metric {a['max-metric']}",
    "set-tag": lambda a: f"set tag {a['tag']}",
    # frr-bgp-route-map
    "weight": lambda a: f"set weight {a['weight']}",
    "set-local-preference": lambda a: f"set local-preference {a['local-pref']}",
    "set-origin": lambda a: f"set origin {a['origin']}",
    "as-path-prepend": _as_path_prepend,
    "set-community": _set_community("community", "community"),
    "set-large-community": _set_community("large-community", "large-community"),
    "set-extcommunity-rt": lambda a: f"set extcommunity rt {a['extcommunity-rt']}",
    "set-extcommunity-soo": lambda a: f"set extcommunity soo {a['extcommunity-soo']}",
    "set-ipv4-nexthop": lambda a: f"set ip next-hop {a['ipv4-nexthop']}",
    "ipv6-nexthop-global": lambda a: f"set ipv6 next-hop global {a['ipv6-address']}",
    "comm-list-delete": lambda a: f"set comm-list {a['comm-list-name']} delete",
    "atomic-aggregate": lambda a: "set atomic-aggregate",
    "originator-id": lambda a: f"set originator-id {a['originator-id']}",
    "distance": lambda a: f"set distance {a['distance']}",
    "table": lambda a: f"set table {a['table']}",
}


class RouteMapRenderer(Renderer):
    key = "frr-route-map"
    modules = ("frr-route-map", "frr-bgp-route-map")
    xpaths = (ROUTE_MAP_XPATH,)
    block_prefixes = ("route-map",)

    def render(self, data: dict[str, Any]) -> Rendered:
        out = Rendered()
        for rmap in items(root(data, ROUTE_MAP_XPATH, "lib"), "route-map"):
            for entry in sorted(items(rmap, "entry"), key=lambda e: int(e["sequence"])):
                out.blocks.append(self._entry(rmap["name"], entry))
        return out

    def _entry(self, name: str, entry: dict[str, Any]) -> Block:
        where = f"route-map {name} {entry['action']} {entry['sequence']}"
        lines: list[str] = []
        if entry.get("description"):
            lines.append(f" description {entry['description']}")
        for condition in items(entry, "match-condition"):
            lines.append(" " + self._convert(where, "match", MATCH, condition, "condition"))
        for action in items(entry, "set-action"):
            lines.append(" " + self._convert(where, "set", SET, action, "action"))
        if entry.get("call"):
            lines.append(f" call {entry['call']}")
        policy = entry.get("exit-policy")
        if policy == "next":
            lines.append(" on-match next")
        elif policy == "goto":
            lines.append(f" on-match goto {entry['goto-value']}")
        return Block(where, lines, "exit")

    @staticmethod
    def _convert(
        where: str,
        kind: str,
        table: dict[str, Callable[[dict[str, Any]], str]],
        node: dict[str, Any],
        key: str,
    ) -> str:
        name = ident(str(node[key]))
        converter = table.get(name)
        if converter is None:
            raise ConfigError(
                f"{where}: {kind} '{name}' is not supported by Tarvos yet "
                f"(supported: {', '.join(sorted(table))})"
            )
        values = node.get(f"rmap-{kind}-{'condition' if kind == 'match' else 'action'}") or {}
        try:
            return converter(values)
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"{where}: {kind} '{name}' is missing its value") from exc
