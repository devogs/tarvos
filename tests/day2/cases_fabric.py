"""Day-2 cases: fabric events (link flap, reboots, MAC mobility), observability
consistency (NETCONF, gNMI, Prometheus and vtysh agree; ON_CHANGE delay), syslog, gNMI."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid

from cases_lifecycle import reaches, relink
from labkit import NC, Lab, Pinger, Recorder, T, case, sh, wait


def evpn_counts_vtysh(lab: Lab, node: str) -> dict[int, int]:
    data = lab.vty_json(node, "show bgp l2vpn evpn json")
    counts: dict[int, int] = {}
    for rd, routes in data.items():
        if not isinstance(routes, dict) or rd in ("numPrefix", "totalPrefix"):
            continue
        for prefix in routes:
            m = re.match(r"\[(\d)\]", prefix)
            if m:
                counts[int(m.group(1))] = counts.get(int(m.group(1)), 0) + 1
    return counts


def evpn_counts_netconf(lab: Lab, node: str) -> dict[int, int]:
    xml = lab.get(node, f'<bgp xmlns="{T}tarvos-bgp"><state><evpn><route-count/></evpn></state></bgp>')
    return {int(t): int(c) for t, c in re.findall(r"<route-type>(\d)</route-type>\s*<count>(\d+)</count>", xml)}


@case("fabric", short=True)
def link_flap(lab: Lab, r: Recorder) -> None:
    """Spine uplink to leaf1 down 5 s then up: BFD detection, reconvergence, loss."""
    with Pinger(lab, "h1", "10.1.100.12") as l2, Pinger(lab, "h2", "10.10.1.1") as l3:
        time.sleep(2)
        lab.x("spine", "ip link set eth1 down")
        down = time.monotonic()
        try:
            detect = wait("spine sees leaf1 down", lambda: not lab.established("spine", 2), timeout=30, every=0.2)
            r.measure("session down detected after", f"{detect:.1f} s (BFD, hold time 9 s)")
        except AssertionError as exc:
            r.check("link failure detected", False, str(exc))
        time.sleep(max(0, 5 - (time.monotonic() - down)))
        lab.x("spine", "ip link set eth1 up")
        try:
            up = wait("sessions back", lambda: lab.established("spine", 2), timeout=90, every=0.5)
            r.check("BGP sessions re-established", True)
            r.measure("re-established after link up", f"{up:.1f} s")
        except AssertionError as exc:
            r.check("BGP sessions re-established", False, str(exc))
        try:
            wait("h1 -> h2 again", lambda: reaches(lab, "h1", "10.1.100.12"), timeout=60)
            wait("h2 -> leaf1 RED again", lambda: reaches(lab, "h2", "10.10.1.1"), timeout=60)
            r.check("L2 and L3 traffic back", True)
        except AssertionError as exc:
            r.check("L2 and L3 traffic back", False, str(exc))
        r.measure("L2 (VNI 100) outage", f"{l2.lost_seconds():.1f} s")
        r.measure("L3 (L3VNI 5000) outage", f"{l3.lost_seconds():.1f} s")
    counts = evpn_counts_vtysh(lab, "leaf1")
    r.check("EVPN type-2/3/5 routes restored on leaf1", all(counts.get(t, 0) > 0 for t in (2, 3, 5)), str(counts))


def reboot(lab: Lab, r: Recorder, node: str, peers: int) -> None:
    lab.save(node)
    before = evpn_counts_vtysh(lab, "leaf1" if node != "leaf1" else "leaf2")
    with Pinger(lab, "h1", "10.1.100.12") as ping:
        took = lab.restart(node)
        relink(node)
        r.measure("NETCONF back after", f"{took:.0f} s")
        try:
            up = wait(f"{node} sessions back", lambda: lab.established(node, peers), timeout=120)
            r.measure("BGP back after NETCONF", f"{up:.0f} s")
            wait("h1 -> h2 again", lambda: reaches(lab, "h1", "10.1.100.12"), timeout=90)
            r.check(f"{node} back in the fabric, traffic restored", True)
        except AssertionError as exc:
            r.check(f"{node} back in the fabric, traffic restored", False, str(exc))
        r.measure("h1 -> h2 outage", f"{ping.lost_seconds():.0f} s")
    other = "leaf1" if node != "leaf1" else "leaf2"
    # MAC+IP type-2 routes come back once the hosts talk to their gateway again (README)
    for host in ("h1", "h2"):
        lab.x(host, "ping -c2 -W1 10.1.100.1", check=False)
    try:
        wait("EVPN routes restored", lambda: all(evpn_counts_vtysh(lab, other).get(t, 0) >= before.get(t, 0) for t in (2, 3, 5)),
             timeout=60)  # fmt: skip
        r.check("EVPN route counts back to their pre-reboot values", True)
    except AssertionError:
        r.check("EVPN route counts back to their pre-reboot values", False,
                f"{before} -> {evpn_counts_vtysh(lab, other)}")  # fmt: skip


@case("fabric")
def leaf_reboot(lab: Lab, r: Recorder) -> None:
    """docker restart of leaf2 with its configuration saved."""
    reboot(lab, r, "leaf2", 1)


@case("fabric")
def spine_reboot(lab: Lab, r: Recorder) -> None:
    """docker restart of the route reflector."""
    reboot(lab, r, "spine", 2)


@case("fabric")
def mac_mobility(lab: Lab, r: Recorder) -> None:
    """h2's MAC and IP move from leaf2 to leaf1 (a new host on lan1)."""
    mac = lab.x("h2", "cat /sys/class/net/eth0/address").strip()
    h3 = f"{lab.prefix}-h3"
    try:
        lab.x("h2", "ip link set eth0 down")
        sh("docker", "run", "-d", "--name", h3, "--network", f"{lab.prefix}-lan1", "--mac-address", mac,
           "--cap-add", "NET_ADMIN", "--entrypoint", "sleep", lab.image, "infinity")  # fmt: skip
        sh("docker", "exec", h3, "sh", "-c", "ip addr add 10.1.100.12/24 dev eth0 && arping -c 3 -U -I eth0 10.1.100.12 >/dev/null 2>&1; ping -c2 -W1 10.1.100.1 >/dev/null 2>&1; true")
        took = wait("h1 reaches the moved host", lambda: reaches(lab, "h1", "10.1.100.12"), timeout=60)
        r.measure("moved host reachable after", f"{took:.0f} s")
        detail = lab.vty_json("leaf2", f"show evpn mac vni 100 mac {mac} json")
        entry = next(iter(detail.values())) if detail else {}
        seq = max(int(entry.get("localSequence", 0) or 0), int(entry.get("remoteSequence", 0) or 0)) if isinstance(entry, dict) else 0
        r.check("leaf2 sees the MAC as remote (moved to leaf1)", isinstance(entry, dict) and entry.get("type") == "remote", json.dumps(entry)[:200])
        r.check("MAC mobility sequence number increased", seq >= 1, f"sequence {seq}")
    finally:
        sh("docker", "rm", "-f", h3, check=False)
        lab.x("h2", "ip link set eth0 up && ip route replace 10.10.0.0/16 via 10.1.100.1 && ping -c2 -W1 10.1.100.1 >/dev/null 2>&1; true", check=False)
    try:
        wait("h2 back on leaf2", lambda: reaches(lab, "h1", "10.1.100.12"), timeout=60)
        r.check("MAC moves back to leaf2", True)
    except AssertionError as exc:
        r.check("MAC moves back to leaf2", False, str(exc))


