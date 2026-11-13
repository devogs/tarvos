#!/usr/bin/env python3
"""Sysrepo backend for the tarvos-lldp YANG module.

* Applies the running configuration to lldpd through ``lldpcli`` (at start and after every
  committed change). Leaves that are not configured keep their LLDP_* environment value.
* Serves the discovered neighbours as operational data.

Started by the entrypoint when lldpd runs. Everything that touches Sysrepo is imported
lazily so the pure functions below can be unit tested.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.environ.get("SCRIPTS_DIR", "/opt/tarvos/scripts"))

from lldp_configure import LldpSettings, load_settings  # noqa: E402
from tarvos_common import log as _log  # noqa: E402

MODULE = "tarvos-lldp"
LLDPCLI = "lldpcli"
CAPABILITIES = {"other", "repeater", "bridge", "wlan", "router", "telephone", "docsis", "station"}
# YANG admin-status -> lldpcli keyword
PORT_STATUS = {
    "tx-and-rx": "rx-and-tx",
    "tx-only": "tx-only",
    "rx-only": "rx-only",
    "disabled": "disabled",
}


def log(message: str) -> None:
    _log("lldp-sync", message)


@dataclass(frozen=True)
class Desired:
    settings: LldpSettings
    paused: bool = False
    ports: dict[str, str] = field(default_factory=dict)  # interface -> YANG admin-status


def desired_state(base: LldpSettings, config: dict[str, Any] | None) -> Desired:
    """Overlay the /lldp running configuration on the environment-derived settings."""
    config = config or {}
    settings = LldpSettings(
        enabled=True,
        interfaces=config.get("interface-pattern", base.interfaces),
        tx_interval=int(config.get("tx-interval", base.tx_interval)),
        tx_hold=int(config.get("tx-hold", base.tx_hold)),
        system_name=config.get("system-name", base.system_name),
        description=config.get("system-description", base.description),
    )
    ports = {
        entry["name"]: entry.get("admin-status", "tx-and-rx") for entry in config.get("interface", [])
    }
    return Desired(settings, paused=config.get("enabled", True) is False, ports=ports)


def build_commands(desired: Desired, previous_ports: set[str]) -> list[list[str]]:
    """lldpcli argument lists that bring lldpd to ``desired`` (idempotent)."""
    s = desired.settings
    commands = [
        ["configure", "system", "hostname", s.system_name],
        ["configure", "system", "description", s.description],
        ["configure", "system", "interface", "pattern", s.interfaces],
        ["configure", "lldp", "tx-interval", str(s.tx_interval)],
        ["configure", "lldp", "tx-hold", str(s.tx_hold)],
    ]
    # Interfaces dropped from the configuration go back to the default status.
    for name in sorted(previous_ports - desired.ports.keys()):
        commands.append(["configure", "ports", name, "lldp", "status", "rx-and-tx"])
    for name, status in sorted(desired.ports.items()):
        commands.append(["configure", "ports", name, "lldp", "status", PORT_STATUS[status]])
    commands.append(["pause" if desired.paused else "resume"])
    return commands


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_neighbors(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert ``lldpcli -f json show neighbors details`` to tarvos-lldp neighbor entries.

    lldpd emits a single interface as an object and several as a list of objects, and keys
    the chassis by system name (or inlines it when the neighbour sent none).
    """
    neighbors: list[dict[str, Any]] = []
    interfaces = (data.get("lldp") or {}).get("interface")
    for group in _as_list(interfaces):
        for ifname, info in group.items():
            for chassis_entry in _as_list(info.get("chassis")):
                if isinstance(chassis_entry.get("id"), dict) and "type" in chassis_entry["id"]:
                    sysname, chassis = None, chassis_entry
                else:
                    ((sysname, chassis),) = chassis_entry.items()
                port = info.get("port") or {}
                chassis_id = str((chassis.get("id") or {}).get("value", ""))
                port_id = str((port.get("id") or {}).get("value", ""))
                if not chassis_id or not port_id:
                    continue
                entry: dict[str, Any] = {
                    "interface": ifname,
                    "chassis-id": chassis_id,
                    "port-id": port_id,
                }
                if port.get("descr"):
                    entry["port-description"] = port["descr"]
                if sysname:
                    entry["system-name"] = sysname
                if chassis.get("descr"):
                    entry["system-description"] = chassis["descr"]
                if chassis.get("mgmt-ip"):
                    entry["management-address"] = [str(a) for a in _as_list(chassis["mgmt-ip"])]
                capabilities = [
                    c["type"].lower()
                    for c in _as_list(chassis.get("capability"))
                    if c.get("enabled") and c.get("type", "").lower() in CAPABILITIES
                ]
                if capabilities:
                    entry["capability"] = capabilities
                if str(port.get("ttl", "")).isdigit():
                    entry["ttl"] = int(port["ttl"])
                neighbors.append(entry)
    return neighbors


def lldpcli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [LLDPCLI, *args], capture_output=True, text=True, timeout=10, check=False
    )


def wait_for_lldpd(timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lldpcli("show", "configuration").returncode == 0:
            return True
        time.sleep(1)
    return False


class Agent:
    def __init__(self, session: Any) -> None:
        self.session = session
        self.base = load_settings(dict(os.environ))
        self.applied_ports: set[str] = set()
        self.lock = threading.Lock()

    def running_config(self) -> dict[str, Any]:
        data = self.session.get_data(f"/{MODULE}:lldp")
        return data.get("lldp", {})

    def apply(self) -> None:
        with self.lock:
            desired = desired_state(self.base, self.running_config())
            for command in build_commands(desired, self.applied_ports):
                result = lldpcli(*command)
                if result.returncode != 0:
                    log(f"WARNING: lldpcli {' '.join(command)}: {result.stderr.strip()}")
            self.applied_ports = set(desired.ports)
            log("configuration applied")

    def on_change(self, event: str, req_id: int, changes: Any, private_data: Any) -> None:
        if event == "done":
            try:
                self.apply()
            except Exception as exc:  # callbacks must not raise into the C stack
                log(f"ERROR: applying configuration: {exc}")

    def on_neighbors(self, xpath: str, private_data: Any) -> dict[str, Any]:
        try:
            result = lldpcli("-f", "json", "show", "neighbors", "details")
            neighbors = parse_neighbors(json.loads(result.stdout or "{}"))
        except Exception as exc:
            log(f"ERROR: reading neighbours: {exc}")
            neighbors = []
        return {"lldp": {"neighbors": {"neighbor": neighbors}}} if neighbors else {}


def main() -> int:
    from sysrepo import SysrepoConnection, SysrepoError

    if not wait_for_lldpd():
        log("FATAL: lldpd is not reachable")
        return 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with SysrepoConnection() as connection, connection.start_session("running") as session:
        agent = Agent(session)
        try:
            session.get_data(f"/{MODULE}:lldp")
        except SysrepoError as exc:  # the YANG directory is not mounted
            log(f"{MODULE} is not available ({exc}); runtime LLDP management disabled")
            return 0
        agent.apply()
        session.subscribe_module_change(MODULE, None, agent.on_change, done_only=True)
        session.subscribe_oper_data_request(
            MODULE, f"/{MODULE}:lldp/neighbors", agent.on_neighbors
        )
        log("serving tarvos-lldp")
        stop.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
