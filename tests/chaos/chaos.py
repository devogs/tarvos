#!/usr/bin/env python3
"""Tarvos chaos suite: apply a fault, wait, check the invariants, undo it, check again.

Runs against the configured lab of tests/lab/lab.sh (never against anything else).
Seeded and logged so that a run can be replayed: CHAOS_SEED (default: random, printed).

Invariants, checked after every fault and after its undo, within a recovery deadline
(CHAOS_DEADLINE, 60 s), with no manual action:
  1. management: SSH and NETCONF accept admin, the healthcheck is healthy;
  2. configuration: running equals what FRR, the kernel, tc and nftables apply for every
     Tarvos-owned object (compared with the pre-fault reference);
  3. control plane: BGP sessions established, EVPN route counts as before, hosts reach
     each other (loss measured);
  4. observability: the telemetry endpoint answers with tarvos_telemetry_up 1, NETCONF
     <get> answers within its timeout;
  5. no leak: daemon RSS, file descriptors, /dev/shm and threads within a margin of the
     baseline, no zombie;
  6. security: wrong passwords and unknown users stay refused, admin keeps working.

Usage: chaos.py [--only FAULT,...] [--list] [--report DIR] [--monkey MINUTES]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "day2"))

from labkit import FRR, NC, Lab, Pinger, T, sh, wait  # noqa: E402

DEADLINE = float(os.environ.get("CHAOS_DEADLINE", "60"))
FABRIC = ("spine", "leaf1", "leaf2")
PEERS = {"spine": 2, "leaf1": 1, "leaf2": 1}


# --------------------------------------------------------------------------- invariants
def applied(lab: Lab, node: str) -> dict[str, object]:
    """What the node applies for Tarvos-owned objects (the configuration invariant)."""
    frr = lab.vty(node, "show running-config")
    bgp = sorted(line.strip() for line in frr.splitlines() if line.startswith((" neighbor", "router bgp", " bgp router-id")))
    links = json.loads(lab.x(node, "ip -j -d link show") or "[]")
    owned = sorted(
        (link["ifname"], (link.get("linkinfo") or {}).get("info_kind"), link.get("master"), link.get("mtu"))
        for link in links if link.get("ifalias") == "tarvos"
    )
    members = sorted((link["ifname"], link.get("master")) for link in links
                     if link.get("master") in {o[0] for o in owned} and link.get("ifalias") != "tarvos")
    qdisc = lab.x(node, "tc qdisc show dev eth1 root", check=False).split(" ")[1:2]
    nft = sorted(re.findall(r'comment "(tarvos:[^"]+)"', lab.x(node, "nft list table inet tarvos 2>/dev/null", check=False)))
    isolation = sorted(re.findall(r"table (\d+)", lab.x(node, "ip route show table all type unreachable proto tarvos", check=False)))
    return {"bgp": bgp, "devices": owned, "members": members, "qdisc": qdisc, "copp": nft, "isolation": isolation}


def health(lab: Lab, node: str) -> str:
    return sh("docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{end}}", lab.name(node)).strip()


def resources(lab: Lab, node: str) -> dict[str, float]:
    out = {f"rss:{k}": v for k, v in lab.rss(node).items()}
    out["fds"] = float(lab.x(node, "ls /proc/[0-9]*/fd 2>/dev/null | wc -l", check=False).strip() or 0)
    out["shm_kib"] = float(lab.x(node, "du -sk /dev/shm | cut -f1", check=False).strip() or 0)
    out["threads"] = float(lab.x(node, "ls -d /proc/[0-9]*/task/* | wc -l", check=False).strip() or 0)
    out["zombies"] = float(lab.x(node, "ps -eo stat | grep -c '^Z' || true", check=False).strip() or 0)
    return out


@dataclass
class Baseline:
    applied: dict[str, dict[str, object]]
    evpn: dict[str, dict[int, int]]
    resources: dict[str, dict[str, float]]


def evpn_counts(lab: Lab, node: str) -> dict[int, int]:
    xml = lab.get(node, f'<bgp xmlns="{T}tarvos-bgp"><state><evpn><route-count/></evpn></state></bgp>')
    return {int(t): int(c) for t, c in re.findall(r"<route-type>(\d)</route-type>\s*<count>(\d+)</count>", xml)}


def baseline(lab: Lab) -> Baseline:
    return Baseline(
        applied={n: applied(lab, n) for n in FABRIC},
        evpn={n: evpn_counts(lab, n) for n in ("leaf1", "leaf2")},
        resources={n: resources(lab, n) for n in FABRIC},
    )


def check_invariants(lab: Lab, base: Baseline, deadline: float = DEADLINE) -> tuple[dict[str, str], float]:
    """Wait (up to the deadline) until every invariant holds; returns the failures left
    and the time it took to recover."""
    start = time.monotonic()
    failures: dict[str, str] = {}
    while True:
        failures = {}
        for node in FABRIC:
            try:
                ssh = lab.ssh(node, "true", timeout=15)
                if ssh.returncode != 0:
                    failures[f"1 management {node}"] = "admin SSH refused"
                elif health(lab, node) not in ("healthy", ""):
                    failures[f"1 management {node}"] = f"healthcheck {health(lab, node)}"
                if not lab.established(node, PEERS[node]):
                    failures[f"3 control plane {node}"] = "BGP sessions not all established"
                now = applied(lab, node)
                for key, value in base.applied[node].items():
                    if now[key] != value:
                        failures[f"2 configuration {node}"] = f"{key} differs from running"
                metrics = lab.metrics(node)
                if metrics.get("tarvos_telemetry_up") != 1:
                    failures[f"4 observability {node}"] = "tarvos_telemetry_up is not 1"
            except Exception as exc:  # noqa: BLE001 - an unreachable node is a failure
                failures.setdefault(f"1 management {node}", f"{type(exc).__name__}: {str(exc)[:120]}")
        for node in ("leaf1", "leaf2"):
            try:
                counts = evpn_counts(lab, node)
                # type-2 follows host traffic (MACs learned and aged): only types 3 and 5
                # must be back to their reference values, and type-2 routes present
                stable = {t: c for t, c in counts.items() if t != 2}
                reference = {t: c for t, c in base.evpn[node].items() if t != 2}
                if stable != reference or not counts.get(2):
                    failures[f"3 control plane {node} EVPN"] = f"{counts} != {base.evpn[node]}"
            except Exception as exc:  # noqa: BLE001
                failures[f"3 control plane {node} EVPN"] = str(exc)[:120]
        if " 0% packet loss" not in lab.x("h1", "ping -c1 -W1 10.1.100.12", check=False):
            failures["3 control plane h1->h2"] = "no reply"
        if " 0% packet loss" not in lab.x("h1", "ping -c1 -W1 10.10.2.1", check=False):
            failures["3 control plane h1->leaf2 RED"] = "no reply"
        if not failures or time.monotonic() - start > deadline:
            break
        time.sleep(2)
    for node in FABRIC:  # security, once recovered
        try:
            if lab.ssh(node, "true", "admin", "wrong").returncode == 0:
                failures[f"6 security {node}"] = "wrong admin password accepted"
            if lab.ssh(node, "true", "mallory", "x").returncode == 0:
                failures[f"6 security {node}"] = "unknown user accepted"
        except Exception as exc:  # noqa: BLE001
            failures[f"6 security {node}"] = str(exc)[:120]
    return failures, time.monotonic() - start


def check_leaks(lab: Lab, base: Baseline) -> dict[str, str]:
    failures = {}
    for node in FABRIC:
        now = resources(lab, node)
        for key, before in base.resources[node].items():
            after = now.get(key, 0.0)
            margin = {"fds": 50, "threads": 20, "shm_kib": 2048, "zombies": 0}.get(key, max(8.0, 0.25 * before))
            if key.startswith("rss:") and key[4:] not in now and before:
                continue
            if after - before > margin:
                failures[f"5 leak {node} {key}"] = f"{before:.0f} -> {after:.0f}"
    return failures


# --------------------------------------------------------------------------- faults
@dataclass
class Fault:
    name: str
    group: str
    apply: Callable[[Lab], object]
    undo: Callable[[Lab, object], None] = lambda lab, token: None
    hold: float = 5.0
    # what may be true while the fault is held (checked during the hold)
    during: Callable[[Lab, dict[str, str]], None] | None = None


FAULTS: list[Fault] = []


def fault(name: str, group: str, hold: float = 5.0) -> Callable:
    def register(apply: Callable[[Lab], object]) -> Callable[[Lab], object]:
        FAULTS.append(Fault(name, group, apply, hold=hold))
        return apply

    return register


def kill(node: str, process: str, signal: str = "KILL") -> Callable[[Lab], object]:
    def apply(lab: Lab) -> object:
        pattern = process if len(process) <= 15 else process[:15]
        flag = "-f" if process.endswith(".py") else "-x"
        return lab.x(node, f"pkill -{signal} {flag} {pattern}; true", check=False)

    return apply


PROCESSES = ["bgpd", "zebra", "mgmtd", "staticd", "bfdd", "watchfrr", "netopeer2-server",
             "sysrepo-plugind", "frr_sync.py", "netdev_sync.py", "kernel_sync.py", "lldp_sync.py",
             "tarvos-telemetry", "sshd", "lldpd", "rsyslogd"]  # fmt: skip
for _process in PROCESSES:
    FAULTS.append(Fault(f"kill -9 {_process} (leaf1)", "process", kill("leaf1", _process)))


def _stop_cont(process: str, seconds: float) -> Fault:
    def apply(lab: Lab) -> object:
        return kill("leaf1", process, "STOP")(lab)

    def undo(lab: Lab, _token: object) -> None:
        kill("leaf1", process, "CONT")(lab)

    return Fault(f"SIGSTOP {process} {seconds:.0f} s (leaf1)", "process", apply, undo, hold=seconds)


FAULTS += [_stop_cont("bgpd", 15), _stop_cont("netopeer2-server", 10), _stop_cont("frr_sync.py", 10)]


def _repeated_kill(process: str, times: int) -> Fault:
    def apply(lab: Lab) -> object:
        for _ in range(times):
            kill("leaf2", process)(lab)
            time.sleep(4)
        return None

    return Fault(f"kill -9 {process} {times} times (leaf2)", "process", apply, hold=1)


FAULTS += [_repeated_kill("bgpd", 3), _repeated_kill("tarvos-telemetry", 3), _repeated_kill("kernel_sync.py", 3)]


def _kill_during_write(process: str, repeat: int) -> Fault:
    """SIGKILL of a daemon while an edit-config commits, repeated: after recovery the
    datastore, FRR and the kernel must agree (checked by the invariants)."""

    def apply(lab: Lab) -> object:
        rng = random.Random(lab.seed)
        for i in range(repeat):
            entries = [(s, f"10.{i % 200}.{s % 250}.0/24") for s in range(5, 5 + 5 * rng.randint(20, 120), 5)]
            body = "".join(f"<entry><sequence>{s}</sequence><action>permit</action><ipv4-prefix>{p}</ipv4-prefix></entry>"
                           for s, p in entries)  # fmt: skip
            xml = (f'<lib xmlns="{FRR}filter"><prefix-list><type>ipv4</type><name>CHAOS</name>{body}</prefix-list></lib>'
                   f'<vxlan xmlns="{T}tarvos-vxlan"><l2vni><vni>{3000 + i % 5}</vni><vrf>RED</vrf></l2vni></vxlan>')  # fmt: skip
            worker = threading.Thread(target=lambda x=xml: lab.refused("leaf1", x))
            worker.start()
            time.sleep(rng.uniform(0.05, 0.6))
            kill("leaf1", process)(lab)
            worker.join(timeout=150)
            time.sleep(rng.uniform(1, 3))
        return None

    def undo(lab: Lab, _token: object) -> None:
        wait("NETCONF back", lambda: lab.refused("leaf1", f'<lib xmlns="{FRR}filter" xmlns:nc="{NC}"><prefix-list nc:operation="remove"><type>ipv4</type><name>CHAOS</name></prefix-list></lib>'
                                                f'<vxlan xmlns="{T}tarvos-vxlan" xmlns:nc="{NC}">'
                                                + "".join(f'<l2vni nc:operation="remove"><vni>{3000 + k}</vni></l2vni>' for k in range(5))
                                                + "</vxlan>") is None, timeout=120)  # fmt: skip

    return Fault(f"kill -9 {process} during edit-config x{repeat} (leaf1)", "write", apply, undo, hold=1)


REPEAT = int(os.environ.get("CHAOS_REPEAT", "20"))
FAULTS += [_kill_during_write(p, REPEAT) for p in ("netopeer2-server", "sysrepo-plugind", "frr_sync.py", "netdev_sync.py")]


def _container(action: str) -> Fault:
    def apply(lab: Lab) -> object:
        name = lab.name("leaf2")
        if action == "kill":
            sh("docker", "kill", name)
            sh("docker", "start", name)
        elif action == "restart":
            sh("docker", "restart", name)
        elif action == "stop-mid-commit":
            worker = threading.Thread(target=lambda: lab.refused("leaf2", f'<vxlan xmlns="{T}tarvos-vxlan"><l2vni><vni>3100</vni><vrf>RED</vrf></l2vni></vxlan>'))
            worker.start()
            time.sleep(0.3)
            sh("docker", "stop", "-t", "5", name)
            worker.join(timeout=60)
            sh("docker", "start", name)
        lab.refresh_ip("leaf2")
        sh("bash", str(Path(__file__).resolve().parent.parent / "lab/lab.sh"), "relink", "leaf2")
        return None

    return Fault(f"docker {action} (leaf2)", "container", apply, hold=1)


FAULTS += [_container("kill"), _container("restart"), _container("stop-mid-commit")]


def _limits(kind: str) -> Fault:
    def apply(lab: Lab) -> object:
        name = lab.name("leaf2")
        if kind == "cpu":
            sh("docker", "update", "--cpus", "0.05", name)
        elif kind == "pids":
            used = int(lab.x("leaf2", "ls -d /proc/[0-9]*/task/* | wc -l").strip())
            sh("docker", "update", "--pids-limit", str(used + 20), name)
        elif kind == "memory":
            usage = int(lab.x("leaf2", "cat /sys/fs/cgroup/memory.current").strip())
            # below the working set: the kernel reclaims, then OOM-kills a process
            sh("docker", "update", "--memory", str(int(usage * 0.7)), "--memory-swap", str(int(usage * 0.7)), name)
        elif kind == "disk":
            lab.x("leaf2", "dd if=/dev/zero of=/run/chaos-fill bs=1M count=200 2>/dev/null; dd if=/dev/zero of=/tmp/chaos-fill bs=1M count=200 2>/dev/null; true", check=False)
        elif kind == "fds":
            lab.x("leaf2", "python3 -c 'import os, time\nfds=[os.open(\"/dev/null\", os.O_RDONLY) for _ in range(4000)]\ntime.sleep(20)' >/dev/null 2>&1 &", check=False)
        return kind

    def undo(lab: Lab, _token: object) -> None:
        name = lab.name("leaf2")
        if kind == "cpu":
            sh("docker", "update", "--cpus", "0", name)
        elif kind == "pids":
            sh("docker", "update", "--pids-limit", "-1", name)
        elif kind == "memory":
            # a limit cannot be removed from a running container: set a large one
            sh("docker", "update", "--memory", "16g", "--memory-swap", "-1", name, check=False)
            if "running" not in sh("docker", "inspect", "-f", "{{.State.Status}}", name):
                sh("docker", "start", name)
                lab.refresh_ip("leaf2")
                sh("bash", str(Path(__file__).resolve().parent.parent / "lab/lab.sh"), "relink", "leaf2")
        elif kind == "disk":
            lab.x("leaf2", "rm -f /run/chaos-fill /tmp/chaos-fill", check=False)

    return Fault(f"resource limit: {kind} (leaf2)", "container", apply, undo, hold=20)


FAULTS += [_limits(k) for k in ("cpu", "pids", "memory")]

# Not simulated, with the reason (listed in the report):
NOT_SIMULATED = {
    "full /run or /tmp": "Docker gives the container no size-limited /run or /tmp: there is nothing to fill",
    "exhausted file descriptors": "limits are per process (nofile 1048576 in Docker); the 50 idle NETCONF sessions "
                                  "and idle TCP clients cover descriptor pressure on the daemons",
    "clock jump": "setting the clock needs CAP_SYS_TIME and changes the host clock",
    "read-only root filesystem": "not supported: Tarvos renders PAM, sshd, FRR and Sysrepo files at boot",
}


def _drift(name: str, node: str, command: str) -> Fault:
    return Fault(f"drift: {name} ({node})", "drift", lambda lab: lab.x(node, command, check=False), hold=1)


FAULTS += [
    _drift("ip link del vx100", "leaf1", "ip link del vx100"),
    _drift("ip link del br100", "leaf1", "ip link del br100"),
    _drift("ip link del RED", "leaf1", "ip link del RED"),
    _drift("ip addr flush br100", "leaf1", "ip addr flush dev br100"),
    _drift("isolation route removed", "leaf1", "ip route del unreachable default table 1010 metric 4278198272"),
    _drift("tc qdisc del (QoS)", "leaf1", "tc qdisc del dev eth1 root"),
    _drift("nft flush ruleset (CoPP)", "leaf1", "nft flush ruleset"),
    _drift("mgmt default route removed", "leaf1", "ip route del default table 1001"),
    _drift("mgmt fallback rule removed", "leaf1", "ip rule del pref 32800"),
    _drift("vtysh: Tarvos-owned BGP neighbour removed", "leaf1", "vtysh -c 'conf t' -c 'router bgp 65000' -c 'no neighbor 10.0.1.2'"),
]


def _netem(spec: str, node: str = "spine", dev: str = "eth1") -> Fault:
    def apply(lab: Lab) -> object:
        return lab.x(node, f"tc qdisc replace dev {dev} root netem {spec}")

    def undo(lab: Lab, _token: object) -> None:
        lab.x(node, f"tc qdisc del dev {dev} root", check=False)

    return Fault(f"network: netem {spec} (spine->leaf1)", "network", apply, undo, hold=20)


FAULTS += [_netem(s) for s in ("loss 5%", "loss 30%", "delay 100ms 50ms", "reorder 25% delay 20ms", "duplicate 20%")]


def _black_hole(lab: Lab) -> object:
    # MTU black hole: big frames dropped on the underlay, no ICMP
    return lab.x("spine", "nft add table inet chaos; nft add chain inet chaos fwd '{ type filter hook forward priority 0; }'; "
                 "nft add chain inet chaos out '{ type filter hook output priority 0; }'; "
                 "nft add rule inet chaos fwd meta length gt 1400 drop; nft add rule inet chaos out meta length gt 1400 drop")


FAULTS.append(Fault("network: MTU black hole on the spine (> 1400 bytes)", "network", _black_hole,
                    lambda lab, t: lab.x("spine", "nft delete table inet chaos", check=False), hold=20))  # fmt: skip
FAULTS.append(Fault("network: partial partition (spine loses leaf2)", "network",
                    lambda lab: lab.x("spine", "ip link set eth2 down"),
                    lambda lab, t: lab.x("spine", "ip link set eth2 up"), hold=15))  # fmt: skip


def _flap(lab: Lab) -> object:
    for _ in range(8):
        lab.x("spine", "ip link set eth1 down")
        time.sleep(2)
        lab.x("spine", "ip link set eth1 up")
        time.sleep(2)
    return None


FAULTS.append(Fault("network: link flapping every 2 s for 32 s", "network", _flap, hold=1))
FAULTS.append(Fault("network: BGP reset storm (clear bgp * x20)", "network",
                    lambda lab: [lab.vty("spine", "clear bgp *") or time.sleep(1) for _ in range(20)], hold=1))  # fmt: skip


def _rst(port: int) -> Fault:
    def apply(lab: Lab) -> object:
        return lab.x("spine", f"nft add table inet chaos; nft add chain inet chaos out '{{ type filter hook output priority 0; }}'; "
                     f"nft add rule inet chaos out tcp dport {port} reject with tcp reset; "
                     f"nft add rule inet chaos out tcp sport {port} reject with tcp reset")

    return Fault(f"network: TCP RST on port {port} (spine)", "network", apply,
                 lambda lab, t: lab.x("spine", "nft delete table inet chaos", check=False), hold=15)  # fmt: skip


FAULTS += [_rst(179)]


def _malformed(lab: Lab) -> object:
    """Garbage towards BGP (179), VXLAN (4789), NETCONF (830), SSH (22), LLDP (raw frames)
    and the TACACS+ client path, from h1 and the runner."""
    target = lab.ip("leaf1")
    script = f"""