@case("observability", short=True)
def views_agree(lab: Lab, r: Recorder) -> None:
    """NETCONF, gNMI, Prometheus and vtysh report the same sessions and EVPN counts."""
    time.sleep(11)  # Prometheus state cache (10 s)
    for node, peers in (("spine", 2), ("leaf1", 1), ("leaf2", 1)):
        nc = lab.bgp_state(node).count("<session-state>established</session-state>")
        gnmi = lab.gnmic(node, "get", "--encoding", "json_ietf", "--path", "/bgp/state/instance[vrf=default]/neighbor")
        gnmi_up = gnmi.count('"established"')
        prom = sum(v for k, v in lab.metrics(node).items() if k.startswith("tarvos_bgp_neighbor_up{") and 'vrf="default"' in k)
        vty = sum(1 for p in lab.vty_json(node, "show bgp summary json").get("ipv4Unicast", {}).get("peers", {}).values()
                  if p.get("state") == "Established")  # fmt: skip
        r.check(f"{node}: sessions agree (NETCONF {nc}, gNMI {gnmi_up}, Prometheus {prom:.0f}, vtysh {vty})",
                nc == gnmi_up == int(prom) == vty == peers)  # fmt: skip
    for node in ("leaf1", "leaf2"):
        nc = evpn_counts_netconf(lab, node)
        vty = evpn_counts_vtysh(lab, node)
        metrics = lab.metrics(node)
        prom = {t: int(metrics.get(f'tarvos_evpn_routes{{route_type="{t}"}}', -1)) for t in range(1, 6)}
        vty_full = {t: vty.get(t, 0) for t in range(1, 6)}
        r.check(f"{node}: EVPN route counts agree (NETCONF {nc}, Prometheus {prom}, vtysh {vty_full})",
                nc == prom == vty_full)  # fmt: skip


