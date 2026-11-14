"""Day-2 cases: access control (NACM, TACACS+ ro/rw/none, password change, lock-out)
and TACACS+ failures (server down, slow, wrong secret, failover order, login rate)."""

from __future__ import annotations

import subprocess
import time

from labkit import NC, Lab, Recorder, T, case, sh, wait
from ncclient.operations import RPCError

PL = '<lib xmlns="http://frrouting.org/yang/filter"><prefix-list><type>ipv4</type><name>ACL{}</name><entry><sequence>5</sequence><action>permit</action><ipv4-prefix>192.0.2.0/24</ipv4-prefix></entry></prefix-list></lib>'


def standalone(lab: Lab, name: str, *env: str) -> str:
    """A node outside the fabric (environment variables need a new container)."""
    full = f"{lab.prefix}-{name}"
    sh("docker", "rm", "-f", full, check=False)
    args = ["docker", "run", "-d", "--name", full, "--hostname", name, "--cap-add", "NET_ADMIN",
            "--cap-add", "NET_RAW", "--cap-add", "SYS_ADMIN", "--cap-add", "BPF", "--cap-add", "PERFMON",
            "--sysctl", "net.ipv4.tcp_l3mdev_accept=1", "--sysctl", "net.ipv4.udp_l3mdev_accept=1",
            "-e", "FRR_DAEMONS=bgpd", "-e", f"TACACS_SERVER={lab.env['TACACS_IP']}",
            "-e", f"TACACS_SECRET={lab.env['TACACS_KEY']}"]  # fmt: skip
    for item in env:
        args += ["-e", item]
    sh(*args, lab.image)
    lab.env[f"{name.upper()}_IP"] = sh("docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", full).strip()

    def ready() -> bool:
        out = subprocess.run(["docker", "logs", full], capture_output=True, text=True, check=False)
        return "frr-sync] serving" in out.stdout + out.stderr and "aaa-sync] serving" in out.stdout + out.stderr

    wait(f"{name} ready", ready, timeout=120)
    time.sleep(2)
    return name


def remove(lab: Lab, name: str) -> None:
    sh("docker", "rm", "-f", f"{lab.prefix}-{name}", check=False)


def netconf_ok(lab: Lab, node: str, user: str, password: str) -> bool:
    try:
        with lab.nc(node, user, password, timeout=30) as m:
            m.get_config(source="running", filter=("subtree", f'<vrfs xmlns="{T}tarvos-vrf"/>'))
        return True
    except Exception:  # noqa: BLE001 - authentication or access failure
        return False


def users_file(lab: Lab, content: str) -> None:
    (lab.state / "tacacs-users").write_text(content)


