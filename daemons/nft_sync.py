#!/usr/bin/env python3
"""Sysrepo backend for tarvos-nftables: control-plane policing on the INPUT hook.

The CoPP rules live in a table owned by Tarvos, ``inet tarvos`` (chain ``copp``, hook
input, policy accept); nothing else in nftables is touched. Every change replaces the
whole table in one ``nft -f`` transaction, so a rule set is either fully installed or
not at all (and counters restart from zero).

Each rule carries a counter and the comment ``tarvos:<name>``; a rate-limit rule is two
kernel rules (``limit rate over ... drop``, comment ``tarvos:<name>:excess``, then
``accept``). The counters are served as operational data.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("SCRIPTS_DIR", "/opt/tarvos/scripts"))

from tarvos_backend import BackendError, serve  # noqa: E402

MODULE = "tarvos-nftables"
TABLE = ("inet", "tarvos")
CHAIN = "copp"
COMMENT = "tarvos:"
EXCESS = ":excess"
# IP protocol numbers: names would need /etc/protocols, absent from slim images.
PROTOCOLS = {"tcp": 6, "udp": 17, "icmp": 1, "icmpv6": 58, "ospf": 89, "vrrp": 112}
MIN_BURST = 5


def _rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    return ((config.get("nftables") or {}).get("copp") or {}).get("rule") or []


def enabled(config: dict[str, Any]) -> bool:
    copp = (config.get("nftables") or {}).get("copp") or {}
    return copp.get("enabled", True) is not False and bool(_rules(config))


def match(rule: dict[str, Any]) -> str:
    """nft match expression of a rule (all inputs are YANG-validated, re-checked here)."""
    parts = []
    prefix = rule.get("source-prefix")
    if prefix:
        network = ipaddress.ip_network(prefix, strict=False)
        parts.append(f"{'ip' if network.version == 4 else 'ip6'} saddr {network}")
    protocol = rule["protocol"]
    if protocol not in PROTOCOLS:
        raise BackendError(f"rule {rule['name']}: unsupported protocol {protocol}")
    parts.append(f"meta l4proto {PROTOCOLS[protocol]}")
    port = rule.get("destination-port")
    if port is not None:
        parts.append(f"th dport {int(port)}")
    return " ".join(parts)


def render_rule(rule: dict[str, Any]) -> list[str]:
    name = rule["name"]
    if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise BackendError(f"invalid rule name {name!r}")
    expression, action = match(rule), rule.get("action", "accept")
    comment = f'comment "{COMMENT}{name}"'
    if action == "rate-limit":
        pps = int(rule["max-pps"])
        burst = int(rule.get("burst") or max(MIN_BURST, pps // 10))
        return [
            f"{expression} limit rate over {pps}/second burst {burst} packets "
            f'counter drop comment "{COMMENT}{name}{EXCESS}"',
            f"{expression} counter accept {comment}",
        ]
    if action not in ("accept", "drop"):
        raise BackendError(f"rule {name}: unsupported action {action}")
    return [f"{expression} counter {action} {comment}"]


def render(config: dict[str, Any]) -> str:
    """One nft transaction: (re)create the Tarvos table, or remove it."""
    family, table = TABLE
    # "table X" first makes "delete table X" safe when the table does not exist yet.
    lines = [f"table {family} {table}", f"delete table {family} {table}"]
    if enabled(config):
        lines += [
            f"table {family} {table} {{",
            f"  chain {CHAIN} {{",
            "    type filter hook input priority filter; policy accept;",
        ]
        for rule in _rules(config):
            lines += [f"    {line}" for line in render_rule(rule)]
        lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def nft(*args: str, script: str | None = None) -> str:
    result = subprocess.run(
        ["nft", *args], input=script, capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        raise BackendError(f"nft: {result.stderr.strip() or 'failed'}")
    return result.stdout


def counters(listing: dict[str, Any]) -> dict[str, dict[str, int]]:
    """``nft -j list table inet tarvos`` -> {comment: {"packets", "bytes"}}."""
    out: dict[str, dict[str, int]] = {}
    for item in listing.get("nftables") or []:
        rule = item.get("rule")
        if not rule or rule.get("chain") != CHAIN or not rule.get("comment"):
            continue
        for expression in rule.get("expr") or []:
            counter = expression.get("counter") if isinstance(expression, dict) else None
            if isinstance(counter, dict):
                out[rule["comment"]] = {
                    "packets": int(counter.get("packets", 0)),
                    "bytes": int(counter.get("bytes", 0)),
                }
    return out


def state(config: dict[str, Any], listing: dict[str, Any] | None) -> dict[str, Any]:
    """Applied configuration + kernel counters -> /nftables/copp state."""
    hits = counters(listing or {})
    active = listing is not None and enabled(config)
    rules = []
    for rule in _rules(config):
        name, action = rule["name"], rule.get("action", "accept")
        passed = hits.get(f"{COMMENT}{name}", {"packets": 0, "bytes": 0})
        excess = hits.get(f"{COMMENT}{name}{EXCESS}", {"packets": 0, "bytes": 0})
        if action == "rate-limit":
            matched = {k: passed[k] + excess[k] for k in passed}
            dropped = excess
        else:
            matched = passed
            dropped = passed if action == "drop" else {"packets": 0, "bytes": 0}
        rule_state = {
            "matched-packets": matched["packets"],
            "matched-bytes": matched["bytes"],
            "dropped-packets": dropped["packets"],
            "dropped-bytes": dropped["bytes"],
        }
        rules.append({"name": name, "state": rule_state if active else {}})
    copp: dict[str, Any] = {"state": {"active": active, "rule-count": len(rules) if active else 0}}
    if active:
        copp["rule"] = [r for r in rules if r["state"]]
    return {"nftables": {"copp": copp}}


class NftBackend:
    module = MODULE
    xpath = f"/{MODULE}:nftables"
    name = "nft-sync"
    interval = None  # the table is ours alone: nothing to drift

    def __init__(self) -> None:
        self.oper = [
            (f"/{MODULE}:nftables/copp/state", self.copp_state),
            (f"/{MODULE}:nftables/copp/rule/state", self.copp_state),
        ]

    def apply(self, config: dict[str, Any]) -> None:
        script = render(config)
        nft("-c", "-f", "-", script=script)  # dry run: refuse before touching anything
        nft("-f", "-", script=script)

    def copp_state(self, config: dict[str, Any]) -> dict[str, Any]:
        listing = None
        if enabled(config):
            listing = json.loads(nft("-j", "list", "table", *TABLE) or "{}")
        return state(config, listing)


def main() -> int:
    return serve(NftBackend())


if __name__ == "__main__":
    sys.exit(main())
