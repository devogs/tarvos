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

Debian bookworm-slim + FRRouting + Sysrepo/Netopeer2 (NETCONF) + OpenSSH, with TACACS+ AAA, rsyslog and the management interface in its own VRF.

## Highlights

- **Routing:** FRRouting (BGP, OSPF, IS-IS, BFD) ready for BGP EVPN labs.
- **Model-driven management:** NETCONF through Sysrepo/Netopeer2, with YANG modules loaded dynamically from `/opt/tarvos/yangs`.
- **AAA:** TACACS+ with ordered failover and a local break-glass `admin`.
- **Discovery:** LLDP (lldpd) advertises the node. Manage it and read discovered neighbours over NETCONF with the `tarvos-lldp` module, or use `lldpcli show neighbors`.
- **Isolated management plane:** `eth0` lives in VRF `mgmt` by default.
- **Observability:** rsyslog to stdout, optional forwarding to remote collectors.
- **Small and fast:** about 64 MB compressed, boots in under 10 s, uses about 35 MiB of RAM.

## Get the image

```bash
docker pull matt13290/tarvos:latest      # or a pinned release, e.g. :v1.0.0
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
| `FRR_DAEMONS` | FRR daemons to start, e.g. `zebra bgpd`, `bgpd,ospfd` or `[zebra, ospfd]` (names: zebra, bgpd, ospfd, ospf6d, ripd, ripngd, isisd, pimd, pim6d, ldpd, nhrpd, eigrpd, babeld, sharpd, pbrd, bfdd, fabricd, vrrpd, pathd, staticd). zebra always runs; empty means zebra only. `staticd` is needed for static routes |
| `LLDP_ENABLED`, `LLDP_INTERFACES`, `LLDP_TX_INTERVAL`, `LLDP_TX_HOLD`, `LLDP_SYSTEM_NAME`, `LLDP_SYSTEM_DESCRIPTION` | LLDP neighbour discovery (lldpd, on by default on every interface; `LLDP_INTERFACES` is an lldpd pattern such as `eth*,!eth0`). Inspect with `lldpcli show neighbors` or over NETCONF |
| `SSH_PORT`, `NETCONF_PORT`, `LOG_LEVEL`, `YANG_DIR` | Runtime parameters |

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

## Known limitations

- Remote syslog forwarding is unit-tested but not yet tested against a real collector; `sysrepo-plugind` and `netopeer2-server` log to stdout only.
- Netopeer2 advertises the `yang-library` capability and serves modules via `<get-schema>`; it does not list every module in its `<hello>`.
- Image is `linux/amd64` only.

## Development

`feature` -> PR to `qa` (lint, unit tests, build, Trivy, integration tests incl. TACACS+, baseline metrics) -> PR to `main` (same checks, then push to Docker Hub as `matt13290/tarvos` with tags `latest`, `vX.Y.Z` from `VERSION`, and the commit SHA). Bump `VERSION` for every merge to `main`.