@case("access")
def access_control(lab: Lab, r: Recorder) -> None:
    """admin full access; TACACS+ users ro (default), rw, none; user removed while
    connected; ADMIN_PASSWORD change; local fallback disabled with TACACS+ down."""
    node = "leaf1"
    r.check("TACACS+ user alice logs in over SSH", lab.ssh(node, "id -un", "alice", "alicepw").returncode == 0)
    r.check("alice reads over NETCONF (ro)", netconf_ok(lab, node, "alice", "alicepw"))
    try:
        lab.edit(node, PL.format(1), user="alice", password="alicepw")
        r.check("alice cannot write over NETCONF (ro)", False, "edit accepted")
    except RPCError as exc:
        r.check("alice cannot write over NETCONF (ro)", "denied" in str(exc).lower(), str(exc))
    r.check("alice reads over gNMI", "tarvos-bgp" in lab.gnmic(node, "capabilities", user="alice", password="alicepw", check=False))
    r.check("wrong password refused", lab.ssh(node, "true", "alice", "nope").returncode != 0)
    r.check("unknown user refused", lab.ssh(node, "true", "mallory", "x").returncode != 0)

    # alice removed from TACACS+ while connected
    session = subprocess.Popen(
        ["sshpass", "-p", "alicepw", "ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR", f"alice@{lab.ip(node)}", "sleep 8; echo still-here"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    time.sleep(3)
    users_file(lab, "bob:bobpw\n")
    try:
        r.check("removed user cannot open a new session", lab.ssh(node, "true", "alice", "alicepw").returncode != 0)
        out, _ = session.communicate(timeout=30)
        r.check("an open session of a removed user is not cut", "still-here" in out,
                known="TACACS+ is consulted at login only; existing sessions keep running (close them with pkill -u tacacs)")  # fmt: skip
    finally:
        users_file(lab, "alice:alicepw\nbob:bobpw\n")

    # TACACS_NETCONF_ACCESS=rw and an ADMIN_PASSWORD change (new containers)
    try:
        acl = standalone(lab, "aclrw", "TACACS_NETCONF_ACCESS=rw", "ADMIN_PASSWORD=Day2-Secret")
        r.check("ADMIN_PASSWORD applied: the old password is refused", not netconf_ok(lab, acl, "admin", "admin"))
        r.check("ADMIN_PASSWORD applied: the new password works", netconf_ok(lab, acl, "admin", "Day2-Secret"))
        try:
            lab.edit(acl, PL.format(2), user="alice", password="alicepw")
            r.check("TACACS_NETCONF_ACCESS=rw lets alice write", True)
        except RPCError as exc:
            r.check("TACACS_NETCONF_ACCESS=rw lets alice write", False, str(exc))
        # local fallback disabled, then TACACS+ down: expected lock-out, explained
        lab.edit(acl, f'<aaa xmlns="{T}tarvos-aaa"><local-fallback><enabled>false</enabled></local-fallback></aaa>',
                 user="admin", password="Day2-Secret")  # fmt: skip
        time.sleep(2)
        r.check("local fallback disabled: admin refused", lab.ssh(acl, "true", "admin", "Day2-Secret").returncode != 0)
        r.check("local fallback disabled: alice still logs in", lab.ssh(acl, "true", "alice", "alicepw").returncode == 0)
        sh("docker", "stop", lab.name("tacacs"))
        try:
            r.check("TACACS+ down + no local fallback: alice refused", lab.ssh(acl, "true", "alice", "alicepw").returncode != 0)
            r.check("TACACS+ down + no local fallback: admin refused (lock-out)",
                    lab.ssh(acl, "true", "admin", "Day2-Secret").returncode != 0)  # fmt: skip
            logs = subprocess.run(["docker", "logs", lab.name(acl)], capture_output=True, text=True, check=False)
            text = logs.stdout + logs.stderr
            r.check("each refused admin login is explained in the node log",
                    "login of admin from" in text and "local fallback is disabled" in text,
                    "no log line explains why admin is refused")  # fmt: skip
            r.check("recovery: docker exec still works", "root" in sh("docker", "exec", lab.name(acl), "id", "-un"))
        finally:
            sh("docker", "start", lab.name("tacacs"))
            time.sleep(2)
    finally:
        remove(lab, "aclrw")
    try:
        acl = standalone(lab, "aclnone", "TACACS_NETCONF_ACCESS=none")
        r.check("TACACS_NETCONF_ACCESS=none: alice cannot open a NETCONF session", not netconf_ok(lab, acl, "alice", "alicepw"))
        r.check("TACACS_NETCONF_ACCESS=none: alice keeps her shell", lab.ssh(acl, "true", "alice", "alicepw").returncode == 0)
        caps = lab.gnmic(acl, "capabilities", user="alice", password="alicepw", check=False)
        r.check("TACACS_NETCONF_ACCESS=none: alice has no gNMI", "tarvos-bgp" not in caps)
        r.check("TACACS_NETCONF_ACCESS=none: admin keeps NETCONF", netconf_ok(lab, acl, "admin", "admin"))
    finally:
        remove(lab, "aclnone")


def login_time(lab: Lab, node: str, user: str, password: str) -> tuple[bool, float]:
    start = time.monotonic()
    ok = lab.ssh(node, "true", user, password, timeout=60).returncode == 0
    return ok, time.monotonic() - start


def tacacs_logs(lab: Lab, container: str) -> str:
    out = subprocess.run(["docker", "logs", container], capture_output=True, text=True, check=False)
    return out.stdout + out.stderr


@case("access")
def tacacs_failures(lab: Lab, r: Recorder) -> None:
    """Server down, slow server, wrong secret, failover order with two live servers,
    50 logins in a minute."""
    node = "leaf2"
    tac = lab.name("tacacs")
    ok, base = login_time(lab, node, "alice", "alicepw")
    r.check("baseline TACACS+ login", ok)
    r.measure("TACACS+ login", f"{base:.1f} s")
    sh("docker", "stop", tac)
    try:
        ok, took = login_time(lab, node, "alice", "alicepw")
        r.check("server down: TACACS+ user refused", not ok)
        ok, took_admin = login_time(lab, node, "admin", "admin")
        r.check("server down: admin logs in at once (break-glass)", ok and took_admin < 5, f"{took_admin:.1f} s")
        r.measure("admin login with TACACS+ down", f"{took_admin:.1f} s")
    finally:
        sh("docker", "start", tac)
    wait("TACACS+ back", lambda: login_time(lab, node, "alice", "alicepw")[0], timeout=30)
    r.check("server back: alice logs in again", True)

    # Slow server (3 s per reply) within TACACS_TIMEOUT
    slow = f"{lab.prefix}-tacacs-slow"
    try:
        sh("docker", "run", "-d", "--name", slow, "-v", f"{lab.state}:/state", "-v",
           f"{str(lab.state.parent.parent / 'integration')}:/t:ro", "--entrypoint", "python3", lab.image,
           "/t/tacacs_server.py", "--key", lab.env["TACACS_KEY"], "--users-file", "/state/tacacs-users",
           "--delay", "2")  # fmt: skip
        slow_ip = sh("docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", slow).strip()
        servers = lambda *ips: f'<aaa xmlns="{T}tarvos-aaa"><tacacs>' + "".join(  # noqa: E731
            f"<server><address>{ip}</address><secret>{lab.env['TACACS_KEY']}</secret></server>" for ip in ips) + "</tacacs></aaa>"
        lab.edit(node, servers(slow_ip))
        ok, took = login_time(lab, node, "alice", "alicepw")
        r.check("slow server (2 s per reply): login still succeeds", ok, f"{took:.1f} s")
        r.measure("login through a slow server", f"{took:.1f} s")
        # Failover order with two live servers: the first one answers.
        lab.edit(node, f'<aaa xmlns="{T}tarvos-aaa" xmlns:nc="{NC}" nc:operation="remove"/>')
        lab.edit(node, servers(lab.env["TACACS_IP"], slow_ip))
        before_first, before_second = tacacs_logs(lab, tac).count("auth alice"), tacacs_logs(lab, slow).count("auth alice")
        login_time(lab, node, "alice", "alicepw")
        first = tacacs_logs(lab, tac).count("auth alice") - before_first
        second = tacacs_logs(lab, slow).count("auth alice") - before_second
        r.check("two live servers: the first one in the list answers", first >= 1 and second == 0, f"first {first}, second {second}")
        node_log = subprocess.run(["docker", "logs", lab.name(node)], capture_output=True, text=True, check=False)
        r.check("requests to every server are obfuscated (each server has its key)",
                "no TACACS+ obfuscation" not in node_log.stdout + node_log.stderr)  # fmt: skip
        sh("docker", "stop", tac)
        try:
            before_second = tacacs_logs(lab, slow).count("auth alice")
            ok, took = login_time(lab, node, "alice", "alicepw")
            r.check("first server down: failover to the second", ok and tacacs_logs(lab, slow).count("auth alice") > before_second)
            r.measure("login with failover", f"{took:.1f} s")
        finally:
            sh("docker", "start", tac)
        # Wrong secret
        lab.edit(node, f'<aaa xmlns="{T}tarvos-aaa" xmlns:nc="{NC}" nc:operation="remove"/>')
        lab.edit(node, f'<aaa xmlns="{T}tarvos-aaa"><tacacs><server><address>{lab.env["TACACS_IP"]}</address><secret>wrong</secret></server></tacacs></aaa>')
        time.sleep(1)
        r.check("wrong secret: TACACS+ user refused", not login_time(lab, node, "alice", "alicepw")[0])
        r.check("wrong secret: admin still logs in", login_time(lab, node, "admin", "admin")[0])
    finally:
        lab.edit(node, f'<aaa xmlns="{T}tarvos-aaa" xmlns:nc="{NC}" nc:operation="remove"/>')
        sh("docker", "rm", "-f", slow, check=False)
    wait("environment servers back", lambda: login_time(lab, node, "alice", "alicepw")[0], timeout=30)
    # 50 logins in a minute
    start = time.monotonic()
    results = [login_time(lab, node, "alice", "alicepw")[0] for _ in range(50)]
    took = time.monotonic() - start
    r.check("50 TACACS+ logins succeed", all(results), f"{results.count(False)} failed")
    r.measure("50 logins", f"{took:.0f} s ({50 / took * 60:.0f} per minute)")
    r.check("50 logins within a minute", took <= 60, f"{took:.0f} s")