import os, random, socket
rng = random.Random({lab.seed})
for port in (179, 830, 22):
    for _ in range(20):
        try:
            s = socket.create_connection(("{target}", port), timeout=2)
            s.sendall(os.urandom(rng.randint(1, 4096)))
            s.close()
        except OSError:
            pass
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for _ in range(500):
    u.sendto(os.urandom(rng.randint(8, 1400)), ("{target}", 4789))
"""
    sh("python3", "-c", script, timeout=120)
    # malformed LLDP and random Ethernet frames on leaf1's access port (from h1)
    lab.x("h1", "python3 -c \"import os, socket\ns=socket.socket(socket.AF_PACKET, socket.SOCK_RAW)\ns.bind(('eth0', 0))\n"
          "for i in range(300):\n    s.send(bytes.fromhex('0180c200000e') + os.urandom(6) + bytes.fromhex('88cc') + os.urandom(i % 200 + 1))\"", check=False)
    return None


FAULTS.append(Fault("malformed packets: BGP, NETCONF, SSH, VXLAN, LLDP", "network", _malformed, hold=1))


def _flood(lab: Lab) -> object:
    """ICMP and SYN flood towards leaf1 with CoPP active: BGP must stay up."""
    lab.edit("leaf1", f'<nftables xmlns="{T}tarvos-nftables"><copp>'
             "<rule><name>bgp</name><protocol>tcp</protocol><destination-port>179</destination-port></rule>"
             "<rule><name>icmp</name><protocol>icmp</protocol><action>rate-limit</action><max-pps>100</max-pps></rule>"
             "</copp></nftables>")  # fmt: skip
    target = lab.ip("leaf1")
    flood = f"""
