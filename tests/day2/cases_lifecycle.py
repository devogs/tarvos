"""Day-2 cases: configuration lifecycle (operations, datastores, locks, candidate,
persistence, changes in place, scale, leaks, concurrency, upgrade)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from labkit import FRR, LAB, NC, Lab, Pinger, Recorder, T, case, fabric, sh, wait
from ncclient.operations import RPCError
from ncclient.xml_ import to_ele

PL = f'xmlns="{FRR}filter" xmlns:nc="{NC}"'


def prefix_list(name: str, entries: list[tuple[int, str]], op: str = "") -> str:
    attr = f' nc:operation="{op}"' if op else ""
    body = "".join(
        f"<entry><sequence>{seq}</sequence><action>permit</action><ipv4-prefix>{prefix}</ipv4-prefix></entry>"
        for seq, prefix in entries
    )
    return f"<lib {PL}><prefix-list{attr}><type>ipv4</type><name>{name}</name>{body}</prefix-list></lib>"


def relink(node: str) -> None:
    sh("bash", str(LAB / "lab.sh"), "relink", node)


def reaches(lab: Lab, host: str, target: str) -> bool:
    return " 0% packet loss" in lab.x(host, f"ping -c1 -W1 {target}", check=False)


def frr_has(lab: Lab, node: str, text: str) -> bool:
    return text in lab.vty(node, "show running-config")


@case("lifecycle", short=True)
def edit_operations(lab: Lab, r: Recorder) -> None:
    """merge, replace, create, delete, remove on a prefix-list, checked in FRR."""
    node = "leaf1"
    line = "ip prefix-list DAY2 seq {} permit {}"
    r.check("create", lab.refused(node, prefix_list("DAY2", [(10, "192.0.2.0/24")], "create")) is None)
    r.check("created in FRR", frr_has(lab, node, line.format(10, "192.0.2.0/24")))
    msg = lab.refused(node, prefix_list("DAY2", [(10, "192.0.2.0/24")], "create"))
    r.check("create of an existing entry refused (data-exists)", msg is not None and "exist" in msg.lower(), str(msg))
    r.check("merge adds an entry", lab.refused(node, prefix_list("DAY2", [(20, "198.51.100.0/24")])) is None
            and frr_has(lab, node, line.format(20, "198.51.100.0/24")))  # fmt: skip
    r.check("replace leaves only the new content",
            lab.refused(node, prefix_list("DAY2", [(30, "203.0.113.0/24")], "replace")) is None
            and frr_has(lab, node, line.format(30, "203.0.113.0/24"))
            and not frr_has(lab, node, line.format(10, "192.0.2.0/24")))  # fmt: skip
    r.check("delete", lab.refused(node, prefix_list("DAY2", [], "delete")) is None
            and not frr_has(lab, node, "prefix-list DAY2"))  # fmt: skip
    msg = lab.refused(node, prefix_list("DAY2", [], "delete"))
    r.check("delete of a missing entry refused (data-missing)",
            msg is not None and ("missing" in msg.lower() or "not exist" in msg.lower()), str(msg))  # fmt: skip
    r.check("remove of a missing entry accepted", lab.refused(node, prefix_list("DAY2", [], "remove")) is None)


@case("lifecycle", short=True)
def datastores(lab: Lab, r: Recorder) -> None:
    """copy-config, delete-config, validate, get-config of startup."""
    node = "leaf2"
    with lab.nc(node) as m:
        m.copy_config(source="running", target="startup")
        startup = m.get_config(source="startup", filter=("subtree", f'<bgp xmlns="{T}tarvos-bgp"/>')).xml
        r.check("copy-config running -> startup", "<asn>65000</asn>" in startup)
        bad = f'<config xmlns="{NC}"><vrfs xmlns="{T}tarvos-vrf"><vrf><name>BAD</name><table>254</table></vrf></vrfs></config>'
        try:
            m.validate(source=to_ele(bad))
            r.check("validate refuses an invalid configuration", False, "accepted table 254")
        except RPCError as exc:
            r.check("validate refuses an invalid configuration", True, str(exc))
        good = f'<config xmlns="{NC}"><vrfs xmlns="{T}tarvos-vrf"><vrf><name>GOOD</name><table>2000</table></vrf></vrfs></config>'
        try:
            m.validate(source=to_ele(good))
            r.check("validate accepts a valid configuration", True)
        except RPCError as exc:
            r.check("validate accepts a valid configuration", False, str(exc))
        r.check("validate changes nothing", "GOOD" not in lab.x(node, "ip -br link show type vrf"))
        m.delete_config(target="startup")
        empty = m.get_config(source="startup", filter=("subtree", f'<bgp xmlns="{T}tarvos-bgp"/>')).xml
        r.check("delete-config startup", "<asn>" not in empty)
        r.check("running untouched by delete-config", lab.established(node, 1))
        m.copy_config(source="running", target="startup")


@case("lifecycle", short=True)
def locks(lab: Lab, r: Recorder) -> None:
    """lock/unlock; a session that dies holding the lock releases it."""
    node = "leaf1"
    holder = lab.nc(node)
    holder.lock(target="running")
    msg = lab.refused(node, prefix_list("LOCKED", [(5, "192.0.2.0/24")]))
    r.check("edit refused while another session holds the lock", msg is not None and "lock" in msg.lower(), str(msg))
    holder.unlock(target="running")
    r.check("edit accepted after unlock", lab.refused(node, prefix_list("LOCKED", [(5, "192.0.2.0/24")])) is None)
    holder.lock(target="running")
    # The session dies without unlocking (TCP connection dropped).
    holder._session._transport.close()  # noqa: SLF001
    try:
        took = wait("lock released after the session died",
                    lambda: lab.refused(node, prefix_list("LOCKED", [], "remove")) is None, timeout=60)  # fmt: skip
        r.check("lock of a dead session released", True)
        r.measure("lock released after", f"{took:.1f} s")
    except AssertionError as exc:
        r.check("lock of a dead session released", False, str(exc))


@case("lifecycle", short=True)
def candidate_and_confirmed_commit(lab: Lab, r: Recorder) -> None:
    """candidate, discard-changes, commit, confirmed commit with timeout and rollback."""
    node = "leaf1"
    rule = "ip prefix-list CAND seq 5 permit 192.0.2.0/24"
    with lab.nc(node) as m:
        m.edit_config(target="candidate", config=f'<config xmlns="{NC}">{prefix_list("CAND", [(5, "192.0.2.0/24")])}</config>')
        r.check("candidate edit not applied before commit", not frr_has(lab, node, rule))
        m.discard_changes()
        m.commit()
        r.check("discard-changes drops the candidate", not frr_has(lab, node, rule))
        m.edit_config(target="candidate", config=f'<config xmlns="{NC}">{prefix_list("CAND", [(5, "192.0.2.0/24")])}</config>')
        m.commit()
        r.check("commit applies the candidate to FRR", frr_has(lab, node, rule))
        m.edit_config(target="candidate", config=f'<config xmlns="{NC}">{prefix_list("CAND", [], "remove")}</config>')
        m.commit(confirmed=True, timeout="10")
        r.check("confirmed commit applied at once", not frr_has(lab, node, rule))
    # Not confirmed within 10 s: rolled back to the previous running configuration.
    try:
        took = wait("confirmed commit rolled back", lambda: frr_has(lab, node, rule), timeout=40)
        r.check("unconfirmed commit rolled back (running and FRR)", True)
        r.measure("rollback after", f"{took:.0f} s (timeout 10 s)")
    except AssertionError as exc:
        r.check("unconfirmed commit rolled back (running and FRR)", False, str(exc))
    with lab.nc(node) as m:
        m.edit_config(target="candidate", config=f'<config xmlns="{NC}">{prefix_list("CAND", [], "remove")}</config>')
        m.commit(confirmed=True, timeout="30")
        m.commit()  # confirm
    time.sleep(35)
    r.check("confirmed commit kept after the timeout", not frr_has(lab, node, rule))


KERNEL_FEATURES = f"""
<qos xmlns="{T}tarvos-qos"><interface><name>eth1</name><qdisc>htb</qdisc><bandwidth>1000000</bandwidth>
  <class><id>1</id><dscp>46</dscp><priority>0</priority><rate>100000</rate></class></interface></qos>
