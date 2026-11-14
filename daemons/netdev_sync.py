#!/usr/bin/env python3
"""Sysrepo backend for tarvos-vrf and tarvos-vxlan: kernel VRF, bridge and VXLAN devices.

The desired devices are derived from the running datastore and reconciled with the
kernel through iproute2 (argument lists, never a shell). Devices created here carry the
interface alias ``tarvos``; only those are ever modified or deleted, so a name that is
already used by another device is refused instead of being taken over.

Reconciliation runs in the Sysrepo change event (errors refuse the edit) and again every
RECONCILE_INTERVAL seconds, so ports that appear later (containerlab attaches links
after the container started) are picked up.

The state of each configured VRF and VNI (device status, members, remote VTEPs) is
served as operational data, read from the kernel on every request.
"""

from __future__ import annotations

import ipaddress
import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.environ.get("SCRIPTS_DIR", "/opt/tarvos/scripts"))

from tarvos_common import log as _log  # noqa: E402

MODULES = ("tarvos-vrf", "tarvos-vxlan")
ALIAS = "tarvos"
DEFAULT_PORT = 4789
RECONCILE_INTERVAL = float(os.environ.get("NETDEV_RECONCILE_INTERVAL", "10"))
KIND_ORDER = {"vxlan": 0, "bridge": 1, "vrf": 2}  # deletion order; creation is reversed


class NetdevError(ValueError):
    """Configuration that cannot be applied to the kernel."""


def log(message: str) -> None:
    _log("netdev-sync", message)


@dataclass(frozen=True)
class Device:
    name: str
    kind: str  # vrf | bridge | vxlan
    table: int | None = None  # vrf
    vni: int | None = None  # vxlan
    local: str | None = None  # vxlan
    port: int | None = None  # vxlan
    master: str | None = None
    mtu: int | None = None
    mac: str | None = None  # bridge
    addresses: tuple[str, ...] = ()  # bridge
    neigh_suppress: bool = True  # vxlan bridge port

    def create_args(self) -> list[str]:
        if self.kind == "vrf":
            return ["ip", "link", "add", self.name, "type", "vrf", "table", str(self.table)]
        if self.kind == "bridge":
            return ["ip", "link", "add", self.name, "type", "bridge"]
        return [
            "ip", "link", "add", self.name, "type", "vxlan", "id", str(self.vni),
            "local", str(self.local), "dstport", str(self.port), "nolearning",
        ]  # fmt: skip


@dataclass
class Desired:
    devices: dict[str, Device] = field(default_factory=dict)
    members: dict[str, str] = field(default_factory=dict)  # port -> master (vrf/bridge)


def desired_state(
    vrfs: dict[str, Any], vxlan: dict[str, Any], reserved_vrf: str, reserved_table: int
) -> Desired:
    out = Desired()

    def add_member(port: str, master: str) -> None:
        if out.members.get(port, master) != master:
            raise NetdevError(f"{port} cannot be in both {out.members[port]} and {master}")
        out.members[port] = master

    for vrf in (vrfs.get("vrfs") or {}).get("vrf") or []:
        if vrf["name"] == reserved_vrf or int(vrf["table"]) == reserved_table:
            raise NetdevError(
                f"VRF {vrf['name']} (table {vrf['table']}) collides with the management VRF"
            )
        out.devices[vrf["name"]] = Device(vrf["name"], "vrf", table=int(vrf["table"]))
        for port in vrf.get("interface") or []:
            add_member(port, vrf["name"])

    conf = vxlan.get("vxlan") or {}
    local = conf.get("source-address")
    port = int(conf.get("udp-port", DEFAULT_PORT))
    mtu = int(conf["mtu"]) if conf.get("mtu") else None
    segments = [(entry, True) for entry in conf.get("l2vni") or []]
    segments += [(entry, False) for entry in conf.get("l3vni") or []]
    for entry, layer2 in segments:
        vni = int(entry["vni"])
        bridge, vx = f"br{vni}", f"vx{vni}"
        for name in (bridge, vx):
            if name in out.devices or name in out.members:
                raise NetdevError(f"device name {name} is already used")
        try:
            gateways = entry.get("gateway-address") or []
            addresses = tuple(str(ipaddress.ip_interface(a)) for a in gateways)
        except ValueError as exc:
            raise NetdevError(f"l2vni {vni}: {exc}") from exc
        out.devices[bridge] = Device(
            bridge,
            "bridge",
            master=entry.get("vrf"),
            mtu=mtu,
            mac=(entry.get("gateway-mac") or "").lower() or None,
            addresses=addresses if layer2 else (),
        )
        out.devices[vx] = Device(
            vx,
            "vxlan",
            vni=vni,
            local=local,
            port=port,
            master=bridge,
            mtu=mtu,
            neigh_suppress=bool(entry.get("neighbor-suppression", True)) if layer2 else True,
        )
        if layer2:
            for access in entry.get("access-interface") or []:
                add_member(access, bridge)
    for port_name in out.members:
        if port_name in out.devices:
            raise NetdevError(f"{port_name} is a Tarvos device and cannot be a member port")
    return out


