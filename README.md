<p align="center">
  <img src="images/tarvos_project.jpeg" alt="Tarvos - containerized network OS, eBPF telemetry, BGP EVPN" width="100%">
</p>

<h1 align="center">Tarvos Network OS</h1>

<p align="center">
  <a href="https://github.com/devogs/tarvos/actions/workflows/ci-cd.yml"><img alt="CI/CD" src="https://github.com/devogs/tarvos/actions/workflows/ci-cd.yml/badge.svg?branch=main"></a>
  <a href="https://hub.docker.com/r/matt13290/tarvos"><img alt="Docker image" src="https://img.shields.io/docker/v/matt13290/tarvos?sort=semver&label=docker%20hub&logo=docker"></a>
  <a href="https://hub.docker.com/r/matt13290/tarvos"><img alt="Image size" src="https://img.shields.io/docker/image-size/matt13290/tarvos/latest"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/devogs/tarvos"></a>
</p>

Lightweight, open-source Network OS designed to bring carrier-grade BGP EVPN routing, SDN orchestration, and real-time kernel telemetry to any containerized network lab. Built to replace heavy proprietary virtual routers with a fast, standards-driven appliance.

Debian trixie-slim + FRRouting 10.7.1 (official packages, mgmtd) + Sysrepo/Netopeer2 (NETCONF) + OpenSSH, with TACACS+ AAA, rsyslog and the management interface in its own VRF.

## Highlights

