#!/usr/bin/env python3
"""Sysrepo backend for tarvos-aaa: runtime TACACS+ settings and AAA state.

Configuration overlays the .env bootstrap (TACACS_SERVER, TACACS_SECRET,
TACACS_TIMEOUT): servers configured over NETCONF replace the environment ones (each
with its own secret), an unset timeout keeps TACACS_TIMEOUT, and removing the servers
restores the environment. ``local-fallback/enabled false`` refuses the break-glass
``admin`` login; it is refused itself while no TACACS+ server is configured (lockout).
The PAM stacks are rewritten with aaa_configure.write_auth_files and take effect at the
next login.

Operational state: effective settings, TCP reachability of every server (probed in the
background every AAA_PROBE_INTERVAL seconds), the server answering next (the first
reachable one, in failover order) and the latest TACACS+ and local authentication
outcomes (recorded by aaa_event.py from the PAM stacks). Secrets are never logged.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("SCRIPTS_DIR", "/opt/tarvos/scripts"))

from aaa_configure import (  # noqa: E402
    AaaSettings,
    load_settings,
    render_pam,
    validate_secret,
    write_auth_files,
)
from aaa_event import METHODS, STATE_DIR  # noqa: E402
from tarvos_backend import BackendError, serve  # noqa: E402
from tarvos_common import ConfigError  # noqa: E402
from tarvos_common import log as _log  # noqa: E402

MODULE = "tarvos-aaa"
TACACS_PORT = 49
PROBE_INTERVAL = float(os.environ.get("AAA_PROBE_INTERVAL", "30"))


def log(message: str) -> None:
    _log("aaa-sync", message)


def effective(base: AaaSettings, config: dict[str, Any]) -> tuple[AaaSettings, str]:
    """Overlay the /aaa running configuration on the environment settings.

    Returns the settings and where the servers come from ("netconf" or "environment").
    """
    aaa = config.get("aaa") or {}
    tacacs = aaa.get("tacacs") or {}
    timeout = int(tacacs.get("timeout", base.timeout))
    fallback = (aaa.get("local-fallback") or {}).get("enabled", True) is not False
    servers = tacacs.get("server") or []
    if servers:
        try:
            addresses = tuple(str(ipaddress.ip_address(s["address"])) for s in servers)
            secrets = tuple(validate_secret(s.get("secret")) for s in servers)
        except (ValueError, ConfigError) as exc:
            raise BackendError(f"invalid TACACS+ server: {exc}") from None
        settings = AaaSettings(addresses, "", timeout, secrets, fallback)
        source = "netconf"
    else:
        settings = AaaSettings(base.servers, base.secret, timeout, (), fallback)
        source = "environment"
    if not settings.local_fallback and not settings.tacacs_enabled:
        raise BackendError(
            "local-fallback cannot be disabled without a TACACS+ server: nobody could log in"
        )
    return settings, source


def probe(address: str, timeout: float) -> tuple[bool, int | None]:
    """TCP connect to the TACACS+ port: (reachable, response time in ms)."""
    start = time.monotonic()
    try:
        with socket.create_connection((address, TACACS_PORT), timeout=timeout):
            return True, int((time.monotonic() - start) * 1000)
    except OSError:
        return False, None


def _datetime(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_events(directory: str) -> dict[str, dict[str, Any]]:
    events = {}
    for method in METHODS:
        try:
            events[method] = json.loads(Path(directory, f"{method}.json").read_text())
        except (OSError, ValueError):
            continue
    return events


def state(
    settings: AaaSettings, source: str, probes: dict[str, dict[str, Any]],
    events: dict[str, dict[str, Any]],
) -> dict[str, Any]:  # fmt: skip
    out: dict[str, Any] = {
        "tacacs-active": settings.tacacs_enabled,
        "active-servers": len(settings.servers),
        "server-source": source,
        "local-fallback-active": settings.local_fallback,
    }
    servers = []
    for priority, address in enumerate(settings.servers, start=1):
        entry: dict[str, Any] = {"address": address, "priority": priority}
        result = probes.get(address)
        if result:
            entry["reachable"] = result["reachable"]
            if result.get("rtt") is not None:
                entry["response-time"] = result["rtt"]
            entry["last-probe"] = _datetime(result["time"])
            if result["reachable"] and "active-server" not in out:
                out["active-server"] = address
        servers.append(entry)
    if servers:
        out["server"] = servers
    for method, container in (("tacacs", "last-tacacs-authentication"),
                              ("local", "last-local-authentication")):  # fmt: skip
        record = events.get(method)
        if not record or record.get("result") not in ("success", "failure"):
            continue
        last: dict[str, Any] = {"result": record["result"]}
        for key, leaf in (("user", "user"), ("service", "service"), ("rhost", "remote-host")):
            if isinstance(record.get(key), str):
                last[leaf] = record[key]
        if isinstance(record.get("time"), int):
            last["time"] = _datetime(record["time"])
        out[container] = last
    return {"aaa": {"state": out}}


class AaaBackend:
    module = MODULE
    xpath = f"/{MODULE}:aaa"
    name = "aaa-sync"
    interval = None

    def __init__(self, root: Path = Path("/"), env: dict[str, str] | None = None) -> None:
        self.root = root
        self.base = load_settings(dict(os.environ) if env is None else env)
        self.settings, self.source = self.base, "environment"
        self.rendered: str | None = None
        self.probes: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.probing = False
        self.oper = [(f"/{MODULE}:aaa/state", self.state)]

    def apply(self, config: dict[str, Any]) -> None:
        settings, source = effective(self.base, config)
        rendered = "".join(render_pam(settings, with_session=w) for w in (True, False))
        if rendered != self.rendered:
            # Forget what was rendered until the write succeeds: a failed write must not
            # make the restore that follows believe the files are already current.
            self.rendered = None
            write_auth_files(settings, self.root)
            self.rendered = rendered
            servers = ", ".join(settings.servers) or "none"
            log(f"TACACS+ servers ({source}): {servers}; local fallback "
                f"{'on' if settings.local_fallback else 'off'}")  # fmt: skip
        with self.lock:
            self.settings, self.source = settings, source
            self.probes = {a: p for a, p in self.probes.items() if a in settings.servers}
        if not self.probing:
            self.probing = True
            threading.Thread(target=self._probe_loop, daemon=True).start()

    def probe_once(self) -> None:
        with self.lock:
            servers, timeout = self.settings.servers, self.settings.timeout
        results = {}
        for address in servers:
            reachable, rtt = probe(address, timeout)
            results[address] = {"reachable": reachable, "rtt": rtt, "time": time.time()}
        with self.lock:
            self.probes.update({a: r for a, r in results.items() if a in self.settings.servers})

    def _probe_loop(self) -> None:
        while True:
            try:
                self.probe_once()
            except Exception as exc:  # keep probing
                log(f"ERROR: probing TACACS+ servers: {exc}")
            time.sleep(PROBE_INTERVAL)

    def state(self, _config: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            settings, source, probes = self.settings, self.source, dict(self.probes)
        return state(settings, source, probes, read_events(STATE_DIR))


def main() -> int:
    return serve(AaaBackend())


if __name__ == "__main__":
    sys.exit(main())
