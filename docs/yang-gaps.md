# YANG gap audit (Tarvos 2.1.1)

The Tarvos models were compared with what an operator configures with FRR and iproute2 on a real EVPN fabric. Every gap gets one of these decisions:

- **Added**: modelled and applied in 2.1.1, with a unit test (rendering identical to FRR's `show running-config`) and a test on a live node (`tests/day2/`, case `bgp_gap_leaves`).
- **Present**: already in the model; listed because the audit asked for it.
- **FRR model**: configure it through FRR's own YANG (mgmtd path), which Tarvos serves as is.
- **Backlog**: not modelled yet. The model has no leaf for it, so nothing is accepted and silently ignored. These are candidates for a later release.
- **By design**: fixed on purpose, with the reason.

Rule kept by CI: no configuration leaf may be accepted and then ignored. `tests/test_yang_coverage.py` fails when a configuration leaf of a Tarvos module is never read by its backend. That was the v2.0.0 problem, where QoS, CoPP and AAA were model-only.

## tarvos-bgp

| Gap | Decision | Notes |
|---|---|---|
| Peer groups | Present | `peer-group` list, `neighbor/peer-group` |
| Dynamic neighbours (listen ranges) | **Added** | `listen-range[prefix]/peer-group`, `listen-limit` |
| Route-reflector cluster ID | **Added** | `cluster-id`; FRR resets the sessions when it changes (measured: back in 2 s) |
| Graceful restart | **Added** | `graceful-restart`; FRR applies it to sessions established afterwards |
| `maximum-paths` per address family | Present | `ipv4-unicast`/`ipv6-unicast` `maximum-paths`, `maximum-paths-ibgp` |
| Best-path options | Present (part) | `bestpath-multipath-relax`. MED and router-ID comparisons are backlog |
| `next-hop-unchanged` | Backlog | In FRR 10.7, `attribute-unchanged next-hop` under `address-family l2vpn evpn` is accepted but never appears in `show running-config`, so Tarvos could not verify it. EVPN keeps the originating VTEP as next hop across a route reflector anyway |
| `local-as` | **Added** | `neighbor/local-as` (`asn`, `no-prepend`, `replace-as`) |
| `update-source` interface | Present | `update-source` takes an address or an interface name |
| Timers per neighbour | Present | `neighbor/timers` |
| `remove-private-as` | **Added** | per neighbour and address family, refused for `l2vpn-evpn` |
| `as-override` | **Added** | per neighbour and address family, refused for `l2vpn-evpn` |
| Communities sent | Backlog | FRR sends standard, extended and large communities by default; turning that off is not modelled |
| Soft reconfiguration | Present | `soft-reconfiguration-inbound` |
| `bgp max-med` | Backlog | |
| Confederations | Backlog | rare in EVPN fabrics |

## EVPN (tarvos-bgp `l2vpn-evpn`, tarvos-vxlan)

| Gap | Decision | Notes |
|---|---|---|
| ESI multihoming (route types 1 and 4) | Backlog | Types 1 and 4 are always 0 because multihoming is not supported: it needs bonds, ESIs and `evpn mh` settings. Single-homed leaves only |
| Per-VNI RD/RT | Present | `l2vpn-evpn/vni[vni]` `rd`, `route-target-import`, `route-target-export` |
| `advertise-svi-ip`, `advertise-default-gw` | Present | global and per VNI |
| Duplicate address detection | Backlog | FRR's default (on, 5 moves in 180 s) applies |
| ARP/ND suppression | Present | `l2vni/neighbor-suppression` (default on) |
| VNI filters | Backlog | |
| MAC-VRF ageing | Backlog | kernel bridge default (300 s) |
| Route-target auto | By design | explicit route targets override the automatic ones; nothing to disable |

## tarvos-vxlan

| Gap | Decision | Notes |
|---|---|---|
| Static flood lists (ingress replication without EVPN) | Backlog | the flood list comes from type-3 routes |
| MTU | Present | default changed to 1450 in 2.1.1 (it black-holed packets over 1450 bytes) |
| UDP port | Present | `udp-port` |
| `learning` | By design | always off: the EVPN control plane fills the FDB |
| Per-VNI ageing | Backlog | |
| VLAN-aware bridge (VLAN-to-VNI) | Backlog | one bridge per VNI today |
| IPv6 VTEP source | Backlog | `source-address` is IPv4. IPv6 BGP transport works (unnumbered, extended next hop) |
| Tunnel TTL/DSCP | Backlog | kernel defaults |

## tarvos-vrf

| Gap | Decision | Notes |
|---|---|---|
| Table auto-allocation | Backlog | `table` is mandatory |
| Route leaking between VRFs | FRR model | static routes with `nexthop-vrf` (`frr-routing` + `frr-staticd`, mgmtd path), or `import-vrf` in `tarvos-bgp` |

## tarvos-qos

| Gap | Decision | Notes |
|---|---|---|
| Class ids | Fixed | id 1 works since 2.1.1 (the root class moved to `1:ffff`) |
| `cake` options | Backlog | bandwidth only |
| `ceil` default | Present | defaults to the interface bandwidth |
| Per-class burst | Backlog | |
| Ingress policing | Backlog | |
| DSCP re-marking | Backlog | |

## tarvos-nftables (CoPP)

| Gap | Decision | Notes |
|---|---|---|
| Match on destination address, interface, TCP flags | Backlog | protocol, destination port and source prefix today |
| Counters per source | Backlog | |
| Named sets | Backlog | |
| Explicit rule order | Present | `rule` is `ordered-by user`: the list order is the evaluation order |

## State

| Gap | Decision | Notes |
|---|---|---|
| OSPF/IS-IS adjacencies | Backlog | use `vtysh` (`show ip ospf neighbor`, `show isis neighbor`) |
| Interface errors and oper-status | **Added** | Prometheus `tarvos_interface_up`, `_netdev_errors_total`, `_netdev_dropped_total`. gNMI `/interfaces/interface/state/oper-status` was already there |
| BGP per-prefix path attributes | Present (part) | `/bgp/state/evpn/route`: best path, next hops, RTs, AS path. IPv4/IPv6 unicast routes are not modelled (backlog) |
| NETCONF session list | Present | `ietf-netconf-monitoring` (`/netconf-state/sessions`), served by Netopeer2 |
| System info (version, uptime, process health) | **Added** | `/telemetry/container` (uptime, per-daemon resources), `/telemetry/agent/version`, Prometheus `tarvos_build_info`, `tarvos_node_uptime_seconds` |
| Time synchronisation | By design | a container uses the host clock |