def _kind(link: dict[str, Any]) -> str | None:
    return (link.get("linkinfo") or {}).get("info_kind")


def _info(link: dict[str, Any]) -> dict[str, Any]:
    return (link.get("linkinfo") or {}).get("info_data") or {}


def _matches(device: Device, link: dict[str, Any]) -> bool:
    """Immutable attributes equal (otherwise the device must be recreated)."""
    if _kind(link) != device.kind:
        return False
    info = _info(link)
    if device.kind == "vrf":
        return int(info.get("table", -1)) == device.table
    if device.kind == "vxlan":
        return (
            int(info.get("id", -1)) == device.vni
            and info.get("local") == device.local
            and int(info.get("port", -1)) == device.port
        )
    return True


def plan(
    desired: Desired,
    links: list[dict[str, Any]],
    addresses: dict[str, set[str]],
) -> tuple[list[list[str]], list[str]]:
    """iproute2 commands bringing the kernel to ``desired``, and warnings."""
    by_name = {link["ifname"]: link for link in links}
    managed = {name for name, link in by_name.items() if link.get("ifalias") == ALIAS}
    commands: list[list[str]] = []
    warnings: list[str] = []

    for name in desired.devices:
        if name in by_name and name not in managed:
            raise NetdevError(f"{name} already exists and is not managed by Tarvos")

    doomed = [
        name
        for name in managed
        if name not in desired.devices or not _matches(desired.devices[name], by_name[name])
    ]
    for name in sorted(doomed, key=lambda n: (KIND_ORDER.get(_kind(by_name[n]) or "", 9), n)):
        commands.append(["ip", "link", "del", name])
    present = set(by_name) - set(doomed)

    creation = sorted(desired.devices.values(), key=lambda d: (-KIND_ORDER[d.kind], d.name))
    for device in creation:
        if device.kind == "vxlan" and not device.local:
            raise NetdevError(f"{device.name}: tarvos-vxlan source-address is not set")
        current = by_name.get(device.name) if device.name in present else None
        if current is None:
            commands.append(device.create_args())
            commands.append(["ip", "link", "set", "dev", device.name, "alias", ALIAS])
        if device.mtu and (current is None or current.get("mtu") != device.mtu):
            commands.append(["ip", "link", "set", "dev", device.name, "mtu", str(device.mtu)])
        if device.mac and (current is None or current.get("address", "").lower() != device.mac):
            commands.append(["ip", "link", "set", "dev", device.name, "address", device.mac])
        current_master = current.get("master") if current else None
        if device.master and device.master not in desired.devices | by_name:
            warnings.append(f"{device.name}: VRF {device.master} does not exist yet")
        elif device.master and current_master != device.master:
            commands.append(["ip", "link", "set", "dev", device.name, "master", device.master])
        elif not device.master and current_master and device.kind != "vrf":
            commands.append(["ip", "link", "set", "dev", device.name, "nomaster"])
        if device.kind == "vxlan":
            suppress = "on" if device.neigh_suppress else "off"
            commands.append(
                ["bridge", "link", "set", "dev", device.name, "neigh_suppress", suppress,
                 "learning", "off"]
            )  # fmt: skip
        if device.kind == "bridge":
            have = addresses.get(device.name, set()) if current is not None else set()
            for address in sorted(set(device.addresses) - have):
                commands.append(["ip", "address", "add", address, "dev", device.name])
            for address in sorted(have - set(device.addresses)):
                commands.append(["ip", "address", "del", address, "dev", device.name])
        commands.append(["ip", "link", "set", "dev", device.name, "up"])

    tarvos_masters = set(desired.devices) | managed
    for port, master in sorted(desired.members.items()):
        link = by_name.get(port)
        if link is None:
            warnings.append(f"{port} does not exist yet; it will be attached to {master} later")
            continue
        if link.get("master") != master:
            commands.append(["ip", "link", "set", "dev", port, "master", master])
        commands.append(["ip", "link", "set", "dev", port, "up"])
    for name, link in sorted(by_name.items()):
        master = link.get("master")
        if (
            master in tarvos_masters
            and name not in desired.members
            and name not in desired.devices
            and name not in doomed
            and master not in doomed
        ):
            commands.append(["ip", "link", "set", "dev", name, "nomaster"])
    return commands, warnings