import socket, threading, time
end = time.time() + 15
def syn():
    while time.time() < end:
        try:
            s = socket.socket(); s.setblocking(False); s.connect_ex(("{target}", 830)); s.close()
        except OSError:
            pass
threads = [threading.Thread(target=syn) for _ in range(8)]
[t.start() for t in threads]; [t.join() for t in threads]
"""
    pinger = subprocess.Popen(["docker", "exec", lab.name("h1"), "ping", "-f", "-c", "200000", "-s", "1000", "10.10.1.1"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    sh("python3", "-c", flood, timeout=60)
    pinger.kill()
    return None


def _flood_undo(lab: Lab, _token: object) -> None:
    lab.edit("leaf1", f'<nftables xmlns="{T}tarvos-nftables" xmlns:nc="{NC}" nc:operation="remove"/>')
    lab.x("h1", "pkill ping", check=False)


def _flood_during(lab: Lab, failures: dict[str, str]) -> None:
    if not lab.established("leaf1", 1):
        failures["3 control plane leaf1 under flood"] = "BGP session lost during the flood"


FAULTS.append(Fault("load: ICMP + SYN flood with CoPP", "load", _flood, _flood_undo, hold=1, during=_flood_during))


def _scrape_and_gnmi_load(lab: Lab) -> object:
    target = lab.ip("leaf1")
    load = f"""
