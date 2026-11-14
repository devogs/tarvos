#!/usr/bin/env python3
"""Check a `tarvos-telemetry snapshot` (JSON) in the integration suites.

Usage: telemetry_check.py smoke|evpn SNAPSHOT_JSON
  smoke: eth0 instrumented with traffic, histogram consistent, SSH/NETCONF classified
  evpn:  leaf1 of evpn.sh after the pings: VNI 100 and 5000 carried traffic both ways,
         BGP and VXLAN packets counted on the fabric port eth1
"""

from __future__ import annotations

import json
import sys


def smoke(snap: dict) -> str:
    eth0 = next(i for i in snap["interfaces"] if i["name"] == "eth0")
    assert eth0["instrumented"], eth0
    assert eth0["rx"]["packets"] > 0 and eth0["tx"]["bytes"] > 0, eth0
    assert sum(eth0["rx"]["sizes"]) == eth0["rx"]["packets"], eth0
    seen = {
        (c["protocol"], c["direction"]) for c in snap["control_plane"] if c["interface"] == "eth0"
    }
    assert {("ssh", "rx"), ("netconf", "rx"), ("netconf", "tx")} <= seen, seen
    return (
        f"attach={snap['attach']} drop_tracing={snap['drop_tracing']} "
        f"control-plane={sorted(seen)}"
    )


def evpn(snap: dict) -> str:
    vnis = {v["vni"]: v for v in snap["vnis"]}
    for vni in (100, 5000):  # L2 stretch (h1 -> h2) and symmetric IRB (h1 -> 10.10.2.1)
        assert vni in vnis, vnis
        assert vnis[vni]["rx"]["packets"] > 0 and vnis[vni]["tx"]["packets"] > 0, vnis[vni]
    cp = {
        (c["interface"], c["protocol"], c["direction"]): c["packets"]
        for c in snap["control_plane"]
    }
    for key in (("eth1", "bgp", "rx"), ("eth1", "bgp", "tx"), ("eth1", "vxlan", "rx"),
                ("eth1", "vxlan", "tx")):  # fmt: skip
        assert cp.get(key, 0) > 0, (key, cp)
    return (
        f"VNI 100 rx/tx {vnis[100]['rx']['packets']}/{vnis[100]['tx']['packets']}, "
        f"VNI 5000 rx/tx {vnis[5000]['rx']['packets']}/{vnis[5000]['tx']['packets']}, "
        f"eth1 bgp rx {cp[('eth1', 'bgp', 'rx')]}, vxlan rx {cp[('eth1', 'vxlan', 'rx')]}"
    )


def main() -> int:
    mode, raw = sys.argv[1], sys.argv[2]
    try:
        print("  " + {"smoke": smoke, "evpn": evpn}[mode](json.loads(raw)))
    except (AssertionError, StopIteration, KeyError) as exc:
        print(f"telemetry check failed: {exc!r}", file=sys.stderr)
        print(raw[:4000], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
