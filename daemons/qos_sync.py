#!/usr/bin/env python3
"""Sysrepo backend for tarvos-qos: per-interface queueing with Linux tc.

Each configured interface gets a root qdisc owned by Tarvos (handle 1:), rebuilt from
scratch when its configuration changes (or when the interface reappears):

* htb: root class 1:1 at ``bandwidth``; one class 1:<id in hex> per configured class
  (rate, ceil, prio) with an fq_codel leaf; a default class 1:fff (priority 7) takes
  the unclassified traffic and the bandwidth left over. DSCP values are matched by u32
  filters on the IPv4 DS field and the IPv6 traffic class.
* prio: eight bands (1:1..1:8); a class goes to band ``priority + 1``, unclassified
  traffic to band 5 (priority 4, the default).
* fq-codel, cake: a single root qdisc (cake shaped at ``bandwidth`` when set).

Interfaces removed from the configuration get their root qdisc deleted (back to the
kernel default). Interfaces that do not exist are skipped and picked up by the periodic
re-application. tc, qdisc and class counters are served as operational data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("SCRIPTS_DIR", "/opt/tarvos/scripts"))

from tarvos_backend import BackendError, serve  # noqa: E402
from tarvos_common import log as _log  # noqa: E402

MODULE = "tarvos-qos"
DEFAULT_CLASS = 0xFFF
MIN_RATE = 8  # kbit/s, tc's lower bound in the model
PRIO_BANDS = 8
PRIO_DEFAULT_BAND = 4  # 0-based band of priority 4 (classid 1:5)
INTERVAL = float(os.environ.get("QOS_REAPPLY_INTERVAL", "10"))
COUNTERS = {
    "bytes": "bytes", "packets": "packets", "drops": "drops", "overlimits": "overlimits",
    "requeues": "requeues", "backlog": "backlog-bytes", "qlen": "backlog-packets",
}  # fmt: skip


def log(message: str) -> None:
    _log("qos-sync", message)


def _interfaces(config: dict[str, Any]) -> list[dict[str, Any]]:
    return (config.get("qos") or {}).get("interface") or []


def classid(minor: int) -> str:
    return f"1:{minor:x}"


def class_minor(interface: dict[str, Any], cls: dict[str, Any]) -> int:
    """tc class minor of a configured class (htb: its id; prio: its band)."""
    if interface["qdisc"] == "prio":
        return int(cls.get("priority", 4)) + 1
    return int(cls["id"])


def _filters(dev: str, flow: str, dscps: list[int]) -> list[list[str]]:
    commands = []
    for dscp in dscps:
        tos = f"0x{int(dscp) << 2:02x}"
        base = ["tc", "filter", "add", "dev", dev, "parent", "1:"]
        commands.append(
            [*base, "protocol", "ip", "prio", "1", "u32",
             "match", "ip", "dsfield", tos, "0xfc", "flowid", flow]
        )  # fmt: skip
        commands.append(
            [*base, "protocol", "ipv6", "prio", "2", "u32",
             "match", "ip6", "priority", tos, "0xfc", "flowid", flow]
        )  # fmt: skip
    return commands


def build(interface: dict[str, Any]) -> list[list[str]]:
    """tc commands installing ``interface``'s policy (after its root qdisc was deleted)."""
    dev, kind = interface["name"], interface["qdisc"]
    bandwidth = interface.get("bandwidth")
    classes = interface.get("class") or []
    seen: dict[int, int] = {}
    for cls in classes:
        for dscp in cls.get("dscp") or []:
            if dscp in seen:
                raise BackendError(
                    f"{dev}: DSCP {dscp} is mapped to classes {seen[dscp]} and {cls['id']}"
                )
            seen[dscp] = cls["id"]
    qdisc = ["tc", "qdisc", "add", "dev", dev]

    if kind == "fq-codel":
        return [[*qdisc, "root", "fq_codel"]]
    if kind == "cake":
        shaping = ["bandwidth", f"{bandwidth}kbit"] if bandwidth else ["unlimited"]
        return [[*qdisc, "root", "cake", *shaping]]
    if kind == "prio":
        priomap = [str(PRIO_DEFAULT_BAND)] * 16
        commands = [
            [*qdisc, "root", "handle", "1:", "prio", "bands", str(PRIO_BANDS), "priomap", *priomap]
        ]
        for cls in classes:
            commands += _filters(dev, classid(class_minor(interface, cls)), cls.get("dscp") or [])
        return commands
    if kind != "htb":
        raise BackendError(f"{dev}: unsupported qdisc {kind}")

    if not bandwidth:
        raise BackendError(f"{dev}: htb needs the interface bandwidth")
    bandwidth = int(bandwidth)
    unset = [c for c in classes if not c.get("rate")]
    fixed = sum(int(c["rate"]) for c in classes if c.get("rate"))
    share = max(MIN_RATE, (bandwidth - fixed) // (len(unset) + 1)) if unset else 0
    rates = {c["id"]: int(c.get("rate") or share) for c in classes}
    if sum(rates.values()) > bandwidth:
        raise BackendError(
            f"{dev}: class rates ({sum(rates.values())} kbit/s) exceed the bandwidth "
            f"({bandwidth} kbit/s)"
        )
    commands = [
        [*qdisc, "root", "handle", "1:", "htb", "default", f"{DEFAULT_CLASS:x}"],
        ["tc", "class", "add", "dev", dev, "parent", "1:", "classid", "1:1",
         "htb", "rate", f"{bandwidth}kbit", "ceil", f"{bandwidth}kbit"],
    ]  # fmt: skip
    leaves = [
        (int(c["id"]), rates[c["id"]], int(c.get("ceil") or bandwidth), int(c.get("priority", 4)))
        for c in classes
    ]
    leaves.append((DEFAULT_CLASS, max(MIN_RATE, bandwidth - sum(rates.values())), bandwidth, 7))
    for minor, rate, ceil, prio in leaves:
        if rate > ceil:
            raise BackendError(f"{dev}: class {minor} rate {rate} is above its ceil {ceil}")
        if ceil > bandwidth:
            raise BackendError(f"{dev}: class {minor} ceil {ceil} is above the bandwidth")
        commands.append(
            ["tc", "class", "add", "dev", dev, "parent", "1:1", "classid", classid(minor),
             "htb", "rate", f"{rate}kbit", "ceil", f"{ceil}kbit", "prio", str(prio)]
        )  # fmt: skip
        commands.append([*qdisc, "parent", classid(minor), "fq_codel"])
    for cls in classes:
        commands += _filters(dev, classid(int(cls["id"])), cls.get("dscp") or [])
    return commands


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    if check and result.returncode != 0:
        raise BackendError(f"{' '.join(command)}: {result.stderr.strip() or 'failed'}")
    return result


def tc_json(*args: str) -> list[dict[str, Any]]:
    return json.loads(run(["tc", "-s", "-j", *args]).stdout or "[]")


def ifindexes() -> dict[str, int]:
    result = run(["ip", "-j", "link", "show"])
    return {link["ifname"]: int(link["ifindex"]) for link in json.loads(result.stdout or "[]")}


def _counters(data: dict[str, Any]) -> dict[str, int]:
    return {
        leaf: int(data[key]) for key, leaf in COUNTERS.items() if isinstance(data.get(key), int)
    }


def interface_state(
    interface: dict[str, Any], qdiscs: list[dict[str, Any]] | None,
    classes: list[dict[str, Any]] | None,
) -> dict[str, Any]:  # fmt: skip
    """Configured interface + ``tc -s -j qdisc/class show dev`` -> interface state entry."""
    entry: dict[str, Any] = {"name": interface["name"]}
    if qdiscs is None:
        entry["state"] = {"oper-status": "not-present"}
        return entry
    root = next((q for q in qdiscs if q.get("root")), None)
    state: dict[str, Any] = {"oper-status": "present"}
    if root is not None:
        state["root-qdisc"] = {"kind": str(root.get("kind")), **_counters(root)}
    by_handle = {c.get("handle"): c for c in classes or []}
    class_entries = []
    for cls in interface.get("class") or []:
        found = by_handle.get(classid(class_minor(interface, cls)))
        if found is not None:
            class_entries.append({"id": cls["id"], "state": _counters(found.get("stats") or {})})
    default = by_handle.get(classid(DEFAULT_CLASS)) if interface["qdisc"] == "htb" else None
    if default is not None:
        state["default-class"] = _counters(default.get("stats") or {})
    entry["state"] = state
    if class_entries:
        entry["class"] = class_entries
    return entry


class QosBackend:
    module = MODULE
    xpath = f"/{MODULE}:qos"
    name = "qos-sync"
    interval: float | None = INTERVAL

    def __init__(self) -> None:
        # interface -> (configuration it was built from, ifindex it was built on)
        self.applied: dict[str, tuple[str, int]] = {}
        self.missing: set[str] = set()  # configured interfaces that do not exist
        self.oper = [
            (f"/{MODULE}:qos/interface/state", self.state),
            (f"/{MODULE}:qos/interface/class/state", self.state),
        ]

    def apply(self, config: dict[str, Any]) -> None:
        present = ifindexes()
        desired = {i["name"]: i for i in _interfaces(config)}
        for name in sorted(set(self.applied) - set(desired)):
            if name in present:
                run(["tc", "qdisc", "del", "dev", name, "root"], check=False)
                log(f"{name}: QoS removed")
            del self.applied[name]
        for name, interface in sorted(desired.items()):
            if name not in present:
                self.applied.pop(name, None)
                if name not in self.missing:
                    log(f"{name}: interface not present, QoS applied when it appears")
                    self.missing.add(name)
                continue
            self.missing.discard(name)
            key = (json.dumps(interface, sort_keys=True), present[name])
            if self.applied.get(name) == key:
                continue
            commands = build(interface)  # validate before touching the interface
            self.applied.pop(name, None)
            run(["tc", "qdisc", "del", "dev", name, "root"], check=False)
            for command in commands:
                run(command)
            self.applied[name] = key
            log(f"{name}: {interface['qdisc']} applied")

    def state(self, config: dict[str, Any]) -> dict[str, Any]:
        present = ifindexes()
        entries = []
        for interface in _interfaces(config):
            name = interface["name"]
            if name not in present:
                entries.append(interface_state(interface, None, None))
                continue
            entries.append(
                interface_state(
                    interface, tc_json("qdisc", "show", "dev", name),
                    tc_json("class", "show", "dev", name),
                )
            )  # fmt: skip
        return {"qos": {"interface": entries}} if entries else {}


def main() -> int:
    return serve(QosBackend())


if __name__ == "__main__":
    sys.exit(main())
