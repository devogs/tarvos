"""Operational state of the rendered FRR models, from FRR's ``show ... json`` output.

bgpd is not an mgmtd backend in FRR 10.7, so its state cannot come from mgmtd. Each
function here converts the decoded JSON of one or more vtysh commands into the
RFC 7951-style dictionaries that sysrepo-python expects for a ``config false`` tree.
Pure functions only: :mod:`tarvos_frr.vtysh` runs the commands.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

# FRR JSON address-family keys -> tarvos-bgp afi-safi
AFI_SAFI = {"ipv4Unicast": "ipv4-unicast", "ipv6Unicast": "ipv6-unicast", "l2VpnEvpn": "l2vpn-evpn"}
SESSION_STATES = {
    "idle", "connect", "active", "opensent", "openconfirm", "established", "clearing", "deleted",
}  # fmt: skip
EVPN_ROUTE_TYPES = range(1, 6)
_RT = re.compile(r"RT:(\S+)")
_RMAC = re.compile(r"Rmac:([0-9A-Fa-f:]{17})")
_ROUTE_TYPE = re.compile(r"^\[(\d)\]")


def decode_many(text: str) -> list[Any]:
    """Decode several JSON documents printed one after the other (one vtysh call)."""
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    position, end = 0, len(text)
    while True:
        while position < end and text[position].isspace():
            position += 1
        if position >= end:
            return documents
        document, position = decoder.raw_decode(text, position)
        documents.append(document)


def _ip(value: Any) -> str | None:
    """A valid IP address in canonical form, or None (FRR prints 'Unknown', '::', ...)."""
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        return None
    return None if address.is_unspecified else str(address)


def _uint(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _set(entry: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and value != "":
        entry[key] = value


def _children(data: Mapping[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    """(key, object) pairs of a JSON object, skipping scalar bookkeeping keys."""
    return ((key, value) for key, value in data.items() if isinstance(value, dict))


# -- BGP instances and neighbours ----------------------------------------------------


def neighbor_state(address: str, data: Mapping[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"address": address}
    _set(entry, "description", data.get("nbrDesc"))
    hostname = data.get("hostname")
    if hostname and hostname != "Unknown":
        entry["hostname"] = hostname
    remote_as = _uint(data.get("remoteAs"))
    _set(entry, "remote-as", remote_as or None)
    _set(entry, "local-as", _uint(data.get("localAs")) or None)
    if data.get("nbrInternalLink"):
        entry["type"] = "internal"
    elif data.get("nbrExternalLink"):
        entry["type"] = "external"
    _set(entry, "remote-router-id", _ip(data.get("remoteRouterId")))
    state = str(data.get("bgpState", "")).lower()
    if state in SESSION_STATES:
        entry["session-state"] = state
        if state == "established":
            uptime = _uint(data.get("bgpTimerUpMsec"))
            _set(entry, "uptime", None if uptime is None else uptime // 1000)
            hold = _uint(data.get("bgpTimerHoldTimeMsecs"))
            keepalive = _uint(data.get("bgpTimerKeepAliveIntervalMsecs"))
            _set(entry, "hold-time", None if hold is None else hold // 1000)
            _set(entry, "keepalive", None if keepalive is None else keepalive // 1000)
            _set(entry, "local-address", _ip(data.get("hostLocal")))
            port_local, port_remote = data.get("portLocal"), data.get("portForeign")
            _set(entry, "local-port", port_local if _uint(port_local) else None)
            _set(entry, "remote-port", port_remote if _uint(port_remote) else None)
    _set(entry, "connections-established", _uint(data.get("connectionsEstablished")))
    _set(entry, "connections-dropped", _uint(data.get("connectionsDropped")))
    _set(entry, "last-reset-reason", data.get("lastResetDueTo"))

    stats = data.get("messageStats") or {}
    messages: dict[str, Any] = {}
    for key, leaf in (
        ("totalRecv", "received"),
        ("totalSent", "sent"),
        ("updatesRecv", "updates-received"),
        ("updatesSent", "updates-sent"),
        ("notificationsRecv", "notifications-received"),
        ("notificationsSent", "notifications-sent"),
    ):
        _set(messages, leaf, _uint(stats.get(key)))
    if messages:
        entry["messages"] = messages

    families = []
    for key, info in _children(data.get("addressFamilyInfo") or {}):
        if key not in AFI_SAFI:
            continue
        family: dict[str, Any] = {"name": AFI_SAFI[key]}
        family["prefixes-received"] = _uint(info.get("acceptedPrefixCounter")) or 0
        family["prefixes-sent"] = _uint(info.get("sentPrefixCounter")) or 0
        families.append(family)
    if families:
        entry["address-family"] = families
    return entry


def bgp_instances(vrfs: Mapping[str, Any], neighbors: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``show bgp vrfs json`` + ``show bgp vrf all neighbors json`` -> /bgp/state/instance."""
    instances = []
    for name, info in sorted(_children(vrfs.get("vrfs") or {})):
        instance: dict[str, Any] = {"vrf": name}
        _set(instance, "asn", _uint(info.get("as")) or None)
        _set(instance, "router-id", _ip(info.get("routerId")))
        _set(instance, "l3vni", _uint(info.get("l3vni")) or None)
        rmac = str(info.get("rmac", "")).lower()
        if rmac and rmac != "00:00:00:00:00:00":
            instance["router-mac"] = rmac
        _set(instance, "configured-neighbors", _uint(info.get("numConfiguredPeers")))
        _set(instance, "established-neighbors", _uint(info.get("numEstablishedPeers")))
        peers = [
            neighbor_state(address, data)
            for address, data in sorted(_children(neighbors.get(name) or {}))
        ]
        if peers:
            instance["neighbor"] = peers
        instances.append(instance)
    return instances


