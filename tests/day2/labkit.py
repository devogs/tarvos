"""Helpers for the day-2 campaign (tests/day2/day2.py): lab access, NETCONF, gNMI,
Prometheus, vtysh, continuous ping, and result recording.

Everything runs from the test runner (host network, Docker socket); nodes are reached
on their management addresses (Docker's default bridge).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ncclient import manager
from ncclient.operations import RPCError

HERE = Path(__file__).resolve().parent
LAB = HERE.parent / "lab"
sys.path.insert(0, str(LAB))

import fabric  # noqa: E402, F401, I001 - re-exported for the cases (tests/lab/fabric.py)

NC = "urn:ietf:params:xml:ns:netconf:base:1.0"
T = "urn:tarvos:params:xml:ns:yang:"
FRR = "http://frrouting.org/yang/"
NODES = ("spine", "leaf1", "leaf2")


def sh(*args: str, check: bool = True, timeout: float = 120, input: str | None = None) -> str:
    result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout,
                            input=input, check=False)  # fmt: skip
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {result.returncode} {result.stderr.strip()}")
    return result.stdout


def wait(what: str, condition: Callable[[], Any], timeout: float = 60, every: float = 1) -> float:
    """Wait for condition() to be truthy; returns the seconds it took (raises on timeout)."""
    start = time.monotonic()
    last: Exception | None = None
    while time.monotonic() - start < timeout:
        try:
            if condition():
                return time.monotonic() - start
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last = exc
        time.sleep(every)
    raise AssertionError(f"timeout ({timeout:.0f} s): {what}" + (f" (last error: {last})" if last else ""))


class Lab:
    def __init__(self, state: Path | None = None) -> None:
        self.state = state or Path(os.environ.get("LAB_STATE", LAB / ".state"))
        env = {}
        for line in (self.state / "lab.env").read_text().splitlines():
            key, _, value = line.partition("=")
            env[key] = value
        self.env = env
        self.prefix = env["LAB_PREFIX"]
        self.image = env["LAB_IMAGE"]

    def name(self, node: str) -> str:
        return f"{self.prefix}-{node}"

    def ip(self, node: str) -> str:
        key = f"{node.upper()}_IP"
        if key in self.env:
            return self.env[key]
        return sh("docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", self.name(node)).strip()

    def refresh_ip(self, node: str) -> str:
        """After a docker restart the management address may change."""
        address = sh("docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", self.name(node)).strip()
        self.env[f"{node.upper()}_IP"] = address
        return address

    # -- shell ----------------------------------------------------------------------------
    def x(self, node: str, command: str, check: bool = True, timeout: float = 120,
          user: str | None = None) -> str:  # fmt: skip
        args = ["docker", "exec"] + (["-u", user] if user else []) + [self.name(node), "sh", "-c", command]
        return sh(*args, check=check, timeout=timeout)

    def vty(self, node: str, command: str) -> str:
        return sh("docker", "exec", self.name(node), "vtysh", "-c", command)

    def vty_json(self, node: str, command: str) -> Any:
        return json.loads(self.vty(node, command) or "{}")

    # -- NETCONF ---------------------------------------------------------------------------
    def nc(self, node: str, user: str = "admin", password: str = "admin", timeout: int = 120):
        return manager.connect(host=self.ip(node), port=830, username=user, password=password,
                               hostkey_verify=False, look_for_keys=False, allow_agent=False,
                               timeout=timeout)  # fmt: skip

    def edit(self, node: str, xml: str, target: str = "running", user: str = "admin",
             password: str = "admin", **kwargs: Any) -> None:  # fmt: skip
        with self.nc(node, user, password) as m:
            m.edit_config(target=target, config=f'<config xmlns="{NC}">{xml}</config>', **kwargs)

    def refused(self, node: str, xml: str, **kwargs: Any) -> str | None:
        """Error message if the edit is refused, None if accepted."""
        try:
            self.edit(node, xml, **kwargs)
        except RPCError as exc:
            return str(exc)
        return None

    def get(self, node: str, subtree: str, user: str = "admin", password: str = "admin") -> str:
        with self.nc(node, user, password) as m:
            return m.get(filter=("subtree", subtree)).xml

    def get_config(self, node: str, subtree: str, source: str = "running") -> str:
        with self.nc(node) as m:
            return m.get_config(source=source, filter=("subtree", subtree)).xml

    def bgp_state(self, node: str) -> str:
        return self.get(node, f'<bgp xmlns="{T}tarvos-bgp"><state><instance/></state></bgp>')

    def established(self, node: str, expected: int) -> bool:
        return self.bgp_state(node).count("<session-state>established</session-state>") >= expected

    def save(self, node: str) -> None:
        with self.nc(node) as m:
            m.copy_config(source="running", target="startup")

    # -- other interfaces ----------------------------------------------------------------
    def metrics(self, node: str) -> dict[str, float]:
        with urllib.request.urlopen(f"http://{self.ip(node)}:9273/metrics", timeout=30) as r:  # noqa: S310
            text = r.read().decode()
        out = {}
        for line in text.splitlines():
            m = re.match(r"^([a-z_]+)(\{[^}]*\})? ([0-9.e+-]+)$", line)
            if m:
                out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
        return out

    def gnmic(self, node: str, *args: str, user: str = "admin", password: str = "admin",
              timeout: float = 60, check: bool = True) -> str:  # fmt: skip
        return sh("gnmic", "-a", f"{self.ip(node)}:57400", "-u", user, "-p", password,
                  "--skip-verify", "--timeout", "30s", *args, timeout=timeout, check=check)  # fmt: skip

    def ssh(self, node: str, command: str, user: str = "admin", password: str = "admin",
            timeout: float = 30) -> subprocess.CompletedProcess[str]:  # fmt: skip
        return subprocess.run(
            ["sshpass", "-p", password, "ssh", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
             "-o", "ConnectTimeout=10", f"{user}@{self.ip(node)}", command],
            capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip

    def rss(self, node: str) -> dict[str, float]:
        """Resident memory per daemon (MiB), from the node's own metrics."""
        m = self.metrics(node)
        out = {}
        for key, value in m.items():
            hit = re.match(r'tarvos_process_resident_memory_bytes\{process="([^"]+)"\}', key)
            if hit:
                out[hit.group(1)] = value / 2**20
        return out

    def restart(self, node: str, timeout: float = 120) -> float:
        start = time.monotonic()
        sh("docker", "restart", self.name(node), timeout=timeout)
        self.refresh_ip(node)
        wait(f"{node} NETCONF back", lambda: self.get(node, f'<vrfs xmlns="{T}tarvos-vrf"/>') is not None,
             timeout=timeout)  # fmt: skip
        return time.monotonic() - start