- **Routing:** FRRouting 10.7.1 (BGP, MP-BGP, OSPFv2/v3, IS-IS, BFD, RIP, static, PIM, ...).
- **BGP EVPN over VXLAN:** type-2/type-3 (L2VNI, ARP/ND suppression, anycast gateway) and type-5 with symmetric IRB (L3VNI), kernel data plane included. Tested end to end with two leaves configured only over NETCONF.
- **gNMI:** Capabilities, Get, and Subscribe (SAMPLE, ON_CHANGE) for OpenConfig interface counters, eBPF telemetry and every YANG model's state (BGP/EVPN included), authenticated like NETCONF (admin, TACACS+).
- **Everything over NETCONF:** FRR's own YANG models where FRR implements them (through mgmtd, as transactions), Tarvos YANG for the daemons FRR does not model yet, rendered to vtysh. A configuration FRR rejects is refused over NETCONF with FRR's error.
- **AAA:** TACACS+ with ordered failover and a local break-glass `admin`.
- **Discovery:** LLDP (lldpd) advertises the node. Manage it and read discovered neighbours over NETCONF with the `tarvos-lldp` module, or use `lldpcli show neighbors`.
- **Isolated management plane:** `eth0` lives in VRF `mgmt` by default.
- **Troubleshooting toolbox:** `ping`, `traceroute`, `mtr`, `tracepath`, `dig`, `tcpdump`, `curl`, `nc`, `iperf3` and more, usable in any VRF with `vrfexec <vrf> <command>`, as root or as an SSH user.
- **Observability:** operational state over NETCONF for every model (BGP sessions, EVPN VNIs and routes, VRF/VXLAN devices, PIM/LDP/VRRP/BFD sessions, tc and CoPP counters, TACACS+ reachability), rsyslog to stdout with optional forwarding to remote collectors.
- **eBPF telemetry:** per-interface traffic, packet sizes and drops by reason, per-VNI VXLAN counters and control-plane packet counters, collected in the kernel and exported to Prometheus (`/metrics`, port 9273).
- **Per-node metrics:** the container's own CPU, memory and PIDs (from its cgroup, never the host's), each daemon's resources, the eBPF cost, BGP/EVPN health and QoS/CoPP/AAA counters on the same endpoint, with a Grafana dashboard.
- **QoS and CoPP over NETCONF:** tc shaping and DSCP classes (`tarvos-qos`), nftables control-plane policing (`tarvos-nftables`).
- **Hardened (2.1.1):** a day-2 campaign and a chaos suite run against a committed lab; see [Day-2 operations and resilience](#day-2-operations-and-resilience).
- **Small and fast:** about 100 MB compressed, boots in under 10 s, about 115 MiB of RAM with bgpd and telemetry (FRR 10.7 always runs mgmtd, zebra and staticd).

## Get the image

```bash
docker pull matt13290/tarvos:latest      # or a pinned release, e.g. :v2.0.0
```

## Quick start

```bash
cp .env.example .env     # edit it, see below
docker compose up --build
ssh -p 2222 admin@localhost          # password: ADMIN_PASSWORD (default "admin")
ssh -p 8300 -s admin@localhost netconf
```

Ports 2222 and 8300 are the host ports `docker-compose.yml` maps to the node's SSH (22) and NETCONF (830). A node run directly on a network, as in GNS3 or containerlab, listens on 22, 830, 9273 (Prometheus) and 57400 (gNMI) of its management address: `ssh admin@<node>`, `ssh -p 830 -s admin@<node> netconf`.

The container needs `NET_ADMIN`, `NET_RAW` and `SYS_ADMIN` (FRR's zebra/bgpd require the last for VRF/netns handling); see `docker-compose.yml`.

The `tarvos-*` YANG modules are built into the image (`/opt/tarvos/yangs-builtin`), so no volume is needed. Custom modules mounted at `/opt/tarvos/yangs` are registered as well (and override a built-in of the same name). Registration happens at every boot, before Netopeer2 starts.

## Configuration (environment, see `.env.example`)

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Password of the local `admin` account (default `admin`, **change it**) |
| `TACACS_SERVER`, `TACACS_SECRET`, `TACACS_TIMEOUT` | TACACS+ servers (ordered, failover), shared secret |
| `SYSLOG_SERVER`, `SYSLOG_PORT`, `SYSLOG_PROTOCOL`, `SYSLOG_MIN_SEVERITY`, `SYSLOG_STDOUT` | Optional remote syslog forwarding, RFC 5424 with the PRI. Logs also go to stdout (`docker logs`) in a short readable format, unless stdout is a terminal: `SYSLOG_STDOUT` is `auto` (default, skip the stdout stream when stdout is a terminal), `yes` (always) or `no` (never) |
| `MGMT_VRF_ENABLED`, `MGMT_VRF_NAME`, `MGMT_VRF_TABLE`, `MGMT_INTERFACE` | Management VRF (default: `eth0` in VRF `mgmt`) |
| `TACACS_NETCONF_ACCESS` | NETCONF rights of TACACS+ users: `ro` (default), `rw` or `none` |
| `FRR_DAEMONS` | FRR daemons to start, e.g. `bgpd`, `bgpd,bfdd` or `[bgpd, ospfd]` (names: bgpd, ospfd, ospf6d, ripd, ripngd, isisd, pimd, pim6d, ldpd, nhrpd, eigrpd, babeld, sharpd, pbrd, bfdd, fabricd, vrrpd, pathd). mgmtd, zebra and staticd always run. NETCONF configuration for a daemon that is not running is refused |
| `LLDP_ENABLED`, `LLDP_INTERFACES`, `LLDP_TX_INTERVAL`, `LLDP_TX_HOLD`, `LLDP_SYSTEM_NAME`, `LLDP_SYSTEM_DESCRIPTION` | LLDP neighbour discovery (lldpd, on by default on every interface; `LLDP_INTERFACES` is an lldpd pattern such as `eth*,!eth0`). Inspect with `lldpcli show neighbors` or over NETCONF |
| `TELEMETRY_ENABLED`, `TELEMETRY_EXCLUDE`, `TELEMETRY_SYNC_INTERVAL`, `TELEMETRY_LISTEN_VRF` | eBPF telemetry agent, see [eBPF telemetry](#ebpf-telemetry) |
| `CONSOLE_SHELL`, `CONSOLE_SHELL_MAX_FAILURES` | `yes` (default): with a TTY (GNS3 telnet console, `docker run -it`), a login shell as `admin` is offered on the console; `no` keeps the console log-only. When the TTY cannot become a controlling terminal (containerlab), the shell is given up after `CONSOLE_SHELL_MAX_FAILURES` (3) quick failures in a row and the console stays log-only |
| `PROMETHEUS_ENABLED`, `PROMETHEUS_PORT`, `PROMETHEUS_ROUTING_METRICS`, `PROMETHEUS_KERNEL_METRICS`, `PROMETHEUS_STATE_CACHE` | `/metrics` endpoint and which series it serves, see [Prometheus](#prometheus) |
| `GNMI_ENABLED`, `GNMI_PORT`, `GNMI_TLS`, `GNMI_TLS_CERT`, `GNMI_TLS_KEY` | gNMI server, see [gNMI](#gnmi) |
| `NEIGH_REFRESH_ENABLED` | `yes` (default): keep the local hosts of every L2 VNI known by probing each STALE neighbor (one unicast ARP/ND request from the anycast gateway), see [Example: EVPN leaf](#example-evpn-leaf); `no` lets idle hosts age out |
| `NETCONF_TIMEOUT` | Seconds a NETCONF request may take, backends included (default 120) |
| `CRASH_LOOP_MAX`, `CRASH_LOOP_WINDOW` | Supervision: a critical daemon restarted more than `CRASH_LOOP_MAX` times (5) in `CRASH_LOOP_WINDOW` seconds (120) stops the container |
| `SSH_PORT`, `NETCONF_PORT`, `LOG_LEVEL`, `YANG_DIR` | Runtime parameters |

## Routing over NETCONF

FRR 10.7's `mgmtd` is a YANG transaction engine (candidate/running datastores, validation, commit), not a NETCONF server, and only some daemons are its backends. Tarvos keeps Netopeer2/Sysrepo as the NETCONF server and picks, per model, the most native path FRR offers. `tarvos-frr-sync` applies every change during the NETCONF transaction, so FRR errors become `<rpc-error>`s and nothing is half-applied.

| Path | YANG modules | How it reaches FRR |
|---|---|---|
| FRR native, mgmtd | `frr-interface`, `frr-vrf`, `frr-zebra`, `frr-routing` + `frr-staticd`, `frr-ripd`, `frr-ripngd`, `frr-affinity-map` | Pushed unchanged to mgmtd: one `mgmt edit` candidate + `mgmt commit apply` per change. State is served from mgmtd (`<get>` on these modules returns interface, VRF and RIB state) |
| FRR native, vtysh | `frr-filter` (prefix/access lists), `frr-route-map` + `frr-bgp-route-map`, `frr-bfdd` | Rendered to FRR configuration (as FRR's own YANG-to-CLI code does) and applied with `frr-reload`. Their daemons (bgpd, ospfd, bfdd, ...) are not mgmtd backends, so mgmtd could not deliver them |
| Tarvos YANG, vtysh | `tarvos-bgp` (BGP, MP-BGP, EVPN, community/AS-path lists), `tarvos-ospf` (OSPFv2 + OSPFv3), `tarvos-isis` | Same rendering path: FRR 10.7 bgpd has no YANG northbound, and ospfd/isisd are not mgmtd backends |
| Tarvos YANG, vtysh (CLI lines) | `tarvos-frr`: pimd, pim6d, ldpd, nhrpd, eigrpd, babeld, pbrd, vrrpd, fabricd | CLI blocks and interface lines per daemon, validated by vtysh and applied in the same transaction. Each daemon can only carry its own block types |
| Kernel | `tarvos-vrf` (VRF devices), `tarvos-vxlan` (VTEP, L2VNI, L3VNI) | iproute2: `RED`, `br<vni>`, `vx<vni>` devices, tagged with the alias `tarvos`; reconciled every 10 s so links attached later are picked up |

Ownership rules, so NETCONF and the CLI can coexist:

- mgmtd path: each list entry (interface, VRF, static route set, ...) configured over NETCONF is owned by NETCONF; entries never configured over NETCONF keep their CLI configuration. Removing an interface or VRF entry that exists in the kernel clears its configuration (mgmtd refuses to delete it); an entry with no kernel device is deleted.
- vtysh path: once a module is configured, Tarvos owns its block types (for example every `router bgp`, or the `ip ospf ...` lines of every interface); other configuration is left alone. `frr-reload` only sends the differences, so unchanged sessions are not reset.
- NETCONF configuration lives in the Sysrepo datastores: `<copy-config>` from running to startup makes it survive a restart of the container, and Tarvos re-applies it to FRR at boot. `/etc/frr/frr.conf` is not written.

### Example: EVPN leaf

One `<edit-config>` (excerpt, see [tests/integration/netconf_evpn.py](tests/integration/netconf_evpn.py) for a complete leaf):

```xml
<vrfs xmlns="urn:tarvos:params:xml:ns:yang:tarvos-vrf">
  <vrf><name>RED</name><table>1010</table></vrf>
</vrfs>
<vxlan xmlns="urn:tarvos:params:xml:ns:yang:tarvos-vxlan">
  <source-address>10.255.0.1</source-address>
  <l2vni><vni>100</vni><vrf>RED</vrf><access-interface>eth2</access-interface>
    <gateway-address>10.1.100.1/24</gateway-address><gateway-mac>00:00:5e:00:01:01</gateway-mac></l2vni>
  <l3vni><vni>5000</vni><vrf>RED</vrf></l3vni>
</vxlan>
<lib xmlns="http://frrouting.org/yang/vrf">
  <vrf><name>RED</name><zebra xmlns="http://frrouting.org/yang/zebra"><l3vni-id>5000</l3vni-id></zebra></vrf>
</lib>
<bgp xmlns="urn:tarvos:params:xml:ns:yang:tarvos-bgp">
  <instance><vrf>default</vrf><asn>65000</asn><default-ipv4-unicast>false</default-ipv4-unicast>
    <neighbor><address>172.30.0.12</address><remote-as>internal</remote-as>
      <address-family><name>l2vpn-evpn</name><activate>true</activate></address-family></neighbor>
    <l2vpn-evpn><advertise-all-vni>true</advertise-all-vni></l2vpn-evpn></instance>
  <instance><vrf>RED</vrf><asn>65000</asn>
    <ipv4-unicast><redistribute><protocol>connected</protocol></redistribute></ipv4-unicast>
    <l2vpn-evpn><advertise><ipv4-unicast>true</ipv4-unicast></advertise></l2vpn-evpn></instance>
</bgp>
```

`FRR_DAEMONS=bgpd` is enough for this leaf; add `bfdd`, `ospfd`, ... for the corresponding models.

Things the example takes for granted:

- **Underlay reachability.** Every VTEP address (`source-address`, a loopback) must be reachable in the default VRF, and so must the next hop of every EVPN route. Routes carry the originating VTEP as next hop; with a route reflector, it reflects them unchanged, so the leaves need routes to each other's loopbacks, not only to the spine. Advertise the loopbacks (and, if the leaves only peer with the spine, the link subnets) in the underlay (`redistribute connected` with a prefix list, OSPF, IS-IS), or set `next-hop-self` where it applies. Without it, EVPN routes are received and listed in `/bgp/state/evpn/route`, but nothing is forwarded: check `vrfexec default ping <remote VTEP>`.
- **MTU.** VXLAN over IPv4 adds 50 bytes. `vx<vni>`/`br<vni>` default to 1450 (`tarvos-vxlan` `mtu`), which fits a 1500-byte underlay; with a jumbo underlay, set `mtu` to the underlay MTU minus 50. Give the hosts the same MTU: a bridged frame larger than the overlay MTU is dropped without any error (routed packets get an ICMP "fragmentation needed").
- **Type-2 MAC+IP routes.** A leaf learns a host's IP address from the ARP/ND exchanges of its anycast gateway. Until a host talks to its gateway (or is resolved by it), it is advertised as a MAC-only type-2 route; two hosts that only talk to each other at layer 2 never produce MAC+IP routes. This is FRR's behaviour, and the NETCONF state reports it as is. Once known, a host stays known while it answers: Tarvos probes every STALE local neighbor of the L2 VNIs (`NEIGH_REFRESH_ENABLED`). Without that, an idle host ages out, its IP is withdrawn, and a remote leaf cannot resolve it any more (its ARP reply is addressed to the anycast gateway MAC, which the host's own leaf consumes), so the host becomes unreachable from the other leaves until it talks again.

## Operational state over NETCONF

`<get>` (or `<get-data>` on the operational datastore) returns live state next to the configuration, so a controller can verify a fabric without `vtysh`. State is read from FRR (`show ... json`) and the kernel on every request; a filtered `<get>` only runs the commands its subtree needs.

| Node (read-only) | Content | Source |
|---|---|---|
| `/tarvos-bgp:bgp/state/instance[vrf]` | AS, router ID, L3VNI and router MAC, neighbour counts; per neighbour: session state, uptime (seconds), TCP endpoints, negotiated timers, message counters, last reset reason, prefixes received/sent per address family | `show bgp vrfs json`, `show bgp vrf all neighbors json` |
| `/tarvos-bgp:bgp/state/evpn/vni[vni]` | L2/L3 type, VXLAN device, bridge, tenant VRF, local and remote VTEPs, MAC and ARP/ND counts; L3VNI status, router MAC and L2VNIs | `show evpn vni detail json` (zebra; also without bgpd) |
| `/tarvos-bgp:bgp/state/evpn/route-count[route-type]` | Number of EVPN routes of each type 1-5 | `show bgp l2vpn evpn json` |
| `/tarvos-bgp:bgp/state/evpn/route[route-distinguisher prefix]` | Every EVPN route: type, MAC, IP, prefix length, path count; best path source/peer, next hops, route targets, router MAC, AS path | `show bgp l2vpn evpn json` |
| `/tarvos-vrf:vrfs/vrf[name]/state` | Device status (`up`, `down`, `not-present`), routing table, enslaved interfaces | `ip -j -d link` |
| `/tarvos-vxlan:vxlan/l2vni[vni]/state`, `l3vni[vni]/state` | VXLAN device status, bridge, local VTEP, remote VTEPs and remote MAC count from the VXLAN FDB, bridge ports | `ip -j -d link`, `bridge -j fdb` |
| `/tarvos-frr:frr/state/pim/neighbor`, `ldp/neighbor`, `vrrp/router`, `bfd/peer` | PIM neighbours (uptime, holdtime, DR priority); LDP sessions (state, transport address, uptime); VRRP routers (master/backup per address family, virtual addresses, advertisement counters); BFD sessions (status, uptime, diagnostics, intervals) | `show ip pim neighbor`, `show mpls ldp neighbor`, `show vrrp`, `show bfd peers` (each only while its daemon runs) |
| `/tarvos-qos:qos/interface[name]/state`, `class[id]/state` | Root qdisc kind and counters (packets, bytes, drops, overlimits, requeues, backlog), htb default class, per-class counters | `tc -s -j qdisc/class show` |
| `/tarvos-nftables:nftables/copp/state`, `rule[name]/state` | CoPP table installed, rule count; per rule matched and dropped packets/bytes | `nft -j list table inet tarvos` |
| `/tarvos-aaa:aaa/state` | TACACS+ in use and where the servers come from (`environment`/`netconf`), local fallback, per-server reachability (TCP probe of port 49) and response time, the server answering next, latest TACACS+ and local authentication (user, service, client, result, time) | probes every `AAA_PROBE_INTERVAL` s; PAM records each outcome |
| `/tarvos-lldp:lldp/neighbors` | LLDP neighbours (see below) | lldpd |
| `frr-interface`, `frr-vrf`, `frr-zebra`, RIB, RIP | FRR's own state | mgmtd |

Example: the EVPN view of a leaf.

```xml
<get><filter type="subtree">
  <bgp xmlns="urn:tarvos:params:xml:ns:yang:tarvos-bgp"><state>
    <instance><vrf>default</vrf><neighbor/></instance>
    <evpn><vni/><route-count/></evpn>
  </state></bgp>
</filter></get>
```

Units: every numeric state leaf carries a YANG `units` statement, repeated in its description (durations in seconds unless stated, rates in kbit/s, counters in packets or bytes).

`tests/integration/netconf_state.py` is a small controller-side check that verifies sessions, VNIs and type-2/type-3/type-5 routes from these trees only.

## Kernel models: QoS, CoPP, runtime AAA

One backend process (`kernel_sync`) applies `tarvos-qos`, `tarvos-nftables` and `tarvos-aaa` during the NETCONF transaction: a configuration the kernel or the backend rejects comes back as an `<rpc-error>` and nothing is applied.

- **`tarvos-qos` (tc):** Tarvos owns the root qdisc of every interface it lists and rebuilds it when the policy changes or the interface reappears. `htb` shapes to `bandwidth` with one htb class per configured class (`1:<id in hex>`, fq_codel leaf); unclassified traffic goes to a default class `1:fff` that gets the leftover bandwidth. `prio` uses eight bands (class priority 0-7 -> band 1-8). DSCP values are matched on the IPv4 DS field and the IPv6 traffic class. Over-subscribed rates, a rate above its ceil and a DSCP in two classes are refused. Removing an interface restores the kernel default qdisc. Interfaces that do not exist yet are applied when they appear (re-checked every `QOS_REAPPLY_INTERVAL` s).
- **`tarvos-nftables` (CoPP):** rules go into a table owned by Tarvos, `inet tarvos` (chain `copp`, input hook, policy accept), replaced as a whole in one `nft` transaction (counters restart at zero). A `rate-limit` rule drops the excess over `max-pps` and accepts the rest. Other nftables tables are never touched.
- **`tarvos-aaa` (PAM):** servers configured over NETCONF replace `TACACS_SERVER`/`TACACS_SECRET` (each server with its own secret) for the next SSH/NETCONF login; removing them restores the environment. `local-fallback/enabled false` rejects the `admin` login, and is itself refused while no TACACS+ server is configured.

## eBPF telemetry

`tarvos-telemetry` (Go, [cilium/ebpf](https://github.com/cilium/ebpf); eBPF programs in `telemetry/bpf/`, GPL) is the single owner of the eBPF programs and maps. It instruments every interface of the container (TCX, or a `clsact` qdisc on kernels older than 6.6) and re-checks for new interfaces every `TELEMETRY_SYNC_INTERVAL` seconds, since containerlab and GNS3 attach links after boot.

| Counter | Labels | Source |
|---|---|---|
| rx/tx packets and bytes, packet-size histogram (<=64, 128, 256, 512, 1024, 1518, 9216, larger) | interface, direction | tc/TCX ingress and egress |
| Drops, by kernel drop reason (`no_socket`, `netfilter_drop`, `ipv6disabled`, ...) | interface, reason | `tp_btf/kfree_skb`, filtered to the container's network namespace |
| VXLAN rx/tx packets and bytes, drops | vni (and its `vx<vni>` device) | counters of the VXLAN device |
| Control-plane packets and bytes: BGP, BFD, LDP, OSPF, IS-IS, VRRP, PIM, LLDP, VXLAN (outer), SSH, NETCONF, TACACS+, gNMI, Prometheus | interface, protocol, direction | header parsing in the tc programs |

Other components read the counters only through the agent, never from the maps. The Prometheus exporter and gNMI server run in the agent process. Everything else uses the root-only socket `/run/tarvos/telemetry.sock` (`GET /v1/snapshot`, JSON). Try `docker exec <node> tarvos-telemetry snapshot`.

Requirements: a kernel with BTF (`/sys/kernel/btf/vmlinux`, standard on current distributions) and `CAP_SYS_ADMIN`, which Tarvos already needs for FRR (or `CAP_BPF` + `CAP_PERFMON`, both listed in `docker-compose.yml`). GNS3 and containerlab run nodes privileged. Without drop tracing (no BTF or no `CAP_PERFMON`), the agent logs why and keeps the traffic counters. If it cannot load at all, the node keeps working without telemetry. `TELEMETRY_ENABLED=no` turns it off.

## Prometheus

The telemetry agent serves `/metrics` on `PROMETHEUS_PORT` (9273, `PROMETHEUS_ENABLED=yes`), in the management VRF (`TELEMETRY_LISTEN_VRF` overrides it, `none` listens in every VRF). Each scrape reads one fresh eBPF snapshot and the container's own cgroup and `/proc`. The routing and kernel-feature state is read from the same operational data as NETCONF and gNMI, cached for `PROMETHEUS_STATE_CACHE` seconds (default 10), so the three always agree.

**eBPF counters** (always on):

| Series | Labels |
|---|---|
| `tarvos_interface_packets_total`, `tarvos_interface_bytes_total` | `interface`, `direction` |
| `tarvos_interface_packet_size_bytes` (histogram, buckets 64 ... 9216) | `interface`, `direction` |
| `tarvos_interface_drops_total` (kernel drops by reason, kfree_skb) | `interface`, `reason` |
| `tarvos_vni_packets_total`, `tarvos_vni_bytes_total` | `vni`, `interface`, `direction` |
| `tarvos_vni_drops_total` | `vni`, `interface` |
| `tarvos_control_plane_packets_total`, `tarvos_control_plane_bytes_total` | `interface`, `protocol`, `direction` |
| `tarvos_telemetry_up`, `tarvos_telemetry_info` | `version`, `attach`, `drop_tracing` |
| `tarvos_telemetry_scrape_duration_seconds`, `tarvos_telemetry_snapshot_age_seconds` | |

**Node and kernel** (`PROMETHEUS_KERNEL_METRICS=yes`, default). Container values come from the container's own cgroup: `/proc/meminfo` and `/proc/stat` show the host, the same on every node, and are never used. cgroup v2 is read (also with `--cgroupns=host`, through `/proc/self/cgroup`); cgroup v1 gives CPU, usage, anon/file and PIDs.

| Series | Labels |
|---|---|
| `tarvos_container_cpu_seconds_total` | |
| `tarvos_container_memory_bytes` | `type`: `usage`, `anon`, `file`, `kernel`, `slab` |
| `tarvos_container_memory_limit_bytes` (absent when unlimited), `tarvos_container_pids` | |
| `tarvos_process_resident_memory_bytes`, `tarvos_process_cpu_seconds_total`, `tarvos_process_count` | `process`: FRR daemons, `netopeer2-server`, `sysrepo-plugind`, `sshd`, `lldpd`, `rsyslogd`, `frr_sync`, `netdev_sync`, `kernel_sync`, `lldp_sync`, `tarvos-telemetry`, `tini`, `entrypoint`, `other` |
| `tarvos_ebpf_map_memory_bytes` | `map` |
| `tarvos_ebpf_programs` | `type` |
| `tarvos_ebpf_attached_interfaces` | |
| `tarvos_ebpf_program_run_seconds_total`, `tarvos_ebpf_program_runs_total` (only when the host sets `kernel.bpf_stats_enabled=1`; Tarvos never changes it) | `program` |
| `tarvos_interface_up` | `interface`, `kind` |
| `tarvos_interface_mtu_bytes`, `tarvos_interface_speed_bits` (physical links only) | `interface` |
| `tarvos_interface_netdev_errors_total`, `tarvos_interface_netdev_dropped_total` (kernel netdev counters) | `interface`, `direction` |
| `tarvos_node_uptime_seconds` | |
| `tarvos_build_info` | `version`, `frr_version` |
| `tarvos_qos_class_packets_total`, `tarvos_qos_class_bytes_total`, `tarvos_qos_class_drops_total`, `tarvos_qos_class_overlimits_total` | `interface`, `class` (id or `default`) |
| `tarvos_qos_qdisc_packets_total`, `tarvos_qos_qdisc_bytes_total`, `tarvos_qos_qdisc_drops_total`, `tarvos_qos_qdisc_overlimits_total` | `interface`, `kind` |
| `tarvos_copp_active` | |
| `tarvos_copp_matched_packets_total`, `tarvos_copp_matched_bytes_total`, `tarvos_copp_dropped_packets_total`, `tarvos_copp_dropped_bytes_total` | `rule` |
| `tarvos_aaa_server_reachable`, `tarvos_aaa_server_response_seconds` | `server` |
| `tarvos_aaa_authentications_total` | `method` (`tacacs`, `local`), `result` |
| `tarvos_lldp_neighbors` | `interface` |

**Routing** (`PROMETHEUS_ROUTING_METRICS=yes`, default):

| Series | Labels |
|---|---|
| `tarvos_bgp_neighbors` | `vrf`, `state` (`configured`, `established`) |
| `tarvos_bgp_neighbor_up` | `vrf`, `neighbor`, `remote_as` |
| `tarvos_bgp_neighbor_uptime_seconds`, `tarvos_bgp_session_resets_total`, `tarvos_bgp_session_established_total` | `vrf`, `neighbor` |
| `tarvos_bgp_prefixes_received`, `tarvos_bgp_prefixes_sent` | `vrf`, `neighbor`, `afi_safi` |
| `tarvos_bgp_messages_total` | `vrf`, `neighbor`, `direction`, `type` (`all`, `update`, `notification`) |
| `tarvos_evpn_routes` | `route_type` (1-5) |
| `tarvos_evpn_vni_info` (value 1) | `vni`, `type`, `vrf`, `interface` |
| `tarvos_evpn_vni_macs`, `tarvos_evpn_vni_arp_nd`, `tarvos_evpn_vni_remote_vteps` | `vni` |
| `tarvos_pim_neighbors` | `interface` |
| `tarvos_ldp_neighbor_up` | `lsr_id` |
| `tarvos_vrrp_master`, `tarvos_vrrp_transitions_total` | `interface`, `vrid`, `afi` |
| `tarvos_bfd_peer_up` | `vrf`, `peer` |
| `tarvos_telemetry_state_up`, `tarvos_telemetry_state_age_seconds` | `source` (`routing`, `kernel`) |

PIM, LDP, VRRP and BFD series exist while their daemon runs. When a state source cannot be read, `tarvos_telemetry_state_up{source}` is 0 and that source's series are absent: stale values are never served as current.

**Cardinality.** Labels are bounded by the configuration (interfaces, neighbours, VNIs, rules, classes) and by fixed lists (process names, route types); there is no per-MAC, per-IP or per-route label. EVPN routes are counted per route type; the routes themselves are in NETCONF and gNMI (`/bgp/state/evpn/route`).

**Cost**, agent CPU on a BGP EVPN leaf (eBPF snapshot, cgroup, `/proc` and netlink reads each scrape; the state read every `PROMETHEUS_STATE_CACHE` seconds):

| | Idle | 1 s scrape |
|---|---|---|
| 2.1.0 (eBPF counters only) | 0.03 % | 0.37 % |
| 2.1.1, all series | 0.02 % | 0.71 % |
| 2.1.1, `PROMETHEUS_ROUTING_METRICS=no`, `PROMETHEUS_KERNEL_METRICS=no` | 0.03 % | same as 2.1.0 |

Idle cost is unchanged. With every series at a 1 s scrape the agent stays within +0.5 percentage point of 2.1.0, and refreshing the state costs the container about 0.5 % more at the default 10 s cache (one `sysrepocfg` read of about 45 ms of CPU, mostly the FRR `show ... json` calls). At a usual 15 s scrape interval the agent cost is below 0.1 %.

```yaml
# prometheus.yml
scrape_configs:
  - job_name: tarvos
    scrape_interval: 15s
    static_configs:
      - targets: ["leaf1:9273", "leaf2:9273", "spine1:9273"]
```

Example queries: `rate(tarvos_interface_bytes_total{direction="rx"}[1m]) * 8` (bit/s), `rate(tarvos_container_cpu_seconds_total[1m])` (CPU cores used by a node), `topk(5, tarvos_process_resident_memory_bytes)`, `sum by (instance) (tarvos_bgp_neighbor_up) < sum by (instance) (tarvos_bgp_neighbors{state="configured"})` (sessions down), `sum by (protocol) (rate(tarvos_control_plane_packets_total[5m]))`.

### Grafana

[`docs/grafana/tarvos.json`](docs/grafana/tarvos.json) is a dashboard built only from these series: per-node CPU and memory (container and daemons), interface throughput, BGP and EVPN health, control-plane packets, drops by reason, QoS and CoPP counters. Import it (Dashboards > New > Import) and pick the Prometheus data source; the `node` variable selects one or more nodes to compare.

## gNMI

The telemetry agent also serves gNMI on `GNMI_PORT` (57400, `GNMI_ENABLED=yes`), in the management VRF. It is read-only (`Set` returns `Unimplemented`: configure over NETCONF).

- **RPCs:**
  - `Capabilities`: every YANG module loaded in Sysrepo, plus `openconfig-interfaces` and `tarvos-telemetry`.
  - `Get`: JSON or JSON_IETF subtrees, or typed leaves with `PROTO`.
  - `Subscribe`: `ONCE`, `POLL` and `STREAM`. `SAMPLE` takes intervals of 1 s and more; `TARGET_DEFINED` means `SAMPLE` every 10 s. `ON_CHANGE` updates within 1 s for agent data and 5 s for Sysrepo data, and reports deleted paths.
- **Paths:**
  - `/interfaces/interface[name=*]/state` (OpenConfig): `admin-status`, `oper-status`, `mtu`, `ifindex`, and `counters/in-pkts`, `in-octets`, `out-pkts`, `out-octets` from the eBPF counters.
  - `/telemetry` (`tarvos-telemetry`, model in `telemetry/yang/`): per interface rx/tx with the size histogram and drops by reason, per VNI, control-plane protocols, agent status; `/telemetry/container` (the container's cgroup CPU, memory and PIDs, uptime, and `process[name]` per daemon) and `/telemetry/ebpf` (map memory, programs, attached interfaces), the same values as the Prometheus series.
  - Every other module, read from the Sysrepo operational datastore: configuration and the state described above, such as `/bgp/state` for BGP sessions and EVPN.
  - Unprefixed names are resolved when only one module defines them (`/bgp`); otherwise prefix them (`/frr-interface:lib`).
- **Authentication:** `username`/`password` metadata, checked through PAM (service `gnmi`, the same stack as NETCONF). Both `admin` and TACACS+ users can log in, and logins appear in `/tarvos-aaa:aaa/state`. `TACACS_NETCONF_ACCESS=none` denies gNMI to TACACS+ users as well.
- **TLS:** the certificate mounted at `GNMI_TLS_CERT`/`GNMI_TLS_KEY`. If none is mounted, a self-signed one is generated at boot, written to `/run/tarvos/gnmi/server.crt`, and its SHA-256 fingerprint is logged. `GNMI_TLS=no` serves plaintext (labs only).

```sh
G="gnmic -a leaf1:57400 -u admin -p admin --skip-verify"
$G capabilities
$G get --path '/bgp/state/instance[vrf=default]/neighbor[address=172.30.0.12]/session-state' --encoding proto
$G subscribe --mode once --path /bgp/state/evpn/route-count
$G subscribe --stream-mode sample --sample-interval 10s --path '/interfaces/interface[name=eth1]/state/counters'
$G subscribe --stream-mode on-change --path '/interfaces/interface[name=*]/state/oper-status'
$G get --path /telemetry/container --encoding json_ietf
```

## LLDP over NETCONF

The `tarvos-lldp` module configures lldpd at runtime and reports discovered neighbours. The `LLDP_*` variables are the boot defaults: a leaf you configure overrides its variable, and removing it restores the variable's value.

| Node | Purpose |
|---|---|
| `/lldp/enabled` | `false` pauses LLDP without losing configuration |
| `/lldp/tx-interval`, `tx-hold`, `system-name`, `system-description`, `interface-pattern` | Same meaning as the matching `LLDP_*` variables |
| `/lldp/interface[name]/admin-status` | `tx-and-rx` (default), `tx-only`, `rx-only` or `disabled` per interface |
| `/lldp/neighbors/neighbor` (read-only) | Chassis/port ID, system name and description, management addresses, capabilities, TTL, per local interface |

Settings are only applied when the container runs with `LLDP_ENABLED=yes`. Per-interface status for an interface that does not exist yet is accepted but only takes effect once the interface is present and the configuration is re-applied.

## Troubleshooting

The image carries the usual toolbox: `ping`, `tracepath`, `arping`, `traceroute`, `mtr`, `dig`/`nslookup`/`host`, `tcpdump`, `ethtool`, `nc` (OpenBSD), `curl`, `iperf3` and `conntrack`. They work from `docker exec` (root) and from an SSH session as `admin` or a TACACS+ user, with the capabilities the container already has (`NET_RAW`, `NET_ADMIN`). `tcpdump` is restricted to the `pcap` group, which `admin` and the TACACS+ account belong to.

### Pick the VRF: `vrfexec`

```bash
vrfexec                                  # list the VRFs: default, mgmt and every tenant VRF
vrfexec RED ping 10.1.100.12             # any command, its sockets bound to VRF RED
vrfexec mgmt dig @192.0.2.53 example.com
vrfexec RED                              # a shell where every command runs in RED
```

`vrfexec <vrf> <command>` works for any VRF, including a VRF created over NETCONF a minute ago, with no per-VRF setup, and completes VRF names with Tab. It binds every IPv4/IPv6 socket of the command to the VRF device (`SO_BINDTODEVICE`, through a small preload library), and fails the socket rather than letting it fall back to another VRF. `ip vrf exec` is not used: it needs root and a writable cgroup hierarchy, which a non-privileged container does not have.

Some tools also have their own option:

| Tool | Own VRF option | Notes |
|---|---|---|
| `ping` | `ping -I RED 10.1.100.12` | |
| `traceroute` | `traceroute -i RED 10.10.2.1` | `-I` (ICMP) and `-T` (TCP) need root; `mtr` and `tracepath` work for everyone |
| `curl` | `curl --interface RED http://10.10.2.1/` | |
| `iperf3` | `iperf3 -c 10.10.2.1 --bind-dev RED` | |
| `arping` | `arping -I eth2 10.1.100.12` | layer 2: name a member port, not the VRF |
| `tcpdump` | `tcpdump -i eth2`, `-i vx100`, `-i br100`, `-i RED` | capture on the VRF device sees all its routed traffic |
| `mtr`, `tracepath`, `dig`, `nslookup`, `host`, `nc` | none that selects a VRF | use `vrfexec` (`mtr --interface` does not work in a VRF) |

**Name resolution.** `dig`, `nslookup` and `host` send their queries from the VRF, so `vrfexec RED dig @10.1.100.53 app.example` asks a resolver that only VRF RED can reach. Lookups made inside glibc (`curl https://name`, `ping name`) always use the node resolver of `/etc/resolv.conf`, reached through the management network: give those tools an address, or resolve with `dig` first.

**Isolation.** Every tenant VRF table gets an `unreachable default` route at the highest metric (`proto tarvos`), so a lookup that misses in the tenant table stops there: tenant traffic never falls back to the main table or to the management VRF. The default VRF still falls back to the management table, by design (syslog, TACACS+).

### Recipes

```bash
# Anycast gateway and a remote host of the L2VNI
vrfexec RED ping -c3 10.1.100.1
vrfexec RED ping -c3 10.1.100.12

# Path across the fabric in a tenant VRF
vrfexec RED traceroute -n 10.10.2.1
vrfexec RED mtr -n -r -c 10 10.10.2.1

# MTU across the VXLAN underlay: 1500 minus 50 bytes of VXLAN overhead = 1450,
# so 1422 bytes of ICMP payload (+ 28 of headers) must pass with DF set
vrfexec RED ping -M do -s 1422 -c3 10.10.2.1
vrfexec RED tracepath -n 10.10.2.1

# BGP and VXLAN on the underlay (the outer header and the VNI are decoded)
tcpdump -ni eth1 'tcp port 179'
tcpdump -ni eth1 'udp port 4789'
tcpdump -ni vx100                       # inner frames of VNI 100

# Management plane
vrfexec mgmt nslookup example.com
vrfexec mgmt curl -sI https://example.com
```

## Day-2 operations and resilience

Two campaigns run against a repeatable lab: one spine, two leaves, two hosts, a TACACS+ server and a syslog collector, in plain Docker (`tests/lab/lab.sh`, wired like containerlab, with links attached after boot).

```bash
tests/lab/lab.sh up matt13290/tarvos:latest && tests/lab/lab.sh configure   # lab only
tests/day2/run.sh matt13290/tarvos:latest            # day-2 campaign (~25 min), --short in CI
tests/chaos/run.sh matt13290/tarvos:latest           # chaos suite (~40 min); CHAOS_SEED=<n> replays a run
```

Both need Docker, Python with `ncclient`, `sshpass` and `gnmic`, and the `vrf` and `vxlan` kernel modules. Each writes a Markdown and JSON report to `tests/day2/report/` or `tests/chaos/report/`. The chaos suite also runs nightly and on demand in GitHub Actions (workflow `chaos`).

What an operator can rely on (each point is a test of the campaigns):

- **Datastores:** `<edit-config>` merge, replace, create, delete and remove; `<copy-config>`, `<delete-config>`, `<validate>`; `candidate` with `<commit>`, `<discard-changes>` and confirmed commit (an unconfirmed commit rolls back FRR and the kernel as well); `<lock>`, with the lock of a dead session released at once.
- **Persistence:** after `<copy-config>` to startup, `docker restart` re-applies everything (FRR, VRF/VXLAN devices, QoS, CoPP, AAA, LLDP). Unsaved changes are gone after a restart, and running is then equal to startup.
- **Refusals are clean:** an edit refused by FRR or a backend changes nothing, and its `<rpc-error>` carries FRR's message (for example "BGP instance is already running; AS is 65000"). A VRF that VNIs still use cannot be deleted.
- **Self-healing:** every daemon is supervised and restarted, including watchfrr. Tarvos-owned kernel objects (VRF/VXLAN devices, isolation routes, the management default route, QoS qdiscs, the CoPP table) are restored within 10 s if removed behind Tarvos' back, and Tarvos-owned FRR configuration on request (`FRR_RECONCILE_INTERVAL`, off by default).
- **Measured (full day-2 run, 24 cases: 23 pass, 1 documented limitation):** BFD detects a link failure in 1.2 s, with 7 s of L2/L3 outage on a link flap; ECMP over two spines loses 0.1 s when a spine link fails; after `docker restart`, NETCONF is back in 3 s and BGP in 5 s; ON_CHANGE reports a BGP session change within 3 s; 50 TACACS+ logins take 10 s; failover to a second TACACS+ server takes 7 s (`TACACS_TIMEOUT` 3 s).
- **Chaos suite:** process kills and hangs (FRR daemons, watchfrr, Netopeer2, Sysrepo, every backend, the telemetry agent, sshd, lldpd, rsyslog), kills during commits, `docker kill`/`restart`/`stop` mid-commit, CPU, PID and memory limits, kernel state removed behind Tarvos' back, netem loss/delay/reorder/duplication, MTU black hole, partition, link flapping, BGP reset storms, TCP RST, malformed packets, floods with CoPP, scrape and gNMI load, idle sessions, a TACACS+ server that never answers, and a seeded random sequence of valid and invalid edits. Weaknesses found and fixed in 2.1.1: watchfrr was not supervised, and QoS and CoPP were not repaired when changed behind Tarvos' back. Known limitation: silent TCP clients on port 830 delay new NETCONF sessions until their handshake times out (rate-limit 830 with CoPP).
- **Concurrency and scale:** 20 parallel NETCONF sessions with SSH and gNMI at the same time; 500 prefix-list entries, 200 route-map entries, 50 neighbours or 100 VNIs in one edit; no memory growth over 1000 commits.

## Security notes

- **Change `ADMIN_PASSWORD`.** The default `admin`/`admin` is public knowledge. The container logs a warning while it is in use. Do not expose port 22/830 of a default container to untrusted networks.
- `admin` is the break-glass account: it never waits on TACACS+ and keeps working when TACACS+ is down. TACACS+ users are mapped to a shared unprivileged local account.
- The TACACS+ secret is written only to root-only (`0600`) PAM files and is never logged. Over NETCONF, `admin` can write it but not read it back.
- NETCONF users are authenticated through PAM under their own name, so TACACS+ users need no per-user setup in Tarvos and appear by name in NETCONF logs. Access is controlled by NACM: `admin` has full access, TACACS+ users get `TACACS_NETCONF_ACCESS` (read-only by default; set `rw` to let them configure).
- Both SSH `password` (ncclient, Ansible `netconf`) and `keyboard-interactive` (OpenSSH `ssh -s`, `netopeer2-cli`) go through the same PAM stack, so TACACS+ users work with either. Upstream libnetconf2 checks `password` against the local password file only; the image carries a small patch (`docker/patches/libnetconf2-password-pam.patch`) to route it through PAM.

## Upgrading from 2.1.4

2.1.5 stops a log storm on hosts that confine rsyslogd with AppArmor. Existing `.env` files and NETCONF configurations keep working. Behaviour that changes:

- **No syslog stream on stdout when the container has a terminal** (new `SYSLOG_STDOUT=auto`, the default): containerlab starts containers with a TTY, `/dev/stdout` is then `/dev/pts/0`, and the AppArmor profile `rsyslogd` denies opening it. rsyslogd suspended and resumed its stdout action in a loop, about 2,520 events per hour per node (SHD-27). Without a terminal (`docker run -d`, compose) the stream is unchanged. Remote forwarding is unchanged. On a host whose AppArmor allows `/dev/pts`, set `SYSLOG_STDOUT=yes` to keep the stream on the terminal; `no` disables it everywhere.

## Upgrading from 2.1.3

2.1.4 fixes two problems found in a containerlab fabric. Existing `.env` files and NETCONF configurations keep working. Behaviour that changes:

- **Remote syslog is RFC 5424** (`RSYSLOG_SyslogProtocol23Format`), with the PRI: collectors now get the facility and the severity. Before, the remote servers received the stdout format (`<time> <host> <tag>: <msg>`, no PRI), which parsers such as Telegraf's syslog grok rejected or stored without a level. The stdout stream keeps the short readable format.
- **Console shell gives up when it cannot start**: a TTY that cannot become a controlling terminal (containerlab gives one) made `setsid` fail every second forever, and each attempt made rsyslog suspend and resume its stdout action, about 40 log lines a minute forwarded to the collectors. After `CONSOLE_SHELL_MAX_FAILURES` (3) quick failures in a row the console stays log-only, with one log line saying why.

## Upgrading from 2.1.2

2.1.3 keeps the hosts of the L2 VNIs known. Existing `.env` files and NETCONF configurations keep working. Behaviour that changes:

- **Neighbor refresh (new, on by default):** every 10 s (`NETDEV_RECONCILE_INTERVAL`), each STALE local neighbor of a bridge with an anycast gateway is set to PROBE, so the kernel sends it one unicast ARP/ND request from the gateway. An idle host stays REACHABLE and keeps its MAC+IP type-2 route; a host that left fails its probe and ages out as before. Remote entries (installed by zebra) are never touched. Before, an idle host aged out of the table, FRR withdrew its IP, and remote leaves could not reach it until it talked again. `NEIGH_REFRESH_ENABLED=no` restores the old behaviour.

## Upgrading from 2.1.1

2.1.2 fixes bugs found while testing VRFs and the console; existing `.env` files and NETCONF configurations keep working. Behaviour that changes:

- **SSH locale:** `sshd` no longer accepts the client's `LANG`/`LC_*` and the image sets `LANG=C.UTF-8`. Before, a client forwarding `en_US.UTF-8` got a dozen `setlocale` warnings at every login.
- **Console shell (new, on by default):** when the container has a TTY (GNS3's telnet console, `docker run -it`, `docker attach`), a login shell as `admin` is started on it and restarted if it exits. Daemon logs share that console. `CONSOLE_SHELL=no` restores the log-only console.
- **VRF and interface entries are deleted for real:** removing a `frr-vrf` or `frr-interface` entry over NETCONF now deletes it when there is no kernel device. Before, a VRF that existed only in FRR's configuration stayed in `vtysh` as `inactive (configured)`. An entry whose device exists is still only cleared (mgmtd refuses to delete it), and is deleted when a `tarvos-vrf` change removes the device. A node restarted in between keeps such a cleared entry until `no vrf <name>` in `vtysh`.
- **Management interface protected:** a `tarvos-vrf` edit putting the management interface (`eth0` by default) in another VRF, or a `tarvos-vxlan` edit using it as access port, is refused. Before, it was accepted and cut the node off its management VRF.

## Upgrading from 2.1.0

2.1.1 is a hardening release: existing `.env` files and NETCONF configurations keep working, and no action is required beyond the TACACS+ note below. Behaviour that changes:

- **VXLAN MTU:** `vx<vni>`/`br<vni>` get MTU 1450 when `tarvos-vxlan` `mtu` is unset (was 1500, which silently dropped packets of more than 1450 bytes on a 1500-byte underlay). Set `mtu` if your underlay is larger.
- **Tenant VRF isolation:** every Tarvos VRF table gets an `unreachable default` at the highest metric. Before, traffic forwarded from a tenant port to an address the VRF had no route for could leave through the management interface.
- **QoS:** the htb root class is now `1:ffff`, so class id 1 works. A refused or failed QoS edit no longer leaves a qdisc on the interface. Counters restart from zero once, at the first application.
- **LLDP:** the system name is the container hostname (it was `tarvos` on every node). `LLDP_SYSTEM_NAME` still overrides it.
- **Management VRF:** if the management default route disappears (Docker moves the gateway when a network is connected after boot), it is restored within 10 s.
- **TACACS+ with several servers (security fix):** 2.1.0 gave the shared secret to the last server only (`pam_tacplus` semantics). Requests to the other servers, including the primary, went out unobfuscated (PAP password readable on the wire), and every login fell through to the last server. Each server now gets its own `secret=`. If you ran 2.1.0 with more than one server in `TACACS_SERVER`, consider rotating the TACACS+ passwords.
- **`TACACS_NETCONF_ACCESS=none`** refuses TACACS+ users at login on NETCONF and gNMI. Before, NETCONF opened a session that saw no data. SSH shell access is unchanged.
- **Supervision:** a daemon that exits is restarted. Before, the telemetry agent and the LLDP, VRF/VXLAN and kernel backends stayed down, and a critical daemon exiting stopped the container. A crash loop of a critical daemon still stops the container.
- **Large edits:** NETCONF requests may take up to `NETCONF_TIMEOUT` (120 s, Sysrepo's default was 5 s), and the VRF/VXLAN backend batches its kernel commands, so removing 100 VNIs in one edit works.
- **Toolbox:** `ping`, `traceroute`, `mtr`, `dig`, `tcpdump`, `curl`, `nc`, `iperf3`, ... and `vrfexec`, see [Troubleshooting](#troubleshooting).

## Upgrading from 2.0

2.1.0 adds observability: state for every model over NETCONF, the eBPF telemetry agent, Prometheus and gNMI. Existing `.env` files and NETCONF configurations keep working.

- **New listeners**, both in the management VRF: 9273 (Prometheus) and 57400 (gNMI, TLS). Publish them (`-p`, compose `ports:`) or turn them off with `PROMETHEUS_ENABLED=no` / `GNMI_ENABLED=no` (`TELEMETRY_ENABLED=no` disables the agent altogether).
- **Capabilities:** the agent works with the `SYS_ADMIN` capability Tarvos already needs. `docker-compose.yml` also lists `BPF` and `PERFMON`.
- **New backends:** `tarvos-qos`, `tarvos-nftables` and `tarvos-aaa` configuration is now applied, where 2.0 accepted it without effect. Review any configuration you stored in those modules before upgrading.
- **PAM:** the PAM stacks record each authentication (`pam_exec`, for `/tarvos-aaa:aaa/state`). Authentication behaviour is unchanged.
- **RAM:** about 115 MiB with bgpd, against about 90 MiB in 2.0 (kernel backends and telemetry agent).

## Upgrading from 1.x

2.0.0 changes the base image (Debian trixie), FRR (10.7.1 from the official FRR repository, `mgmtd` always running) and adds the routing modules above. Existing `.env` files keep working. Two behaviour changes:

- The management VRF fallback rule (`ip rule ... lookup <MGMT_VRF_TABLE>`) now comes after the main table. Before, every routed packet in the default VRF followed the management default route.
- `FRR_DAEMONS` no longer needs `zebra`/`staticd` (always started); listing them is still accepted.

## Known limitations

- Syslog over UDP (the default) loses what is sent while the collector is unreachable; `SYSLOG_PROTOCOL=tcp` queues it and delivers it when the collector is back (both tested by the day-2 campaign). `sysrepo-plugind` and `netopeer2-server` log to stdout only.
- With several Docker networks, `docker restart` may give the interfaces other names (eth0 and eth1 swapped). The configuration then points at the wrong ports, and the management VRF takes the wrong interface (Tarvos logs a warning when the default route is not on `MGMT_INTERFACE`). Pin the names with Docker 28's endpoint option: `docker run --network name=mgmt,driver-opt=com.docker.network.endpoint.ifname=eth0` and `docker network connect --driver-opt com.docker.network.endpoint.ifname=eth1 ...`, as `tests/lab/lab.sh` does. containerlab and GNS3 name the links themselves.
- NETCONF configuration lives in the container: `<copy-config>` to startup survives `docker restart`, not a new container (image upgrade, `docker rm`). Re-push the configuration (a controller, or `<get-config>` before and `<edit-config>` after); the day-2 campaign does this from a v2.0.0 node.
- TACACS+ is consulted at login: removing a user on the server does not close the user's open sessions (`pkill -u tacacs` does). With every server down, a TACACS+ login waits `TACACS_TIMEOUT` per server before failing; `admin` never waits.
- Not modelled yet (refused, never accepted and ignored): ESI multihoming (EVPN route types 1 and 4), IPv6 VTEP addresses, VLAN-aware bridges, CoPP matches beyond protocol/port/source; see [docs/yang-gaps.md](docs/yang-gaps.md) for the full audit.
- Netopeer2 advertises the `yang-library` capability and serves modules via `<get-schema>`; it does not list every module in its `<hello>`.
- OSPF and IS-IS adjacency state is not modelled yet: use `vtysh` (`show ip ospf neighbor`, `show isis neighbor`).
- The vtysh-rendered FRR models support the common nodes; anything Tarvos cannot render yet (for example cisco-style access lists, BFD authentication, some route-map actions) is refused with an explicit error, never silently dropped.
- Image is `linux/amd64` only.

### Known weaknesses of 2.1.1 (open in the chaos suite)

- **Garbage traffic on a host port and on the management ports.** Hundreds of frames with random source MACs on a VXLAN access port, plus junk TCP connections to SSH and NETCONF, made `admin` SSH logins fail for a while and stopped traffic between the hosts for about 150 s. Suspected causes, not confirmed: sshd's limit on pending connections (`MaxStartups`), and no per-port MAC limit, so every fake MAC is learned and advertised as an EVPN type-2 route. Mitigations: CoPP rate limits on 22 and 830, and port security on the hosts' switch.
- **Tarvos-owned FRR configuration changed with vtysh is not repaired by default.** It is restored at the next NETCONF change of that model or restart of `frr_sync`. `FRR_RECONCILE_INTERVAL=<seconds>` (opt-in, default 0) re-applies it periodically. Each pass is a full frr-reload, too heavy to run often on every node: at 30 s it slowed the integration tests. Do not edit Tarvos-owned FRR blocks with vtysh; use NETCONF.
- **Silent TCP clients on port 830** delay new NETCONF sessions until their SSH handshake times out (rate-limit 830 with CoPP).
- **Slow recovery in the last chaos run.** After each fault the lab took 140 to 200 s to pass every check, against 50 to 85 s in earlier runs. The cause is not known yet.
- The chaos suite was run in pieces (seeds 2111-2113, 3 to 5 kills per write fault), not twice in a row in full: run `tests/chaos/run.sh` (or the `chaos` workflow) for a complete report.

## Development

`feature` -> PR to `qa` (lint, unit tests, build, Trivy, integration tests incl. TACACS+, the troubleshooting toolbox, a two-leaf BGP EVPN lab and the short day-2 campaign, baseline metrics) -> PR to `main` (same checks, then push to Docker Hub as `matt13290/tarvos` with tags `latest`, `vX.Y.Z` from `VERSION`, and the commit SHA). Bump `VERSION` for every merge to `main`.