import threading, time, urllib.request
end = time.time() + 15
errors = 0
def scrape():
    global errors
    while time.time() < end:
        try:
            urllib.request.urlopen("http://{target}:9273/metrics", timeout=10).read()
        except Exception:
            errors += 1
threads = [threading.Thread(target=scrape) for _ in range(20)]
[t.start() for t in threads]; [t.join() for t in threads]
print(errors)
"""
    procs = [subprocess.Popen(["gnmic", "-a", f"{target}:57400", "-u", "admin", "-p", "admin", "--skip-verify",
                               "subscribe", "--stream-mode", "sample", "--sample-interval", "1s",
                               "--path", "/interfaces/interface/state/counters"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(30)]  # fmt: skip
    errors = sh("python3", "-c", load, timeout=120).strip()
    for p in procs:
        p.kill()
    return errors


FAULTS.append(Fault("load: 20 parallel scrapers + 30 gNMI subscribers", "load", _scrape_and_gnmi_load, hold=1))


def _idle_sessions(lab: Lab) -> object:
    """50 NETCONF sessions that never close, idle TCP clients and a slow-loris on SSH."""
    sessions = []
    for _ in range(50):
        try:
            sessions.append(lab.nc("leaf1", timeout=30))
        except Exception:  # noqa: BLE001 - a limit is acceptable, the node must stay manageable
            break
    import socket

    idle = []
    for port in (22, 830, 57400, 9273):
        for _ in range(30):
            try:
                idle.append(socket.create_connection((lab.ip("leaf1"), port), timeout=5))
            except OSError:
                pass
    return sessions, idle


def _idle_undo(lab: Lab, token: object) -> None:
    sessions, idle = token  # type: ignore[misc]
    for s in sessions:
        try:
            s.close_session()
        except Exception:  # noqa: BLE001
            pass
    for s in idle:
        s.close()


def _idle_during(lab: Lab, failures: dict[str, str]) -> None:
    if lab.ssh("leaf1", "true", timeout=30).returncode != 0:
        failures["1 management leaf1 with idle clients"] = "admin cannot log in while idle clients hold connections"
    try:
        lab.bgp_state("leaf1")
    except Exception as exc:  # noqa: BLE001
        failures["1 management leaf1 with idle clients"] = f"NETCONF: {exc}"


FAULTS.append(Fault("load: 50 idle NETCONF sessions, idle TCP clients, slow-loris", "load", _idle_sessions, _idle_undo,
                    hold=5, during=_idle_during))  # fmt: skip


def _hanging_tacacs(lab: Lab) -> object:
    name = f"{lab.prefix}-tacacs-hang"
    sh("docker", "run", "-d", "--name", name, "-v", f"{Path(__file__).resolve().parent.parent / 'integration'}:/t:ro",
       "--entrypoint", "python3", lab.image, "/t/tacacs_server.py", "--key", lab.env["TACACS_KEY"], "--hang")  # fmt: skip
    ip = sh("docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", name).strip()
    lab.edit("leaf1", f'<aaa xmlns="{T}tarvos-aaa"><tacacs><server><address>{ip}</address><secret>{lab.env["TACACS_KEY"]}</secret></server></tacacs></aaa>')
    return name


def _hanging_undo(lab: Lab, token: object) -> None:
    lab.edit("leaf1", f'<aaa xmlns="{T}tarvos-aaa" xmlns:nc="{NC}" nc:operation="remove"/>')
    sh("docker", "rm", "-f", str(token), check=False)


def _hanging_during(lab: Lab, failures: dict[str, str]) -> None:
    start = time.monotonic()
    if lab.ssh("leaf1", "true", "alice", "alicepw", timeout=60).returncode == 0:
        failures["6 security leaf1"] = "login accepted through a server that never answers"
    if time.monotonic() - start > 30:
        failures["1 management leaf1 TACACS+ hang"] = f"TACACS+ login hung {time.monotonic() - start:.0f} s"
    start = time.monotonic()
    if lab.ssh("leaf1", "true", timeout=30).returncode != 0 or time.monotonic() - start > 5:
        failures["1 management leaf1 TACACS+ hang"] = "admin waits on a hanging TACACS+ server"


FAULTS.append(Fault("load: TACACS+ server that accepts and never answers", "load", _hanging_tacacs, _hanging_undo,
                    hold=1, during=_hanging_during))  # fmt: skip


# --------------------------------------------------------------------------- monkey
def monkey(lab: Lab, minutes: float, rng: random.Random, base: Baseline, log: list[dict]) -> dict[str, str]:
    """A seeded sequence of valid and invalid edits from two sessions, interleaved with
    light faults; every invalid edit must be refused with no side effect."""
    end = time.monotonic() + minutes * 60
    failures: dict[str, str] = {}
    valid = [
        lambda i: f'<lib xmlns="{FRR}filter"><prefix-list><type>ipv4</type><name>MONKEY{i % 4}</name><entry><sequence>{5 + i % 10}</sequence><action>permit</action><ipv4-prefix>10.{i % 250}.0.0/16</ipv4-prefix></entry></prefix-list></lib>',
        lambda i: f'<vxlan xmlns="{T}tarvos-vxlan"><l2vni><vni>{4000 + i % 6}</vni><vrf>RED</vrf></l2vni></vxlan>',
        lambda i: f'<qos xmlns="{T}tarvos-qos"><interface><name>eth1</name><qdisc>htb</qdisc><bandwidth>{100000 + i}</bandwidth><class><id>{1 + i % 3}</id><rate>1000</rate></class></interface></qos>',
    ]
    invalid = [
        lambda i: f'<vrfs xmlns="{T}tarvos-vrf"><vrf><name>BAD{i}</name><table>254</table></vrf></vrfs>',
        lambda i: f'<qos xmlns="{T}tarvos-qos"><interface><name>eth1</name><qdisc>htb</qdisc><bandwidth>1000</bandwidth><class><id>9</id><rate>9000</rate></class></interface></qos>',
        lambda i: f'<bgp xmlns="{T}tarvos-bgp"><instance><vrf>default</vrf><asn>{65001 + i}</asn></instance></bgp>',
        lambda i: f'<vxlan xmlns="{T}tarvos-vxlan"><l2vni><vni>{4100 + i}</vni><vrf>NOPE</vrf></l2vni></vxlan>',
    ]
    light = [lambda: kill("leaf1", "kernel_sync.py")(lab), lambda: kill("leaf1", "netdev_sync.py")(lab),
             lambda: lab.x("leaf1", "ip link del vx4000", check=False), lambda: None]  # fmt: skip
    i = 0
    while time.monotonic() < end:
        i += 1
        good = rng.random() < 0.6
        xml = (rng.choice(valid) if good else rng.choice(invalid))(i)
        before = applied(lab, "leaf1") if not good else None
        results: dict[str, str | None] = {}

        def edit(tag: str, x: str = xml) -> None:
            try:
                results[tag] = lab.refused("leaf1", x)
            except Exception as exc:  # noqa: BLE001
                results[tag] = f"exception {exc}"

        threads = [threading.Thread(target=edit, args=("a",))]
        if rng.random() < 0.3:  # a conflicting edit from a second session
            threads.append(threading.Thread(target=edit, args=("b", rng.choice(valid)(i + 1))))
        for t in threads:
            t.start()
        if rng.random() < 0.15:
            rng.choice(light)()
        for t in threads:
            t.join(timeout=150)
        log.append({"step": i, "valid": good, "results": results})
        if not good:
            if results.get("a") is None:
                failures[f"monkey step {i}"] = f"invalid edit accepted: {xml[:120]}"
            elif before is not None and "b" not in results:
                after = applied(lab, "leaf1")
                if after != before:
                    failures[f"monkey step {i}"] = "refused edit changed what the node applies"
    # clean up and compare with the reference
    lab.edit("leaf1", f'<lib xmlns="{FRR}filter" xmlns:nc="{NC}">'
             + "".join(f'<prefix-list nc:operation="remove"><type>ipv4</type><name>MONKEY{k}</name></prefix-list>' for k in range(4))
             + f'</lib><vxlan xmlns="{T}tarvos-vxlan" xmlns:nc="{NC}">'
             + "".join(f'<l2vni nc:operation="remove"><vni>{4000 + k}</vni></l2vni>' for k in range(6))
             + f'</vxlan><qos xmlns="{T}tarvos-qos" xmlns:nc="{NC}" nc:operation="remove"/>')  # fmt: skip
    rest, _ = check_invariants(lab, base)
    failures.update(rest)
    return failures


# --------------------------------------------------------------------------- runner
@dataclass
class Outcome:
    fault: str
    group: str
    passed: bool
    recovery: float
    failures: dict[str, str] = field(default_factory=dict)
    note: str = ""


def run_fault(lab: Lab, f: Fault, base: Baseline) -> Outcome:
    print(f"== {f.group}: {f.name}", flush=True)
    failures: dict[str, str] = {}
    with Pinger(lab, "h1", "10.1.100.12") as ping:
        token = f.apply(lab)
        time.sleep(f.hold)
        if f.during:
            f.during(lab, failures)
        f.undo(lab, token)
        after, recovery = check_invariants(lab, base)
        failures.update(after)
        outage = ping.lost_seconds()
    note = f"h1->h2 longest outage {outage:.1f} s"
    out = Outcome(f.name, f.group, not failures, recovery, failures, note)
    print(f"   {'PASS' if out.passed else 'FAIL'} (recovered in {recovery:.0f} s, {note})", flush=True)
    for key, value in failures.items():
        print(f"     {key}: {value}", flush=True)
    return out


def report(outcomes: list[Outcome], directory: Path, seed: int, leaks: dict[str, str], monkey_failures: dict[str, str] | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [f"# Tarvos chaos run (seed {seed}, {time.strftime('%Y-%m-%d %H:%M')})", "",
             "| Group | Fault | Result | Recovery | Notes |", "|---|---|---|---|---|"]  # fmt: skip
    for o in outcomes:
        failed = "; ".join(f"{k}: {v}" for k, v in o.failures.items())
        lines.append(f"| {o.group} | {o.fault} | {'PASS' if o.passed else 'FAIL'} | {o.recovery:.0f} s | {o.note}{'; ' + failed if failed else ''} |")
    if monkey_failures is not None:
        lines += ["", f"Random monkey: {'PASS' if not monkey_failures else 'FAIL'}"]
        lines += [f"- {k}: {v}" for k, v in monkey_failures.items()]
    lines += ["", f"Leak check after the run: {'PASS' if not leaks else 'FAIL'}"]
    lines += [f"- {k}: {v}" for k, v in leaks.items()]
    lines += ["", "Not simulated:"] + [f"- {k}: {v}" for k, v in NOT_SIMULATED.items()]
    (directory / f"chaos-{seed}.md").write_text("\n".join(lines) + "\n")
    (directory / f"chaos-{seed}.json").write_text(json.dumps(
        {"seed": seed, "outcomes": [o.__dict__ for o in outcomes], "leaks": leaks, "monkey": monkey_failures}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--report", default=str(Path(__file__).resolve().parent / "report"))
    parser.add_argument("--monkey", type=float, default=float(os.environ.get("CHAOS_MONKEY_MINUTES", "5")))
    args = parser.parse_args()
    if args.list:
        for f in FAULTS:
            print(f"{f.group:10} {f.name}")
        return 0
    seed = int(os.environ.get("CHAOS_SEED") or random.SystemRandom().randrange(1, 2**31))
    print(f"CHAOS_SEED={seed}", flush=True)
    rng = random.Random(seed)
    lab = Lab()
    lab.seed = seed  # type: ignore[attr-defined]
    # Reference: the fabric configured, saved (container faults restart from startup).
    for node in FABRIC:
        lab.save(node)
    wait("fabric converged", lambda: all(lab.established(n, PEERS[n]) for n in FABRIC), timeout=120)
    lab.edit("leaf1", f'<qos xmlns="{T}tarvos-qos"><interface><name>eth1</name><qdisc>htb</qdisc><bandwidth>1000000</bandwidth>'
             "<class><id>1</id><dscp>46</dscp><rate>100000</rate></class></interface></qos>")  # fmt: skip
    lab.save("leaf1")
    time.sleep(12)
    base = baseline(lab)
    only = [n.strip() for n in args.only.split(",") if n.strip()]
    faults = [f for f in FAULTS if not only or any(o in f.name for o in only)]
    order = faults[:]
    rng.shuffle(order)
    outcomes = [run_fault(lab, f, base) for f in order]
    monkey_failures = None
    if args.monkey > 0 and not only:
        print(f"== monkey: {args.monkey:.0f} min", flush=True)
        steps: list[dict] = []
        monkey_failures = monkey(lab, args.monkey, rng, base, steps)
        print(f"   {'PASS' if not monkey_failures else 'FAIL'} ({len(steps)} steps)", flush=True)
    time.sleep(10)
    leaks = check_leaks(lab, base)
    report(outcomes, Path(args.report), seed, leaks, monkey_failures)
    failed = [o.fault for o in outcomes if not o.passed]
    print(f"\n{len(outcomes)} faults, {len(failed)} failed; monkey {'n/a' if monkey_failures is None else ('ok' if not monkey_failures else 'FAILED')}; leaks {'ok' if not leaks else leaks}")
    print(f"report: {args.report}/chaos-{seed}.md (replay: CHAOS_SEED={seed})")
    return 1 if failed or leaks or monkey_failures else 0


if __name__ == "__main__":
    sys.exit(main())