<nftables xmlns="{T}tarvos-nftables"><copp>
  <rule><name>bgp</name><protocol>tcp</protocol><destination-port>179</destination-port></rule>
  <rule><name>icmp</name><protocol>icmp</protocol><action>rate-limit</action><max-pps>50</max-pps></rule>
</copp></nftables>
<lldp xmlns="{T}tarvos-lldp"><tx-interval>9</tx-interval></lldp>"""


def kernel_features_applied(lab: Lab, node: str) -> dict[str, bool]:
    return {
        "QoS (htb on eth1)": "htb" in lab.x(node, "tc qdisc show dev eth1"),
        "CoPP (nft table inet tarvos)": "tarvos:icmp" in lab.x(node, "nft list table inet tarvos", check=False),
        "AAA (TACACS+ server in PAM)": lab.env["TACACS_IP"] in lab.x(node, "cat /etc/pam.d/sshd"),
        "LLDP (tx-interval 9)": "Transmit delay: 9" in lab.x(node, "lldpcli show running-configuration", check=False).replace("  ", " "),
        "FRR (router bgp)": "router bgp 65000" in lab.vty(node, "show running-config"),
        "kernel devices (vx100, RED)": "vx100" in lab.x(node, "ip -br link") and "RED" in lab.x(node, "ip -br link"),
    }


@case("lifecycle", short=True)
def persistence(lab: Lab, r: Recorder) -> None:
    """copy-config to startup, docker restart: everything re-applied, sessions back."""
    node = "leaf1"
    aaa = f'<aaa xmlns="{T}tarvos-aaa"><tacacs><server><address>{lab.env["TACACS_IP"]}</address><secret>{lab.env["TACACS_KEY"]}</secret></server></tacacs></aaa>'
    lab.edit(node, KERNEL_FEATURES + aaa)
    before = kernel_features_applied(lab, node)
    r.check("features applied before the restart", all(before.values()), str(before))
    lab.save(node)
    with Pinger(lab, "h1", "10.1.100.12") as ping:
        took = lab.restart(node)
        relink(node)
        r.measure("NETCONF back after", f"{took:.0f} s")
        try:
            up = wait("BGP established after restart", lambda: lab.established(node, 1), timeout=120)
            r.check("BGP session back", True)
            r.measure("BGP established after NETCONF", f"{up:.0f} s")
        except AssertionError as exc:
            r.check("BGP session back", False, str(exc))
        wait("h1 reaches h2 again", lambda: reaches(lab, "h1", "10.1.100.12"), timeout=90)
        r.measure("h1->h2 outage", f"{ping.lost_seconds():.0f} s")
    after = wait_features(lab, node)
    for feature, ok in after.items():
        r.check(f"re-applied after restart: {feature}", ok)
    # back to the fabric configuration only (startup too)
    lab.edit(node, f'<qos xmlns="{T}tarvos-qos" xmlns:nc="{NC}" nc:operation="remove"/>'
             f'<nftables xmlns="{T}tarvos-nftables" xmlns:nc="{NC}" nc:operation="remove"/>'
             f'<lldp xmlns="{T}tarvos-lldp" xmlns:nc="{NC}" nc:operation="remove"/>'
             f'<aaa xmlns="{T}tarvos-aaa" xmlns:nc="{NC}" nc:operation="remove"/>')  # fmt: skip
    lab.save(node)


def wait_features(lab: Lab, node: str, timeout: float = 60) -> dict[str, bool]:
    deadline = time.monotonic() + timeout
    state = kernel_features_applied(lab, node)
    while not all(state.values()) and time.monotonic() < deadline:
        time.sleep(2)
        state = kernel_features_applied(lab, node)
    return state


@case("lifecycle")
def restart_without_saving(lab: Lab, r: Recorder) -> None:
    """Unsaved changes are gone after a restart; the node comes back clean (= startup)."""
    node = "leaf2"
    lab.save(node)  # fabric configuration in startup
    lab.edit(node, KERNEL_FEATURES)
    lab.restart(node)
    relink(node)
    wait("leaf2 BGP back", lambda: lab.established(node, 1), timeout=120)
    time.sleep(5)
    state = kernel_features_applied(lab, node)
    r.check("unsaved QoS gone (tc back to the kernel default)", not state["QoS (htb on eth1)"])
    r.check("unsaved CoPP gone", not state["CoPP (nft table inet tarvos)"])
    r.check("saved fabric configuration back", state["FRR (router bgp)"] and state["kernel devices (vx100, RED)"])
    with lab.nc(node) as m:
        running = m.get_config(source="running").xml
        startup = m.get_config(source="startup").xml
    r.check("running equals startup", _strip(running) == _strip(startup))


def _strip(xml: str) -> str:
    import re

    body = re.sub(r"<rpc-reply[^>]*>|</rpc-reply>|message-id=\"[^\"]*\"", "", xml)
    return re.sub(r"\s+", "", body)


@case("lifecycle")
def change_in_place(lab: Lab, r: Recorder) -> None:
    """ASN, router-id, neighbours, VNIs under traffic, VRF deletion, VTEP source, MTU."""
    node = "leaf2"
    bgp = f'xmlns="{T}tarvos-bgp" xmlns:nc="{NC}"'
    # ASN: the instance key is the VRF; changing the ASN of a running instance
    msg = lab.refused(node, f'<bgp {bgp}><instance><vrf>default</vrf><asn>65001</asn></instance></bgp>')
    r.check("ASN change of a running instance refused with a clear error", msg is not None and "as" in (msg or "").lower(),
            str(msg) if msg else "accepted")  # fmt: skip
    if msg is None:
        lab.edit(node, f'<bgp {bgp}><instance><vrf>default</vrf><asn>65000</asn></instance></bgp>')
    # router-id
    r.check("router-id change accepted", lab.refused(node, f'<bgp {bgp}><instance><vrf>default</vrf><router-id>10.255.0.22</router-id></instance></bgp>') is None)
    r.check("router-id applied", "bgp router-id 10.255.0.22" in lab.vty(node, "show running-config"))
    lab.edit(node, f'<bgp {bgp}><instance><vrf>default</vrf><router-id>10.255.0.2</router-id></instance></bgp>')
    wait("session back after router-id change", lambda: lab.established(node, 1), timeout=60)
    # neighbour add/remove
    extra = f'<bgp {bgp}><instance><vrf>default</vrf><neighbor><address>10.0.2.6</address><remote-as>internal</remote-as></neighbor></instance></bgp>'
    r.check("neighbour added", lab.refused(node, extra) is None and "10.0.2.6" in lab.bgp_state(node))
    r.check("neighbour removed", lab.refused(node, f'<bgp {bgp}><instance><vrf>default</vrf><neighbor nc:operation="delete"><address>10.0.2.6</address></neighbor></instance></bgp>') is None
            and "10.0.2.6" not in lab.bgp_state(node))  # fmt: skip
    # VNI add/remove while VNI 100 carries traffic
    vx = f'xmlns="{T}tarvos-vxlan" xmlns:nc="{NC}"'
    with Pinger(lab, "h1", "10.1.100.12") as ping:
        time.sleep(2)
        lab.edit(node, f'<vxlan {vx}><l2vni><vni>200</vni><vrf>RED</vrf></l2vni></vxlan>')
        time.sleep(3)
        lab.edit(node, f'<vxlan {vx}><l2vni nc:operation="delete"><vni>200</vni></l2vni></vxlan>')
        time.sleep(3)
        r.check("VNI 200 added and removed", "vx200" not in lab.x(node, "ip -br link"))
        r.measure("VNI 100 outage while VNI 200 came and went", f"{ping.lost_seconds():.1f} s")
        r.check("no traffic loss on VNI 100", ping.lost_seconds() < 1.0, f"{ping.lost_seconds():.1f} s")
    # VRF that still has VNIs and interfaces
    msg = lab.refused(node, f'<vrfs xmlns="{T}tarvos-vrf" xmlns:nc="{NC}"><vrf nc:operation="delete"><name>RED</name></vrf></vrfs>')
    r.check("deleting a VRF that VNIs still use is refused", msg is not None, "accepted" if msg is None else "")
    if msg is None:
        fabric.edit(lab.ip(node), fabric.config(node))
    r.measure("VRF deletion refusal", (msg or "accepted").splitlines()[0][:160])
    # VTEP source change (a second loopback address, advertised by the LOOPBACKS policy)
    lab.edit(node, f'<lib xmlns="{FRR}interface"><interface><name>lo</name><zebra xmlns="{FRR}zebra"><ipv4-addrs>'
             f'<ip>10.255.0.12</ip><prefix-length>32</prefix-length></ipv4-addrs></zebra></interface></lib>')  # fmt: skip
    with Pinger(lab, "h1", "10.1.100.12") as ping:
        lab.edit(node, f'<vxlan {vx}><source-address>10.255.0.12</source-address></vxlan>')
        try:
            took = wait("traffic over the new VTEP", lambda: reaches(lab, "h1", "10.1.100.12"), timeout=60)
            r.check("traffic recovers after a VTEP source change", True)
            r.measure("VTEP change outage", f"{ping.lost_seconds():.1f} s (recovered after {took:.0f} s)")
        except AssertionError as exc:
            r.check("traffic recovers after a VTEP source change", False, str(exc))
    fabric.edit(lab.ip(node), [f'<vxlan {vx}><source-address>10.255.0.2</source-address></vxlan>',
                               f'<lib xmlns="{FRR}interface" xmlns:nc="{NC}"><interface><name>lo</name><zebra xmlns="{FRR}zebra">'
                               f'<ipv4-addrs nc:operation="delete"><ip>10.255.0.12</ip><prefix-length>32</prefix-length></ipv4-addrs>'
                               f'</zebra></interface></lib>'])  # fmt: skip
    # MTU
    lab.edit(node, f'<vxlan {vx}><mtu>1400</mtu></vxlan>')
    r.check("VXLAN MTU change applied", "mtu 1400" in lab.x(node, "ip link show vx100"))
    lab.edit(node, f'<vxlan {vx}><mtu nc:operation="remove"/></vxlan>')
    r.check("VXLAN MTU back to the 1450 default", "mtu 1450" in lab.x(node, "ip link show vx100"))
    wait("fabric back", lambda: reaches(lab, "h1", "10.1.100.12"), timeout=60)


@case("lifecycle")
def scale_and_leaks(lab: Lab, r: Recorder, commits: int = 1000) -> None:
    """Large configurations (time per commit) and memory after many commits."""
    node = "spine"
    timings = {}
    start = time.monotonic()
    lab.edit(node, prefix_list("SCALE", [(i * 5, f"10.{i // 250}.{i % 250}.0/24") for i in range(1, 501)]))
    timings["500 prefix-list entries"] = time.monotonic() - start
    r.check("500 prefix-list entries in FRR", lab.vty(node, "show running-config").count("prefix-list SCALE") == 500)
    entries = "".join(f"<entry><sequence>{i}</sequence><action>permit</action></entry>" for i in range(1, 201))
    start = time.monotonic()
    lab.edit(node, f'<lib xmlns="{FRR}route-map"><route-map><name>SCALE</name>{entries}</route-map></lib>')
    timings["200 route-map entries"] = time.monotonic() - start
    neighbors = "".join(
        f"<neighbor><address>10.99.{i // 200}.{i % 200 + 1}</address><remote-as>{65100 + i}</remote-as></neighbor>"
        for i in range(50)
    )
    start = time.monotonic()
    lab.edit(node, f'<bgp xmlns="{T}tarvos-bgp"><instance><vrf>default</vrf><asn>65000</asn>{neighbors}</instance></bgp>')
    timings["50 BGP neighbours"] = time.monotonic() - start
    r.check("50 neighbours in FRR", lab.vty(node, "show running-config").count("remote-as 651") == 50)
    vnis = "".join(f"<l2vni><vni>{1000 + i}</vni><vrf>RED</vrf></l2vni>" for i in range(100))
    start = time.monotonic()
    lab.edit("leaf2", f'<vxlan xmlns="{T}tarvos-vxlan">{vnis}</vxlan>')
    timings["100 L2VNIs"] = time.monotonic() - start
    r.check("100 VNIs created", lab.x("leaf2", "ip -br link show type vxlan | wc -l").strip() == "102")
    start = time.monotonic()
    lab.edit("leaf2", f'<vxlan xmlns="{T}tarvos-vxlan" xmlns:nc="{NC}">'
             + "".join(f'<l2vni nc:operation="delete"><vni>{1000 + i}</vni></l2vni>' for i in range(100)) + "</vxlan>")  # fmt: skip
    timings["remove 100 L2VNIs"] = time.monotonic() - start
    lab.edit(node, f'<bgp xmlns="{T}tarvos-bgp" xmlns:nc="{NC}"><instance><vrf>default</vrf>'
             + "".join(f'<neighbor nc:operation="delete"><address>10.99.{i // 200}.{i % 200 + 1}</address></neighbor>' for i in range(50))
             + "</instance></bgp>")  # fmt: skip
    lab.edit(node, prefix_list("SCALE", [], "delete")
             + f'<lib xmlns="{FRR}route-map" xmlns:nc="{NC}"><route-map nc:operation="delete"><name>SCALE</name></route-map></lib>')  # fmt: skip
    for what, seconds in timings.items():
        r.measure(f"commit: {what}", f"{seconds:.1f} s")
    r.check("fabric unaffected by the scale test", lab.established("spine", 2))

    # Leak check: many small commits, resident memory of every daemon before and after.
    watched = ("netopeer2-server", "sysrepo-plugind", "frr_sync", "netdev_sync", "kernel_sync",
               "bgpd", "zebra", "mgmtd", "tarvos-telemetry")  # fmt: skip
    for _ in range(20):  # warm-up: allocators and caches settle first
        lab.edit(node, prefix_list("LEAK", [(5, "192.0.2.0/24")]))
    before = lab.rss(node)
    start = time.monotonic()
    with lab.nc(node) as m:
        for i in range(commits):
            prefix = "192.0.2.0/24" if i % 2 else "198.51.100.0/24"
            m.edit_config(target="running", config=f'<config xmlns="{NC}">{prefix_list("LEAK", [(5, prefix)])}</config>')
    per_commit = (time.monotonic() - start) / commits
    lab.edit(node, prefix_list("LEAK", [], "delete"))
    time.sleep(5)
    after = lab.rss(node)
    r.measure(f"{commits} commits", f"{per_commit * 1000:.0f} ms per commit")
    for daemon in watched:
        b, a = before.get(daemon, 0), after.get(daemon, 0)
        r.measure(f"RSS {daemon}", f"{b:.1f} -> {a:.1f} MiB")
        r.check(f"no leak in {daemon} (growth < 10 % or 5 MiB)", a - b < max(5.0, 0.10 * b), f"{b:.1f} -> {a:.1f} MiB")


@case("lifecycle", short=True)
def concurrency(lab: Lab, r: Recorder) -> None:
    """20 NETCONF sessions in parallel (readers and writers), with SSH and gNMI, and a
    <get> during a long <edit-config>."""
    node = "leaf1"
    errors: list[str] = []

    def reader(i: int) -> None:
        for _ in range(3):
            if "established" not in lab.bgp_state(node):
                errors.append(f"reader {i}: no established session in state")

    def writer(i: int) -> None:
        lab.edit(node, prefix_list(f"CONC{i}", [(5, f"10.200.{i}.0/24")]))
        lab.edit(node, prefix_list(f"CONC{i}", [], "delete"))

    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = [pool.submit(reader, i) for i in range(10)] + [pool.submit(writer, i) for i in range(10)]
        futures.append(pool.submit(lambda: lab.ssh(node, "vtysh -c 'show bgp summary'")))
        futures.append(pool.submit(lambda: lab.gnmic(node, "get", "--path", "/bgp/state/evpn/route-count")))
        for f in futures:
            try:
                f.result(timeout=300)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
    r.check("20 sessions + SSH + gNMI all succeed", not errors, "; ".join(errors[:3]))
    r.check("no leftover from the writers", "CONC" not in lab.vty(node, "show running-config"))
    # A <get> while a long edit-config is running
    long_edit = prefix_list("LONG", [(i * 5, f"10.{i // 250}.{i % 250}.0/24") for i in range(1, 501)])
    done = {}

    def edit() -> None:
        start = time.monotonic()
        lab.edit(node, long_edit)
        done["edit"] = time.monotonic() - start

    thread = threading.Thread(target=edit)
    thread.start()
    time.sleep(0.5)
    start = time.monotonic()
    state = lab.bgp_state(node)
    get_time = time.monotonic() - start
    thread.join(timeout=300)
    r.check("<get> answers during a long edit-config", "established" in state)
    r.measure("<get> during a 500-entry edit", f"{get_time:.1f} s (edit took {done.get('edit', 0):.1f} s)")
    lab.edit(node, prefix_list("LONG", [], "delete"))


@case("lifecycle")
def upgrade_from_2_0(lab: Lab, r: Recorder) -> None:
    """A v2.0.0 node's configuration (with QoS, CoPP, AAA content that 2.0 accepted
    without effect) pushed unchanged to this image: accepted, now applied."""
    old, new = f"{lab.prefix}-v200", f"{lab.prefix}-upgraded"
    caps = ["--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW", "--cap-add", "SYS_ADMIN",
            "--sysctl", "net.ipv4.tcp_l3mdev_accept=1"]  # fmt: skip
    try:
        for name, image in ((old, "matt13290/tarvos:v2.0.0"), (new, lab.image)):
            sh("docker", "run", "-d", "--name", name, *caps, "-e", "FRR_DAEMONS=bgpd", image)
        for name in (old, new):
            wait(f"{name} ready", lambda n=name: "frr-sync] serving" in logs(n), timeout=120)
        ips = {n: sh("docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", n).strip() for n in (old, new)}
        config = fabric.leaf(1)[2:4] + [KERNEL_FEATURES.replace("eth1", "eth0")]
        fabric.edit(ips[old], config)
        from ncclient import manager

        with manager.connect(host=ips[old], port=830, username="admin", password="admin", hostkey_verify=False,
                             look_for_keys=False, allow_agent=False, timeout=60) as m:  # fmt: skip
            exported = m.get_config(source="running").data_ele
        body = "".join(_xml(child) for child in exported)
        r.check("v2.0.0 accepted the configuration", "tarvos-qos" in body)
        with manager.connect(host=ips[new], port=830, username="admin", password="admin", hostkey_verify=False,
                             look_for_keys=False, allow_agent=False, timeout=60) as m:  # fmt: skip
            keep = [c for c in exported if any(k in c.tag for k in ("tarvos-qos", "tarvos-nftables", "tarvos-lldp", "frrouting.org/yang/filter", "frrouting.org/yang/route-map"))]
            m.edit_config(target="running", config=f'<config xmlns="{NC}">{"".join(_xml(c) for c in keep)}</config>')
        applied_qos = "htb" in sh("docker", "exec", new, "tc", "qdisc", "show", "dev", "eth0")
        applied_nft = "tarvos:icmp" in sh("docker", "exec", new, "nft", "list", "ruleset")
        r.check("2.0 configuration accepted by this image", True)
        r.check("QoS from 2.0 now applied (README 'Upgrading from 2.0')", applied_qos)
        r.check("CoPP from 2.0 now applied", applied_nft)
        r.check("2.0 node never applied it (model only)", "htb" not in sh("docker", "exec", old, "tc", "qdisc", "show", "dev", "eth0"))
    finally:
        sh("docker", "rm", "-f", old, new, check=False)


def logs(container: str) -> str:
    import subprocess

    result = subprocess.run(["docker", "logs", container], capture_output=True, text=True, timeout=30, check=False)
    return result.stdout + result.stderr


def _xml(element) -> str:  # noqa: ANN001
    from lxml import etree

    return etree.tostring(element).decode()


@case("lifecycle", short=True)
def bgp_gap_leaves(lab: Lab, r: Recorder) -> None:
    """Leaves added by the YANG gap audit reach FRR, re-applying them changes nothing,
    and removing them restores the previous configuration."""
    node = "spine"
    bgp = f'xmlns="{T}tarvos-bgp" xmlns:nc="{NC}"'
    added = (f"<bgp {bgp}><instance><vrf>default</vrf><asn>65000</asn>"
             "<cluster-id>10.255.0.254</cluster-id><graceful-restart>true</graceful-restart>"
             "<listen-limit>20</listen-limit>"
             "<peer-group><name>DYN</name><remote-as>internal</remote-as></peer-group>"
             "<listen-range><prefix>10.0.9.0/24</prefix><peer-group>DYN</peer-group></listen-range>"
             "<neighbor><address>10.0.2.6</address><remote-as>65200</remote-as>"
             "<local-as><asn>65100</asn><no-prepend>true</no-prepend><replace-as>true</replace-as></local-as>"
             "<address-family><name>ipv4-unicast</name><activate>true</activate>"
             "<remove-private-as>true</remove-private-as><as-override>true</as-override></address-family>"
             "</neighbor></instance></bgp>")  # fmt: skip
    lab.edit(node, added)
    running = lab.vty(node, "show running-config")
    for line in ("bgp cluster-id 10.255.0.254", "bgp graceful-restart", "bgp listen limit 20",
                 "bgp listen range 10.0.9.0/24 peer-group DYN",
                 "neighbor 10.0.2.6 local-as 65100 no-prepend replace-as",
                 "neighbor 10.0.2.6 remove-private-AS", "neighbor 10.0.2.6 as-override"):  # fmt: skip
        r.check(f"FRR has '{line}'", line in running)
    uptime = lab.vty_json(node, "show bgp neighbors 10.0.1.3 json")["10.0.1.3"].get("bgpTimerUpMsec", 0)
    lab.edit(node, added)
    r.check("re-applying the same configuration changes nothing in FRR", lab.vty(node, "show running-config") == running)
    again = lab.vty_json(node, "show bgp neighbors 10.0.1.3 json")["10.0.1.3"].get("bgpTimerUpMsec", 0)
    r.check("sessions are not reset by a no-op edit", again >= uptime, f"{uptime} -> {again} ms")
    removal = (f"<bgp {bgp}><instance><vrf>default</vrf>"
               '<cluster-id nc:operation="remove"/><graceful-restart nc:operation="remove"/>'
               '<listen-limit nc:operation="remove"/>'
               '<listen-range nc:operation="remove"><prefix>10.0.9.0/24</prefix></listen-range>'
               '<neighbor nc:operation="remove"><address>10.0.2.6</address></neighbor>'
               '<peer-group nc:operation="remove"><name>DYN</name></peer-group>'
               "</instance></bgp>")  # fmt: skip
    lab.edit(node, removal)
    running = lab.vty(node, "show running-config")
    r.check("removed from FRR", "cluster-id" not in running and "listen range" not in running
            and "10.0.2.6" not in running and "graceful-restart" not in running)  # fmt: skip
    # FRR resets the sessions when the cluster ID changes (documented in the model)
    took = wait("fabric sessions established again", lambda: lab.established(node, 2), timeout=60)
    r.check("fabric sessions back after the cluster-id removal", True)
    r.measure("sessions re-established after the cluster-id change", f"{took:.0f} s")