# -- EVPN ------------------------------------------------------------------------------


def evpn_vnis(detail: Any) -> list[dict[str, Any]]:
    """``show evpn vni detail json`` (a list, or {} when there is none) -> /bgp/state/evpn/vni."""
    vnis = []
    for info in detail if isinstance(detail, list) else []:
        vni = _uint(info.get("vni"))
        kind = str(info.get("type", "")).lower()
        if not vni or kind not in ("l2", "l3"):
            continue
        entry: dict[str, Any] = {"vni": vni, "type": kind}
        _set(entry, "vxlan-interface", info.get("vxlanInterface") or info.get("vxlanIntf"))
        _set(entry, "bridge", info.get("bridge") or None)
        _set(entry, "tenant-vrf", info.get("tenantVrf"))
        _set(entry, "local-vtep", _ip(info.get("vtepIp") or info.get("localVtepIp")))
        remotes = []
        for vtep in info.get("remoteVteps") or []:
            address = _ip(vtep.get("ip") if isinstance(vtep, dict) else vtep)
            if address and address not in remotes:
                remotes.append(address)
        if remotes:
            entry["remote-vtep"] = remotes
        _set(entry, "mac-count", _uint(info.get("numMacs")))
        _set(entry, "arp-nd-count", _uint(info.get("numArpNd")))
        if kind == "l3":
            state = str(info.get("state", "")).lower()
            _set(entry, "oper-status", state if state in ("up", "down") else None)
            rmac = str(info.get("routerMac", "")).lower()
            _set(entry, "router-mac", rmac or None)
            l2vnis = [v for v in (info.get("l2Vnis") or []) if _uint(v)]
            if l2vnis:
                entry["l2vni"] = l2vnis
        vnis.append(entry)
    return sorted(vnis, key=lambda e: e["vni"])