def _ip_json(*args: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ip", "-j", *args], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        raise NetdevError(f"ip {' '.join(args)}: {result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def kernel_state() -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    links = _ip_json("-d", "link", "show")
    addresses: dict[str, set[str]] = {}
    for entry in _ip_json("address", "show"):
        for info in entry.get("addr_info") or []:
            if info.get("scope") == "link":
                continue
            addresses.setdefault(entry["ifname"], set()).add(
                str(ipaddress.ip_interface(f"{info['local']}/{info['prefixlen']}"))
            )
    return links, addresses


def fdb_state() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["bridge", "-j", "fdb", "show"], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        raise NetdevError(f"bridge fdb show: {result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def execute(commands: list[list[str]]) -> None:
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise NetdevError(f"{' '.join(command)}: {result.stderr.strip() or 'failed'}")


# -- operational state -----------------------------------------------------------------

ZERO_MAC = "00:00:00:00:00:00"


def _oper_status(link: dict[str, Any] | None) -> str:
    if link is None:
        return "not-present"
    flags = set(link.get("flags") or [])
    return "up" if {"UP", "LOWER_UP"} <= flags else "down"


def _members(links: list[dict[str, Any]], master: str) -> list[str]:
    return sorted(link["ifname"] for link in links if link.get("master") == master)


def vrf_state(vrfs: dict[str, Any], links: list[dict[str, Any]]) -> dict[str, Any]:
    """Running tarvos-vrf data + ``ip -j -d link show`` -> /vrfs/vrf/state."""
    by_name = {link["ifname"]: link for link in links}
    entries = []
    for vrf in (vrfs.get("vrfs") or {}).get("vrf") or []:
        link = by_name.get(vrf["name"])
        if link is not None and _kind(link) != "vrf":
            link = None
        state: dict[str, Any] = {"oper-status": _oper_status(link)}
        if link is not None:
            table = _info(link).get("table")
            if isinstance(table, int):
                state["table"] = table
            members = _members(links, vrf["name"])
            if members:
                state["member"] = members
        entries.append({"name": vrf["name"], "state": state})
    return {"vrfs": {"vrf": entries}} if entries else {}


def _vni_state(
    vni: int, by_name: dict[str, dict[str, Any]], links: list[dict[str, Any]],
    fdb: list[dict[str, Any]],
) -> dict[str, Any]:  # fmt: skip
    vxlan_name, bridge_name = f"vx{vni}", f"br{vni}"
    vxlan = by_name.get(vxlan_name)
    if vxlan is not None and _kind(vxlan) != "vxlan":
        vxlan = None
    state: dict[str, Any] = {"oper-status": _oper_status(vxlan)}
    if vxlan is None:
        return state
    state["vxlan-interface"] = vxlan_name
    if vxlan.get("master") == bridge_name:
        state["bridge"] = bridge_name
    local = _info(vxlan).get("local")
    if local:
        state["local-vtep"] = local
    vteps: list[str] = []
    macs: set[str] = set()
    for entry in fdb:
        if entry.get("ifname") != vxlan_name or not entry.get("dst"):
            continue
        # Every FDB entry with a destination points at a remote VTEP: the all-zero MAC
        # is the L2VNI flood entry (type-3), the others are remote MACs (type-2) or,
        # on an L3VNI, router MACs.
        if entry["dst"] not in vteps:
            vteps.append(entry["dst"])
        if entry.get("mac") != ZERO_MAC:
            macs.add(str(entry.get("mac")))
    if vteps:
        state["remote-vtep"] = sorted(vteps, key=ipaddress.ip_address)
    state["remote-mac-count"] = len(macs)
    members = _members(links, bridge_name)
    if members:
        state["member"] = members
    return state


def vxlan_state(
    vxlan: dict[str, Any], links: list[dict[str, Any]], fdb: list[dict[str, Any]]
) -> dict[str, Any]:
    """Running tarvos-vxlan data + links + ``bridge -j fdb show`` -> l2vni/l3vni state."""
    by_name = {link["ifname"]: link for link in links}
    config = vxlan.get("vxlan") or {}
    out: dict[str, Any] = {}
    for kind in ("l2vni", "l3vni"):
        entries = [
            {"vni": entry["vni"], "state": _vni_state(int(entry["vni"]), by_name, links, fdb)}
            for entry in config.get(kind) or []
        ]
        if entries:
            out[kind] = entries
    return {"vxlan": out} if out else {}


XPATHS = {"tarvos-vrf": "/tarvos-vrf:vrfs", "tarvos-vxlan": "/tarvos-vxlan:vxlan"}


class Agent:
    """Sysrepo shows a change event only its own module's new data, so the other
    module's data comes from the last reconciliation (``self.data``)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reserved_vrf = os.environ.get("MGMT_VRF_NAME", "mgmt")
        self.reserved_table = int(os.environ.get("MGMT_VRF_TABLE", "1001") or 1001)
        self.data: dict[str, dict[str, Any]] = {}

    def desired(self, session: Any, module: str | None) -> tuple[Desired, dict[str, Any]]:
        from sysrepo import SysrepoNotFoundError

        data = dict(self.data)
        for name, xpath in XPATHS.items():
            if module is None or module == name or name not in data:
                try:
                    data[name] = session.get_data(xpath) or {}
                except SysrepoNotFoundError:
                    data[name] = {}
        desired = desired_state(
            data["tarvos-vrf"], data["tarvos-vxlan"], self.reserved_vrf, self.reserved_table
        )
        return desired, data

    def reconcile(self, session: Any, module: str | None = None, quiet: bool = False) -> None:
        with self.lock:
            desired, data = self.desired(session, module)
            links, addresses = kernel_state()
            commands, warnings = plan(desired, links, addresses)
            changes = [c for c in commands if c[-1] != "up" and c[:3] != ["bridge", "link", "set"]]
            execute(commands)
            self.data = data
            if not quiet:
                for warning in warnings:
                    log(f"WARNING: {warning}")
            if changes:
                log(f"applied {len(changes)} kernel change(s)")

    def on_vrf_state(self, _xpath: str, _private_data: Any) -> dict[str, Any] | None:
        try:
            return vrf_state(self.data.get("tarvos-vrf") or {}, _ip_json("-d", "link", "show"))
        except Exception as exc:  # callbacks must not raise into the C stack
            log(f"ERROR: reading VRF state: {exc}")
            return None

    def on_vxlan_state(self, _xpath: str, _private_data: Any) -> dict[str, Any] | None:
        try:
            return vxlan_state(
                self.data.get("tarvos-vxlan") or {}, _ip_json("-d", "link", "show"), fdb_state()
            )
        except Exception as exc:
            log(f"ERROR: reading VXLAN state: {exc}")
            return None


OPER_XPATHS = (
    ("tarvos-vrf", "/tarvos-vrf:vrfs/vrf/state", "on_vrf_state"),
    ("tarvos-vxlan", "/tarvos-vxlan:vxlan/l2vni/state", "on_vxlan_state"),
    ("tarvos-vxlan", "/tarvos-vxlan:vxlan/l3vni/state", "on_vxlan_state"),
)


def main() -> int:
    from sysrepo import SysrepoCallbackFailedError, SysrepoConnection, SysrepoError

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    agent = Agent()
    with SysrepoConnection() as connection, connection.start_session("running") as session:

        def callback(event_session: Any, event: str, req_id: int, module: Any) -> None:
            try:
                if event == "change":
                    try:
                        agent.reconcile(event_session, module)
                    except NetdevError:
                        with connection.start_session("running") as running:
                            agent.reconcile(running, quiet=True)
                        raise
                elif event == "abort":
                    with connection.start_session("running") as running:
                        agent.reconcile(running, quiet=True)
            except NetdevError as exc:
                error = SysrepoCallbackFailedError(str(exc))
                event_session.set_error(error)
                raise error from exc

        try:
            agent.reconcile(session)
        except NetdevError as exc:
            log(f"ERROR: initial reconciliation: {exc}")
        for module in MODULES:
            try:
                session.subscribe_module_change_unsafe(
                    module, None, callback, private_data=module
                )
            except SysrepoError as exc:
                log(f"WARNING: {module} is not available ({exc})")
        for module, xpath, method in OPER_XPATHS:
            try:
                session.subscribe_oper_data_request(module, xpath, getattr(agent, method))
            except SysrepoError as exc:
                log(f"WARNING: no operational data for {xpath} ({exc})")
        log("serving tarvos-vrf, tarvos-vxlan")
        while not stop.wait(RECONCILE_INTERVAL):
            try:
                with connection.start_session("running") as running:
                    agent.reconcile(running, quiet=True)
            except Exception as exc:  # keep the loop alive
                log(f"ERROR: periodic reconciliation: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
