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
- **Everything over NETCONF:** FRR's own YANG models where FRR implements them (through mgmtd, as transactions), Tarvos YANG for the daemons FRR does not model yet, rendered to vtysh. A configuration FRR rejects is refused over NETCONF with FRR's error.
- **AAA:** TACACS+ with ordered failover and a local break-glass `admin`.
- **Discovery:** LLDP (lldpd) advertises the node. Manage it and read discovered neighbours over NETCONF with the `tarvos-lldp` module, or use `lldpcli show neighbors`.
- **Isolated management plane:** `eth0` lives in VRF `mgmt` by default.
- **Observability:** rsyslog to stdout, optional forwarding to remote collectors.
- **Small and fast:** about 100 MB compressed, boots in under 10 s, about 90 MiB of RAM with bgpd (FRR 10.7 always runs mgmtd, zebra and staticd).

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

The container needs `NET_ADMIN`, `NET_RAW` and `SYS_ADMIN` (FRR's zebra/bgpd require the last for VRF/netns handling); see `docker-compose.yml`.

The `tarvos-*` YANG modules are built into the image (`/opt/tarvos/yangs-builtin`), so no volume is needed. Custom modules mounted at `/opt/tarvos/yangs` are registered as well (and override a built-in of the same name). Registration happens at every boot, before Netopeer2 starts.

## Configuration (environment, see `.env.example`)

| Variable | Purpose |
|---|---|
| `ADMIN_PASSWORD` | Password of the local `admin` account (default `admin`, **change it**) |
| `TACACS_SERVER`, `TACACS_SECRET`, `TACACS_TIMEOUT` | TACACS+ servers (ordered, failover), shared secret |
| `SYSLOG_SERVER`, `SYSLOG_PORT`, `SYSLOG_PROTOCOL`, `SYSLOG_MIN_SEVERITY` | Optional remote syslog forwarding (logs always go to stdout too) |
| `MGMT_VRF_ENABLED`, `MGMT_VRF_NAME`, `MGMT_VRF_TABLE`, `MGMT_INTERFACE` | Management VRF (default: `eth0` in VRF `mgmt`) |
| `TACACS_NETCONF_ACCESS` | NETCONF rights of TACACS+ users: `ro` (default), `rw` or `none` |
| `FRR_DAEMONS` | FRR daemons to start, e.g. `bgpd`, `bgpd,bfdd` or `[bgpd, ospfd]` (names: bgpd, ospfd, ospf6d, ripd, ripngd, isisd, pimd, pim6d, ldpd, nhrpd, eigrpd, babeld, sharpd, pbrd, bfdd, fabricd, vrrpd, pathd). mgmtd, zebra and staticd always run. NETCONF configuration for a daemon that is not running is refused |
| `LLDP_ENABLED`, `LLDP_INTERFACES`, `LLDP_TX_INTERVAL`, `LLDP_TX_HOLD`, `LLDP_SYSTEM_NAME`, `LLDP_SYSTEM_DESCRIPTION` | LLDP neighbour discovery (lldpd, on by default on every interface; `LLDP_INTERFACES` is an lldpd pattern such as `eth*,!eth0`). Inspect with `lldpcli show neighbors` or over NETCONF |
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

- mgmtd path: each list entry (interface, VRF, static route set, ...) configured over NETCONF is owned by NETCONF; entries never configured over NETCONF keep their CLI configuration. Removing an interface or VRF entry clears its configuration (mgmtd refuses to delete an interface that exists).
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

## LLDP over NETCONF

The `tarvos-lldp` module configures lldpd at runtime and reports discovered neighbours. The `LLDP_*` variables are the boot defaults: a leaf you configure overrides its variable, and removing it restores the variable's value.

| Node | Purpose |
|---|---|
| `/lldp/enabled` | `false` pauses LLDP without losing configuration |
| `/lldp/tx-interval`, `tx-hold`, `system-name`, `system-description`, `interface-pattern` | Same meaning as the matching `LLDP_*` variables |
| `/lldp/interface[name]/admin-status` | `tx-and-rx` (default), `tx-only`, `rx-only` or `disabled` per interface |
| `/lldp/neighbors/neighbor` (read-only) | Chassis/port ID, system name and description, management addresses, capabilities, TTL, per local interface |

Settings are only applied when the container runs with `LLDP_ENABLED=yes`. Per-interface status for an interface that does not exist yet is accepted but only takes effect once the interface is present and the configuration is re-applied.

## Security notes

- **Change `ADMIN_PASSWORD`.** The default `admin`/`admin` is public knowledge. The container logs a warning while it is in use. Do not expose port 22/830 of a default container to untrusted networks.
- `admin` is the break-glass account: it never waits on TACACS+ and keeps working when TACACS+ is down. TACACS+ users are mapped to a shared unprivileged local account.
- The TACACS+ secret is written only to root-only (`0600`) PAM files and is never logged. Over NETCONF, `admin` can write it but not read it back.
- NETCONF users are authenticated through PAM under their own name, so TACACS+ users need no per-user setup in Tarvos and appear by name in NETCONF logs. Access is controlled by NACM: `admin` has full access, TACACS+ users get `TACACS_NETCONF_ACCESS` (read-only by default; set `rw` to let them configure).
- Both SSH `password` (ncclient, Ansible `netconf`) and `keyboard-interactive` (OpenSSH `ssh -s`, `netopeer2-cli`) go through the same PAM stack, so TACACS+ users work with either. Upstream libnetconf2 checks `password` against the local password file only; the image carries a small patch (`docker/patches/libnetconf2-password-pam.patch`) to route it through PAM.

## Upgrading from 1.x

2.0.0 changes the base image (Debian trixie), FRR (10.7.1 from the official FRR repository, `mgmtd` always running) and adds the routing modules above. Existing `.env` files keep working. Two behaviour changes:

- The management VRF fallback rule (`ip rule ... lookup <MGMT_VRF_TABLE>`) now comes after the main table. Before, every routed packet in the default VRF followed the management default route.
- `FRR_DAEMONS` no longer needs `zebra`/`staticd` (always started); listing them is still accepted.

## Known limitations

- Remote syslog forwarding is unit-tested but not yet tested against a real collector; `sysrepo-plugind` and `netopeer2-server` log to stdout only.
- Netopeer2 advertises the `yang-library` capability and serves modules via `<get-schema>`; it does not list every module in its `<hello>`.
- Operational state is served for the mgmtd modules (interfaces, VRFs, RIBs, RIP). BGP/OSPF/IS-IS session state is not modelled yet: use `vtysh` (`show bgp summary`, `show bgp l2vpn evpn`).
- The vtysh-rendered FRR models support the common nodes; anything Tarvos cannot render yet (for example cisco-style access lists, BFD authentication, some route-map actions) is refused with an explicit error, never silently dropped.
- gNMI is not available yet.
- Image is `linux/amd64` only.

## Development

`feature` -> PR to `qa` (lint, unit tests, build, Trivy, integration tests incl. TACACS+ and a two-leaf BGP EVPN lab, baseline metrics) -> PR to `main` (same checks, then push to Docker Hub as `matt13290/tarvos` with tags `latest`, `vX.Y.Z` from `VERSION`, and the commit SHA). Bump `VERSION` for every merge to `main`.