def _best_path(paths: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((p for p in paths if p.get("bestpath")), paths[0] if paths else None)


def _path_state(path: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    peer = _ip(path.get("peerId"))
    if peer is None:
        out["source"] = "local"
    else:
        out["source"] = "internal" if path.get("pathFrom") == "internal" else "external"
        out["peer"] = peer
    hops = []
    for hop in path.get("nexthops") or []:
        address = _ip(hop.get("ip"))
        if address and address not in hops:
            hops.append(address)
    if hops:
        out["next-hop"] = hops
    communities = str((path.get("extendedCommunity") or {}).get("string", ""))
    targets = list(dict.fromkeys(_RT.findall(communities)))
    if targets:
        out["route-target"] = targets
    rmac = _RMAC.search(communities)
    if rmac:
        out["router-mac"] = rmac.group(1).lower()
    _set(out, "as-path", path.get("path"))
    return out


def evpn_routes(table: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``show bgp l2vpn evpn json`` -> /bgp/state/evpn/route entries."""
    routes = []
    for rd, prefixes in sorted(_children(table)):
        for prefix, info in sorted(_children(prefixes)):
            match = _ROUTE_TYPE.match(prefix)
            paths = [p for p in info.get("paths") or [] if isinstance(p, dict)]
            if not match or not paths:
                continue
            route_type = int(match.group(1))
            if route_type not in EVPN_ROUTE_TYPES:
                continue
            best = _best_path(paths) or {}
            entry: dict[str, Any] = {
                "route-distinguisher": rd,
                "prefix": prefix,
                "route-type": route_type,
                "path-count": len(paths),
            }
            _set(entry, "ethernet-tag", _uint(best.get("ethTag")))
            mac = str(best.get("mac", "")).lower()
            _set(entry, "mac", mac or None)
            _set(entry, "ip-address", _ip(best.get("ip")))
            if route_type == 5:
                _set(entry, "ip-prefix-length", _uint(best.get("ipLen")))
            entry["best-path"] = _path_state(best)
            routes.append(entry)
    return routes


def evpn_route_counts(routes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts = dict.fromkeys(EVPN_ROUTE_TYPES, 0)
    for route in routes:
        counts[route["route-type"]] += 1
    return [{"route-type": t, "count": n} for t, n in counts.items()]


# -- provider (sysrepo operational callbacks) ------------------------------------------

BGP = "tarvos-bgp"
BGP_STATE = f"/{BGP}:bgp/state"
# One sysrepo subscription per subtree, so a filtered <get> only runs what it needs.
BGP_OPER_XPATHS = (
    f"{BGP_STATE}/instance",
    f"{BGP_STATE}/evpn/vni",
    f"{BGP_STATE}/evpn/route-count",
    f"{BGP_STATE}/evpn/route",
)


class BgpState:
    """Builds the tarvos-bgp state subtrees; ``frr`` is :mod:`tarvos_frr.vtysh`."""

    def __init__(self, frr: Any) -> None:
        self.frr = frr

    def _bgpd(self) -> bool:
        return "bgpd" in self.frr.running_daemons()

    def get(self, xpath: str) -> dict[str, Any]:
        """The subtree of one BGP_OPER_XPATHS entry, as a {"bgp": ...} dict ({} if empty)."""
        leaf = xpath.rsplit("/", 1)[-1]
        items: list[dict[str, Any]] = []
        if leaf == "vni":
            # zebra owns the VNIs: available without bgpd.
            (detail,) = self.frr.show_json("show evpn vni detail json")
            items = evpn_vnis(detail)
        elif not self._bgpd():
            return {}
        elif leaf == "instance":
            vrfs, neighbors = self.frr.show_json(
                "show bgp vrfs json", "show bgp vrf all neighbors json"
            )
            items = bgp_instances(vrfs, neighbors)
        else:
            (table,) = self.frr.show_json("show bgp l2vpn evpn json")
            routes = evpn_routes(table)
            items = evpn_route_counts(routes) if leaf == "route-count" else routes
        if not items:
            return {}
        state: dict[str, Any] = {leaf: items} if leaf == "instance" else {"evpn": {leaf: items}}
        return {"bgp": {"state": state}}


# -- tarvos-frr: daemons configured with CLI lines --------------------------------------

_HMS = re.compile(r"^(\d+):(\d{2}):(\d{2})$")
_UNITS = {"y": 31536000, "w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_SPAN = re.compile(r"(\d+)([ywdhms])")


def duration(text: Any) -> int | None:
    """FRR uptime strings ('00:01:19', '1d02h03m', '2w3d04h') -> seconds."""
    if isinstance(text, int) and not isinstance(text, bool):
        return text if text >= 0 else None
    value = str(text or "").strip()
    match = _HMS.match(value)
    if match:
        hours, minutes, seconds = (int(g) for g in match.groups())
        return hours * 3600 + minutes * 60 + seconds
    parts = _SPAN.findall(value)
    if parts and "".join(n + u for n, u in parts) == value:
        return sum(int(n) * _UNITS[u] for n, u in parts)
    return None


def pim_neighbors(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``show ip pim neighbor json`` -> /frr/state/pim/neighbor."""
    out = []
    for interface, neighbors in sorted(_children(data)):
        for address, info in sorted(_children(neighbors)):
            if _ip(address) is None:
                continue
            entry: dict[str, Any] = {"interface": interface, "address": _ip(address)}
            _set(entry, "uptime", duration(info.get("upTime")))
            _set(entry, "holdtime-remaining", duration(info.get("holdTime")))
            _set(entry, "dr-priority", _uint(info.get("drPriority")))
            out.append(entry)
    return out


def ldp_neighbors(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``show mpls ldp neighbor json`` -> /frr/state/ldp/neighbor."""
    out = []
    for info in data.get("neighbors") or []:
        lsr = _ip(info.get("neighborId"))
        if lsr is None:
            continue
        entry: dict[str, Any] = {"lsr-id": lsr}
        family = info.get("addressFamily")
        _set(entry, "address-family", family if family in ("ipv4", "ipv6") else None)
        _set(entry, "transport-address", _ip(info.get("transportAddress")))
        _set(entry, "state", str(info.get("state", "")).lower() or None)
        _set(entry, "uptime", duration(info.get("upTime")))
        out.append(entry)
    return sorted(out, key=lambda e: e["lsr-id"])


VRRP_STATES = {"initialize", "backup", "master"}


def vrrp_routers(data: Any) -> list[dict[str, Any]]:
    """``show vrrp json`` -> /frr/state/vrrp/router."""
    out = []
    for info in data if isinstance(data, list) else []:
        vrid, interface = _uint(info.get("vrid")), info.get("interface")
        if not vrid or not interface:
            continue
        entry: dict[str, Any] = {"interface": interface, "vrid": vrid}
        _set(entry, "version", _uint(info.get("version")))
        _set(entry, "priority", _uint(info.get("priority")))
        families = []
        for key, name in (("v4", "ipv4"), ("v6", "ipv6")):
            family = info.get(key) or {}
            addresses = [a for a in (_ip(x) for x in family.get("addresses") or []) if a]
            if not addresses:
                continue  # not configured for this family
            af: dict[str, Any] = {"name": name, "virtual-address": addresses}
            status = str(family.get("status", "")).lower()
            _set(af, "state", status if status in VRRP_STATES else None)
            _set(af, "effective-priority", _uint(family.get("effectivePriority")))
            _set(af, "macvlan-interface", family.get("interface") or None)
            _set(af, "master-address", _ip(family.get("primaryAddress")))
            stats = family.get("stats") or {}
            _set(af, "advertisements-sent", _uint(stats.get("adverTx")))
            _set(af, "advertisements-received", _uint(stats.get("adverRx")))
            _set(af, "transitions", _uint(stats.get("transitions")))
            families.append(af)
        if families:
            entry["address-family"] = families
        out.append(entry)
    return sorted(out, key=lambda e: (e["interface"], e["vrid"]))


BFD_STATUS = {"up", "down", "init", "shutdown"}


def bfd_peers(data: Any) -> list[dict[str, Any]]:
    """``show bfd peers json`` -> /frr/state/bfd/peer."""
    out = []
    for info in data if isinstance(data, list) else []:
        discriminator, peer = _uint(info.get("id")), _ip(info.get("peer"))
        if not discriminator or peer is None:
            continue
        entry: dict[str, Any] = {"local-discriminator": discriminator, "peer-address": peer}
        _set(entry, "local-address", _ip(info.get("local")))
        _set(entry, "interface", info.get("interface") or None)
        _set(entry, "vrf", info.get("vrf") or None)
        entry["multihop"] = bool(info.get("multihop"))
        status = str(info.get("status", "")).lower()
        _set(entry, "status", status if status in BFD_STATUS else None)
        _set(entry, "uptime", _uint(info.get("uptime")))
        _set(entry, "downtime", _uint(info.get("downtime")))
        _set(entry, "diagnostic", info.get("diagnostic"))
        _set(entry, "remote-diagnostic", info.get("remote-diagnostic"))
        _set(entry, "remote-discriminator", _uint(info.get("remote-id")) or None)
        _set(entry, "receive-interval", _uint(info.get("receive-interval")))
        _set(entry, "transmit-interval", _uint(info.get("transmit-interval")))
        _set(entry, "detect-multiplier", _uint(info.get("detect-multiplier")))
        out.append(entry)
    return sorted(out, key=lambda e: e["local-discriminator"])


FRR = "tarvos-frr"
FRR_STATE = f"/{FRR}:frr/state"
# subtree -> (daemon, show command, parser)
FRR_PROTOCOLS: dict[str, tuple[str, str, Any]] = {
    "pim": ("pimd", "show ip pim neighbor json", pim_neighbors),
    "ldp": ("ldpd", "show mpls ldp neighbor json", ldp_neighbors),
    "vrrp": ("vrrpd", "show vrrp json", vrrp_routers),
    "bfd": ("bfdd", "show bfd peers json", bfd_peers),
}
FRR_LISTS = {"pim": "neighbor", "ldp": "neighbor", "vrrp": "router", "bfd": "peer"}
FRR_OPER_XPATHS = tuple(f"{FRR_STATE}/{name}" for name in FRR_PROTOCOLS)


class FrrDaemonState:
    """Builds the tarvos-frr state subtrees; ``frr`` is :mod:`tarvos_frr.vtysh`."""

    def __init__(self, frr: Any) -> None:
        self.frr = frr

    def get(self, xpath: str) -> dict[str, Any]:
        name = xpath.rsplit("/", 1)[-1]
        daemon, command, parser = FRR_PROTOCOLS[name]
        if daemon not in self.frr.running_daemons():
            return {}
        (data,) = self.frr.show_json(command)
        items = parser(data)
        if not items:
            return {}
        return {"frr": {"state": {name: {FRR_LISTS[name]: items}}}}