class Pinger:
    """Continuous ping (10 per second) from a host container; loss counted over time."""

    def __init__(self, lab: Lab, host: str, target: str) -> None:
        self.lab, self.host, self.target = lab, host, target
        self.proc: subprocess.Popen[str] | None = None
        self.replies: list[float] = []
        self.sent_at: float = 0.0

    def __enter__(self) -> Pinger:
        self.proc = subprocess.Popen(
            ["docker", "exec", self.lab.name(self.host), "ping", "-i", "0.1", "-W", "1", "-D", "-O", self.target],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )  # fmt: skip
        self.sent_at = time.time()
        threading.Thread(target=self._read, daemon=True).start()
        return self

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            if "bytes from" in line:
                self.replies.append(time.time())

    def __exit__(self, *_exc: Any) -> None:
        if self.proc:
            self.lab.x(self.host, "pkill -INT ping", check=False)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def lost_seconds(self) -> float:
        """Longest interval without a reply (outage), seconds."""
        times = [self.sent_at, *self.replies, time.time()]
        return max((b - a for a, b in zip(times, times[1:], strict=False)), default=0.0)

    def loss(self) -> float:
        elapsed = time.time() - self.sent_at
        expected = max(1, int(elapsed / 0.1))
        return max(0.0, 1 - len(self.replies) / expected)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    known: str = ""  # known limitation / documented behaviour instead of a failure


@dataclass
class Result:
    area: str
    case: str
    checks: list[Check] = field(default_factory=list)
    measures: dict[str, str] = field(default_factory=dict)
    error: str = ""
    seconds: float = 0.0

    @property
    def status(self) -> str:
        if self.error or any(not c.ok and not c.known for c in self.checks):
            return "FAIL"
        if any(c.known for c in self.checks):
            return "KNOWN"
        return "PASS"


class Recorder:
    def __init__(self, result: Result) -> None:
        self.result = result

    def check(self, name: str, ok: bool, detail: str = "", known: str = "") -> bool:
        self.result.checks.append(Check(name, bool(ok), detail, known if not ok else ""))
        mark = "ok" if ok else ("KNOWN" if known else "FAIL")
        print(f"    [{mark}] {name}" + (f": {detail}" if detail and not ok else ""), flush=True)
        return bool(ok)

    def measure(self, name: str, value: str) -> None:
        self.result.measures[name] = value
        print(f"    - {name}: {value}", flush=True)


def quote(text: str) -> str:
    return shlex.quote(text)


CASES: list[tuple[str, str, bool, Callable[[Lab, Recorder], None]]] = []


def case(area: str, short: bool = False) -> Callable:
    """Register a campaign case; ``short`` cases also run in CI (day2.py --short)."""

    def register(function: Callable[[Lab, Recorder], None]) -> Callable[[Lab, Recorder], None]:
        CASES.append((area, function.__name__, short, function))
        return function

    return register
