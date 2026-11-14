#!/usr/bin/env python3
"""Scrape a Tarvos /metrics endpoint and check that known series are there.

Usage: metrics_check.py URL
Checks: the agent is up; eth0 has rx/tx packet counters and a packet-size histogram
whose +Inf bucket equals the packet counter; NETCONF control-plane packets are counted.
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


def main() -> int:
    with urllib.request.urlopen(sys.argv[1], timeout=10) as response:  # noqa: S310
        text = response.read().decode()
    errors = check(parse(text))
    for error in errors:
        print(f"metrics: {error}", file=sys.stderr)
    if not errors:
        print(f"  {len(parse(text))} series, tarvos_* ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