@case("observability", short=True)
def on_change_delay(lab: Lab, r: Recorder) -> None:
    """gNMI ON_CHANGE reports a BGP session going down and up within its documented delay."""
    path = "/bgp/state/instance[vrf=default]/neighbor[address=10.0.1.2]/session-state"
    sub = subprocess.Popen(
        ["gnmic", "-a", f"{lab.ip('leaf1')}:57400", "-u", "admin", "-p", "admin", "--skip-verify",
         "subscribe", "--stream-mode", "on-change", "--format", "flat", "--path", path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    events: list[tuple[float, str]] = []

    def read() -> None:
        assert sub.stdout
        for line in sub.stdout:
            if "session-state" in line:
                events.append((time.monotonic(), line.strip().split(":", 1)[-1].strip()))

    import threading

    threading.Thread(target=read, daemon=True).start()
    nbr = f'<bgp xmlns="{T}tarvos-bgp"><instance><vrf>default</vrf><neighbor><address>10.0.1.3</address><shutdown>{{}}</shutdown></neighbor></instance></bgp>'
    try:
        wait("initial ON_CHANGE update", lambda: events, timeout=20)
        lab.edit("spine", nbr.format("true"))
        changed = time.monotonic()
        wait("leaf1 sees the session down (vtysh)", lambda: not lab.established("leaf1", 1), timeout=30, every=0.2)
        seen = time.monotonic()
        wait("ON_CHANGE reports it", lambda: any(v != "established" for _, v in events), timeout=30, every=0.2)
        delay = next(t for t, v in events if v != "established") - seen
        r.measure("down: FRR -> gNMI ON_CHANGE", f"{delay:.1f} s (session down {seen - changed:.1f} s after the shutdown)")
        r.check("ON_CHANGE within its documented delay (5 s for Sysrepo data)", delay <= 6, f"{delay:.1f} s")
        lab.edit("spine", nbr.format("false"))
        wait("leaf1 session back", lambda: lab.established("leaf1", 1), timeout=60, every=0.5)
        seen = time.monotonic()
        wait("ON_CHANGE reports established", lambda: events[-1][1] == "established", timeout=30, every=0.2)
        r.measure("up: FRR -> gNMI ON_CHANGE", f"{events[-1][0] - seen:.1f} s")
    finally:
        sub.kill()
        lab.edit("spine", nbr.format("false"))


@case("observability")
def syslog_forwarding(lab: Lab, r: Recorder) -> None:
    """Node logs reach the collector; collector down and back."""
    collector = lab.name("syslog")

    def delivered(tag: str) -> bool:
        return tag in sh("docker", "logs", collector, check=False)

    first = f"day2-{uuid.uuid4().hex[:8]}"
    lab.x("leaf1", f"logger -t day2 {first}")
    try:
        took = wait("message at the collector", lambda: delivered(first), timeout=20)
        r.check("syslog forwarded to the collector (UDP)", True)
        r.measure("delivery", f"{took:.1f} s")
    except AssertionError as exc:
        r.check("syslog forwarded to the collector (UDP)", False, str(exc))
    forwarded = sh("docker", "logs", collector, check=False)
    r.check("FRR daemon logs forwarded", "bgpd" in forwarded or "zebra" in forwarded or "watchfrr" in forwarded)
    sh("docker", "stop", collector)
    lost = f"day2-down-{uuid.uuid4().hex[:8]}"
    lab.x("leaf1", f"logger -t day2 {lost}")
    sh("docker", "start", collector)
    time.sleep(2)
    after = f"day2-back-{uuid.uuid4().hex[:8]}"
    lab.x("leaf1", f"logger -t day2 {after}")
    try:
        wait("forwarding resumes", lambda: delivered(after), timeout=20)
        r.check("forwarding resumes when the collector is back", True)
    except AssertionError as exc:
        r.check("forwarding resumes when the collector is back", False, str(exc))
    r.check("messages sent while the collector is down are kept", delivered(lost),
            known="UDP syslog: messages sent while the collector is unreachable are lost (use SYSLOG_PROTOCOL=tcp)")  # fmt: skip
    r.check("the node keeps working while the collector is down", lab.established("leaf1", 1))
    # TCP forwarding queues what it cannot send (a standalone node: SYSLOG_* are boot settings)
    from cases_access import remove, standalone

    try:
        node = standalone(lab, "logtcp", f"SYSLOG_SERVER={lab.env['SYSLOG_IP']}", "SYSLOG_PROTOCOL=tcp")
        hello = f"day2-tcp-{uuid.uuid4().hex[:8]}"
        lab.x(node, f"logger -t day2 {hello}")
        wait("TCP message delivered", lambda: delivered(hello), timeout=20)
        r.check("syslog over TCP delivered", True)
        sh("docker", "stop", collector)
        queued = f"day2-tcp-queued-{uuid.uuid4().hex[:8]}"
        lab.x(node, f"logger -t day2 {queued}")
        time.sleep(3)
        sh("docker", "start", collector)
        try:
            took = wait("queued TCP message delivered", lambda: delivered(queued), timeout=90)
            r.check("TCP: messages sent while the collector was down are delivered when it is back", True)
            r.measure("TCP queue flushed after the collector came back", f"{took:.0f} s")
        except AssertionError as exc:
            r.check("TCP: messages sent while the collector was down are delivered when it is back", False, str(exc))
    finally:
        remove(lab, "logtcp")


@case("observability")
def gnmi_operations(lab: Lab, r: Recorder) -> None:
    """20 concurrent subscribers; a subscription across an agent restart; certificates."""
    subs = []
    for _ in range(20):
        subs.append(subprocess.Popen(
            ["gnmic", "-a", f"{lab.ip('leaf1')}:57400", "-u", "admin", "-p", "admin", "--skip-verify",
             "subscribe", "--stream-mode", "sample", "--sample-interval", "1s", "--format", "flat",
             "--path", "/interfaces/interface[name=eth1]/state/counters/in-pkts"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        ))  # fmt: skip
    time.sleep(8)
    for s in subs:
        s.kill()
    updates = [s.stdout.read().count("in-pkts") if s.stdout else 0 for s in subs]
    r.check("20 concurrent SAMPLE subscribers all get updates", min(updates) >= 3, f"updates per subscriber {updates}")
    cpu = lab.metrics("leaf1").get('tarvos_process_cpu_seconds_total{process="tarvos-telemetry"}', 0)
    r.measure("agent CPU seconds after the subscribers", f"{cpu:.1f}")
    # A subscription across a restart of the telemetry agent.
    sub = subprocess.Popen(
        ["gnmic", "-a", f"{lab.ip('leaf1')}:57400", "-u", "admin", "-p", "admin", "--skip-verify",
         "--retry", "2s", "subscribe", "--stream-mode", "sample", "--sample-interval", "1s", "--format", "flat",
         "--path", "/interfaces/interface[name=eth1]/state/counters/in-pkts"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )  # fmt: skip
    time.sleep(3)
    old_pid = lab.x("leaf1", "pgrep -x tarvos-telemetr", check=False).strip()
    lab.x("leaf1", "pkill -KILL -x tarvos-telemetr", check=False)
    try:
        took = wait("agent running again (new process)",
                    lambda: lab.x("leaf1", "pgrep -x tarvos-telemetr", check=False).strip() not in ("", old_pid),
                    timeout=60)  # fmt: skip
        r.check("telemetry agent restarted after it died", True)
        r.measure("agent restarted after", f"{took:.0f} s")
        time.sleep(6)
        sub.kill()
        out = sub.stdout.read() if sub.stdout else ""
        r.check("the subscriber (gnmic --retry) receives data again", out.count("in-pkts") >= 5)
    except AssertionError as exc:
        sub.kill()
        r.check("telemetry agent restarted after it died", False, str(exc))
    # Certificate: the self-signed certificate is presented (fingerprint logged at boot).
    caps = lab.gnmic("leaf1", "capabilities")
    r.check("Capabilities over TLS", "tarvos-bgp" in caps)
    logs = sh("docker", "logs", lab.name("leaf1"), check=False) + subprocess.run(
        ["docker", "logs", lab.name("leaf1")], capture_output=True, text=True, check=False).stderr  # fmt: skip
    r.check("certificate fingerprint logged", "gNMI certificate SHA-256" in logs)


@case("observability")
def netconf_on_shutdown(lab: Lab, r: Recorder) -> None:
    """A neighbour shut down over NETCONF is visible as idle in every view."""
    nbr = f'<bgp xmlns="{T}tarvos-bgp" xmlns:nc="{NC}"><instance><vrf>default</vrf><neighbor><address>10.0.2.3</address><shutdown>{{}}</shutdown></neighbor></instance></bgp>'
    lab.edit("spine", nbr.format("true"))
    try:
        wait("spine: leaf2 not established", lambda: not lab.established("spine", 2), timeout=30)
        time.sleep(11)
        prom = lab.metrics("spine").get('tarvos_bgp_neighbor_up{neighbor="10.0.2.3",remote_as="65000",vrf="default"}')
        r.check("Prometheus shows the shut neighbour down", prom == 0, str(prom))
    finally:
        lab.edit("spine", nbr.format("false"))
        wait("spine sessions back", lambda: lab.established("spine", 2), timeout=60)


UL_NETS = {"s1l1": "fd00:0:1::/64", "s1l2": "fd00:0:2::/64", "s2l1": "fd00:0:3::/64",
           "s2l2": "fd00:0:4::/64"}  # fmt: skip


def _unnumbered_bgp(asn: int, router_id: str, ports: list[str], leaf: bool) -> str:
    """eBGP unnumbered (IPv6 link-local, RFC 8950 extended next hop) on every fabric port,
    IPv4 unicast for the loopbacks and EVPN on the same sessions."""
    neighbors = "".join(f"<neighbor><address>{p}</address><peer-group>FABRIC</peer-group></neighbor>" for p in ports)
    evpn = ("<l2vpn-evpn><advertise-all-vni>true</advertise-all-vni><vni><vni>100</vni>"
            "<route-target-import>65000:100</route-target-import><route-target-export>65000:100</route-target-export>"
            "</vni></l2vpn-evpn>") if leaf else ""  # fmt: skip
    return (f'<bgp xmlns="{T}tarvos-bgp"><instance><vrf>default</vrf><asn>{asn}</asn><router-id>{router_id}</router-id>'
            "<ebgp-requires-policy>false</ebgp-requires-policy><default-ipv4-unicast>false</default-ipv4-unicast>"
            "<bestpath-multipath-relax>true</bestpath-multipath-relax>"
            "<peer-group><name>FABRIC</name><remote-as>external</remote-as>"
            "<capability-extended-nexthop>true</capability-extended-nexthop>"
            "<timers><keepalive>3</keepalive><hold-time>9</hold-time></timers>"
            "<address-family><name>ipv4-unicast</name><activate>true</activate></address-family>"
            "<address-family><name>l2vpn-evpn</name><activate>true</activate></address-family></peer-group>"
            f"{neighbors}<ipv4-unicast><redistribute><protocol>connected</protocol><route-map>LOOPBACKS</route-map></redistribute>"
            "<maximum-paths>4</maximum-paths></ipv4-unicast>"
            f"{evpn}</instance></bgp>")  # fmt: skip


@case("fabric")
def underlay_ecmp_unnumbered(lab: Lab, r: Recorder) -> None:
    """Two spines (ECMP), eBGP underlay over IPv6 link-local (unnumbered, extended next
    hop), EVPN on the same eBGP sessions; a spine link fails under traffic."""
    from cases_access import remove

    p = f"{lab.prefix}-ul"
    nodes = {"s1": (65101, "10.255.1.1"), "s2": (65102, "10.255.1.2"),
             "l1": (65201, "10.255.2.1"), "l2": (65202, "10.255.2.2")}  # fmt: skip
    links = {"s1": ["s1l1", "s1l2"], "s2": ["s2l1", "s2l2"], "l1": ["s1l1", "s2l1", "lan1"], "l2": ["s1l2", "s2l2", "lan2"]}
    created: list[str] = []
    try:
        for name, subnet in UL_NETS.items():
            sh("docker", "network", "create", "--ipv6", "--subnet", subnet, f"{p}-{name}")
        for n in (1, 2):
            sh("docker", "network", "create", "--subnet", f"172.31.{110 + n}.0/24", f"{p}-lan{n}")
        for node in nodes:
            sh("docker", "run", "-d", "--name", f"{p}-{node}", "--hostname", f"ul-{node}",
               "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", "--cap-add", "SYS_ADMIN",
               "--sysctl", "net.ipv4.ip_forward=1", "--sysctl", "net.ipv6.conf.all.disable_ipv6=0",
               "--sysctl", "net.ipv6.conf.default.disable_ipv6=0", "--sysctl", "net.ipv6.conf.all.forwarding=1",
               "--sysctl", "net.ipv4.tcp_l3mdev_accept=1", "-e", "FRR_DAEMONS=bgpd", "-e", "TELEMETRY_ENABLED=no",
               lab.image)  # fmt: skip
            created.append(f"{p}-{node}")
        for node in nodes:
            wait(f"{node} ready", lambda n=node: "frr-sync] serving" in subprocess.run(
                ["docker", "logs", f"{p}-{n}"], capture_output=True, text=True, check=False).stderr
                + subprocess.run(["docker", "logs", f"{p}-{n}"], capture_output=True, text=True, check=False).stdout,
                timeout=120)  # fmt: skip
            for net in links[node]:
                sh("docker", "network", "connect", f"{p}-{net}", f"{p}-{node}")
        ips = {n: sh("docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", f"{p}-{n}").strip() for n in nodes}
        import fabric as lab_fabric

        for node, (asn, rid) in nodes.items():
            leaf = node.startswith("l")
            parts = [f'<lib xmlns="{lab_fabric.FRR}interface"><interface><name>lo</name><zebra xmlns="{lab_fabric.FRR}zebra">'
                     f"<ipv4-addrs><ip>{rid}</ip><prefix-length>32</prefix-length></ipv4-addrs></zebra></interface></lib>",
                     *lab_fabric._loopback_policy(), _unnumbered_bgp(asn, rid, ["eth1", "eth2"], leaf)]  # noqa: SLF001
            if leaf:
                parts.append(f'<vxlan xmlns="{T}tarvos-vxlan"><source-address>{rid}</source-address>'
                             "<l2vni><vni>100</vni><access-interface>eth3</access-interface></l2vni></vxlan>")  # fmt: skip
            lab_fabric.edit(ips[node], parts)
        for n in (1, 2):
            sh("docker", "run", "-d", "--name", f"{p}-h{n}", "--network", f"{p}-lan{n}", "--cap-add", "NET_ADMIN",
               "--entrypoint", "sleep", lab.image, "infinity")  # fmt: skip
            created.append(f"{p}-h{n}")
            sh("docker", "exec", f"{p}-h{n}", "ip", "addr", "add", f"10.1.200.1{n}/24", "dev", "eth0")

        def established(node: str) -> int:
            peers = json.loads(sh("docker", "exec", f"{p}-{node}", "vtysh", "-c", "show bgp summary json"))
            return sum(1 for v in peers.get("ipv4Unicast", {}).get("peers", {}).values() if v.get("state") == "Established")

        took = wait("4 unnumbered eBGP sessions", lambda: established("l1") == 2 and established("l2") == 2, timeout=120)
        r.check("eBGP unnumbered sessions over IPv6 link-local established", True)
        r.measure("underlay up after configuration", f"{took:.0f} s")
        def hops() -> list[dict]:
            route = json.loads(sh("docker", "exec", f"{p}-l1", "vtysh", "-c", "show ip route 10.255.2.2/32 json"))
            return [h for e in route.get("10.255.2.2/32", []) for h in e.get("nexthops", []) if h.get("active")]

        try:
            wait("two active next hops to leaf2's VTEP", lambda: len(hops()) == 2, timeout=60)
            r.check("leaf1 reaches leaf2's VTEP over two spines (ECMP)", True)
        except AssertionError:
            r.check("leaf1 reaches leaf2's VTEP over two spines (ECMP)", False, json.dumps(hops())[:200])
        found = hops()
        r.check("IPv4 routes over IPv6 link-local next hops (RFC 8950)",
                bool(found) and all(h.get("afi") == "ipv6" for h in found), json.dumps(found)[:200])  # fmt: skip

        def ping() -> bool:
            out = subprocess.run(["docker", "exec", f"{p}-h1", "ping", "-c1", "-W1", "10.1.200.12"],
                                 capture_output=True, text=True, check=False)  # fmt: skip
            return out.returncode == 0

        wait("h1 -> h2 over VNI 100", ping, timeout=60)
        r.check("hosts reach each other over the eBGP EVPN fabric", True)
        proc = subprocess.Popen(["docker", "exec", f"{p}-h1", "ping", "-i", "0.1", "-W", "1", "-D", "10.1.200.12"],
                                stdout=subprocess.PIPE, text=True)  # fmt: skip
        time.sleep(2)
        sh("docker", "exec", f"{p}-s1", "ip", "link", "set", "eth1", "down")
        time.sleep(6)
        sh("docker", "exec", f"{p}-h1", "pkill", "-INT", "ping", check=False)
        out = proc.communicate(timeout=10)[0]
        stamps = [float(m) for m in re.findall(r"^\[(\d+\.\d+)\]", out, re.M)]
        gap = max((b - a for a, b in zip(stamps, stamps[1:], strict=False)), default=99.0)
        r.measure("outage when a spine link fails (ECMP)", f"{gap:.1f} s")
        r.check("traffic survives a spine link failure (second spine)", gap < 10, f"{gap:.1f} s")
        sh("docker", "exec", f"{p}-s1", "ip", "link", "set", "eth1", "up")
    finally:
        for c in created + [f"{p}-{n}" for n in nodes]:
            sh("docker", "rm", "-f", c, check=False)
        for name in [*UL_NETS, "lan1", "lan2"]:
            sh("docker", "network", "rm", f"{p}-{name}", check=False)
        remove(lab, "ul-unused")


@case("observability")
def grafana_three_nodes(lab: Lab, r: Recorder) -> None:
    """The Grafana dashboard, evaluated by a real Prometheus scraping the 3 nodes: per-node
    CPU, RAM, traffic, BGP/EVPN health and QoS/CoPP series (milestone 3 'done when')."""
    import urllib.parse
    import urllib.request
    from pathlib import Path

    from cases_lifecycle import KERNEL_FEATURES

    name = f"{lab.prefix}-prometheus"
    config = lab.state / "prometheus.yml"
    targets = ", ".join(f'"{lab.ip(n)}:9273"' for n in ("spine", "leaf1", "leaf2"))
    config.write_text(f"global: {{scrape_interval: 5s}}\nscrape_configs:\n  - job_name: tarvos\n"
                      f"    static_configs: [{{targets: [{targets}]}}]\n")  # fmt: skip
    lab.edit("leaf1", KERNEL_FEATURES)  # QoS and CoPP counters to show
    try:
        sh("docker", "run", "-d", "--name", name, "-v", f"{config}:/etc/prometheus/prometheus.yml:ro",
           "prom/prometheus:v2.55.1", "--config.file=/etc/prometheus/prometheus.yml")  # fmt: skip
        prom = sh("docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", name).strip()
        lab.x("h1", "ping -c 20 -i 0.2 10.1.100.12 >/dev/null; ping -c 20 -i 0.2 10.10.2.1 >/dev/null", check=False)
        time.sleep(40)

        def query(expr: str) -> list[dict]:
            url = f"http://{prom}:9090/api/v1/query?" + urllib.parse.urlencode({"query": expr})
            with urllib.request.urlopen(url, timeout=20) as reply:  # noqa: S310
                return json.loads(reply.read())["data"]["result"]

        dashboard = json.loads((Path(__file__).resolve().parents[2] / "docs/grafana/tarvos.json").read_text())
        # Panels that are empty when nothing is wrong (tables of what is down) or that
        # need what the lab does not have (memory limit, TACACS+ servers over NETCONF).
        expected_empty = ("== 0", "memory_limit", "aaa_server", "drops_total", "netdev_errors")
        empty = []
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                expr = target["expr"].replace("$node", ".*")
                if not query(expr) and not any(k in expr for k in expected_empty):
                    empty.append(f"{panel['title']}: {expr}")
        r.check("every dashboard query returns data", not empty, "; ".join(empty[:5]))
        per_node = {
            "CPU": "rate(tarvos_container_cpu_seconds_total[1m])",
            "memory": 'tarvos_container_memory_bytes{type="usage"}',
            "throughput": 'rate(tarvos_interface_bytes_total{interface="eth1"}[1m])',
            "BGP sessions": 'sum by (instance) (tarvos_bgp_neighbors{state="established"})',
        }
        for what, expr in per_node.items():
            instances = {row["metric"].get("instance") for row in query(expr)}
            r.check(f"{what} for each of the 3 nodes", len(instances) == 3, str(sorted(instances)))
        leaves = {row["metric"]["instance"] for row in query('tarvos_evpn_routes{route_type="2"} > 0')}
        r.check("EVPN type-2 routes on both leaves", len(leaves) >= 2, str(sorted(leaves)))
        r.check("QoS and CoPP counters of leaf1", bool(query("tarvos_qos_class_packets_total"))
                and bool(query("tarvos_copp_matched_packets_total")))  # fmt: skip
        usage = sorted(float(row["value"][1]) / 2**20 for row in query(per_node["memory"]))
        r.measure("container memory per node", ", ".join(f"{u:.0f} MiB" for u in usage))
        r.check("nodes report their own memory (values differ)", len(set(usage)) == 3)
    finally:
        sh("docker", "rm", "-f", name, check=False)
        lab.edit("leaf1", f'<qos xmlns="{T}tarvos-qos" xmlns:nc="{NC}" nc:operation="remove"/>'
                 f'<nftables xmlns="{T}tarvos-nftables" xmlns:nc="{NC}" nc:operation="remove"/>'
                 f'<lldp xmlns="{T}tarvos-lldp" xmlns:nc="{NC}" nc:operation="remove"/>')  # fmt: skip
