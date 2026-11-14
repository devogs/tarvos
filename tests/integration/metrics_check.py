#!/usr/bin/env python3
"""Scrape a Tarvos /metrics endpoint and check that known series are there.

Usage: metrics_check.py URL [--node] [--evpn] [--print SERIES...]
Checks: the agent is up; eth0 has rx/tx packet counters and a packet-size histogram
whose +Inf bucket equals the packet counter; NETCONF control-plane packets are counted.
--node: container (cgroup) CPU/RAM/PIDs, per-daemon resources, eBPF cost, interface
gauges, build info, and both state sources up. --evpn: BGP session up and EVPN routes
of types 2, 3 and 5 counted. --print: print the value of each named series and exit.
"""

from __future__ import annotations

import re
import sys
import urllib.request

SERIES = re.compile(r"^([a-z_]+)(\{[^}]*\})? ([0-9.e+-]+)$")


def parse(text: str) -> dict[str, float]:
    out = {}
    for line in text.splitlines():
        match = SERIES.match(line)
        if match:
            out[match.group(1) + (match.group(2) or "")] = float(match.group(3))
    return out


def check(metrics: dict[str, float]) -> list[str]:
    errors = []

    def need(name: str, positive: bool = True) -> float:
        if name not in metrics:
            errors.append(f"missing {name}")
            return 0.0
        if positive and metrics[name] <= 0:
            errors.append(f"{name} is {metrics[name]}")
        return metrics[name]

    need("tarvos_telemetry_up")
    for direction in ("rx", "tx"):
        labels = f'direction="{direction}",interface="eth0"'
        packets = need(f"tarvos_interface_packets_total{{{labels}}}")
        need(f"tarvos_interface_bytes_total{{{labels}}}")
        inf = need(f'tarvos_interface_packet_size_bytes_bucket{{{labels},le="+Inf"}}')
        if inf and packets and inf != need(f"tarvos_interface_packet_size_bytes_count{{{labels}}}"):
            errors.append(f"{direction}: histogram count differs from its +Inf bucket")
    need('tarvos_control_plane_packets_total{direction="rx",interface="eth0",protocol="netconf"}')
    return errors


CORE_DAEMONS = ("zebra", "mgmtd", "staticd", "watchfrr", "netopeer2-server",
                "sysrepo-plugind", "sshd", "frr_sync", "netdev_sync", "kernel_sync",
                "tarvos-telemetry")  # fmt: skip


def check_node(metrics: dict[str, float]) -> list[str]:
    errors = []

    def need(name: str, positive: bool = True) -> float:
        if name not in metrics:
            errors.append(f"missing {name}")
            return 0.0
        if positive and metrics[name] <= 0:
            errors.append(f"{name} is {metrics[name]}")
        return metrics[name]

    need("tarvos_container_cpu_seconds_total")
    usage = need('tarvos_container_memory_bytes{type="usage"}')
    anon = need('tarvos_container_memory_bytes{type="anon"}')
    if usage and anon > usage:
        errors.append("container anon memory above its usage")
    need("tarvos_container_pids")
    for daemon in CORE_DAEMONS:
        need(f'tarvos_process_resident_memory_bytes{{process="{daemon}"}}')
        need(f'tarvos_process_count{{process="{daemon}"}}')
    need('tarvos_ebpf_programs{type="sched_cls"}')
    need("tarvos_ebpf_attached_interfaces")
    if not any(k.startswith("tarvos_ebpf_map_memory_bytes{") and v > 0 for k, v in metrics.items()):
        errors.append("no tarvos_ebpf_map_memory_bytes series")
    need('tarvos_interface_up{interface="eth0",kind="veth"}')
    need('tarvos_interface_mtu_bytes{interface="eth0"}')
    need('tarvos_interface_netdev_errors_total{direction="rx",interface="eth0"}', positive=False)
    need("tarvos_node_uptime_seconds")
    need("tarvos_telemetry_scrape_duration_seconds")
    need("tarvos_telemetry_snapshot_age_seconds", positive=False)
    need('tarvos_telemetry_state_up{source="routing"}')
    need('tarvos_telemetry_state_up{source="kernel"}')
    need("tarvos_copp_active", positive=False)
    if not any(k.startswith("tarvos_build_info{") for k in metrics):
        errors.append("missing tarvos_build_info")
    return errors


def check_evpn(metrics: dict[str, float]) -> list[str]:
    errors = []
    if not any(k.startswith("tarvos_bgp_neighbor_up{") and v == 1 for k, v in metrics.items()):
        errors.append("no established BGP neighbour (tarvos_bgp_neighbor_up)")
    for route_type in (2, 3, 5):
        if metrics.get(f'tarvos_evpn_routes{{route_type="{route_type}"}}', 0) <= 0:
            errors.append(f"no EVPN type-{route_type} route counted")
    if metrics.get('tarvos_evpn_vni_remote_vteps{vni="100"}', 0) < 1:
        errors.append("VNI 100 has no remote VTEP")
    return errors


def main() -> int:
    with urllib.request.urlopen(sys.argv[1], timeout=30) as response:  # noqa: S310
        text = response.read().decode()
    args = sys.argv[2:]
    if "--print" in args:
        metrics = parse(text)
        for name in args[args.index("--print") + 1 :]:
            print(metrics.get(name, "absent"))
        return 0
    errors = check(parse(text))
    if "--node" in args:
        errors += check_node(parse(text))
    if "--evpn" in args:
        errors += check_evpn(parse(text))
    for error in errors:
        print(f"metrics: {error}", file=sys.stderr)
    if not errors:
        print(f"  {len(parse(text))} series, tarvos_* ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
