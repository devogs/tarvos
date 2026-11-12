#!/usr/bin/env python3
"""Check that a NETCONF server serves the given YANG modules, as a controller would.

Netopeer2 advertises the ietf-yang-library capability and may omit per-module hello
capabilities, so a module counts as available when it is either advertised in the
hello or its schema can be retrieved with <get-schema> (RFC 6022).

Usage: netconf_caps.py HOST PORT USER PASSWORD MODULE [MODULE ...]
       netconf_caps.py --write-test HOST PORT USER PASSWORD
Exit code 0 when every module is available, 1 otherwise.
"""

import sys

from ncclient import manager
from ncclient.operations import RPCError

NAMESPACE_PREFIX = "urn:tarvos:params:xml:ns:yang:"


def main() -> int:
    if sys.argv[1] == "--write-test":
        return write_test(*sys.argv[2:6])
    host, port, user, password, *modules = sys.argv[1:]
    missing = []
    with manager.connect(
        host=host,
        port=int(port),
        username=user,
        password=password,
        hostkey_verify=False,
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    ) as session:
        capabilities = list(session.server_capabilities)
        print(f"{len(capabilities)} capabilities advertised")
        for module in modules:
            if any(f"{NAMESPACE_PREFIX}{module}" in c for c in capabilities):
                print(f"{module}: advertised in hello")
                continue
            try:
                schema = session.get_schema(module).xml
            except RPCError as exc:
                print(f"{module}: get-schema failed: {exc}", file=sys.stderr)
                missing.append(module)
                continue
            if f"module {module}" in schema or f"{NAMESPACE_PREFIX}{module}" in schema:
                print(f"{module}: served via get-schema")
            else:
                missing.append(module)
    for module in missing:
        print(f"MISSING: {module}", file=sys.stderr)
    return 1 if missing else 0


def write_test(host: str, port: str, user: str, password: str) -> int:
    """Prove NACM lets the user write and YANG validation runs: add then remove a QoS entry."""
    qos_ns = "urn:tarvos:params:xml:ns:yang:tarvos-qos"
    nc_ns = "urn:ietf:params:xml:ns:netconf:base:1.0"
    base = f'<qos xmlns="{qos_ns}"><interface{{op}}><name>eth9</name>{{body}}</interface></qos>'
    add = base.format(op="", body="<qdisc>fq-codel</qdisc>")
    remove = base.format(
        op=' xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" nc:operation="delete"', body=""
    )
    with manager.connect(
        host=host,
        port=int(port),
        username=user,
        password=password,
        hostkey_verify=False,
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
    ) as session:
        session.edit_config(target="running", config=f'<config xmlns="{nc_ns}">{add}</config>')
        data = session.get_config(
            source="running",
            filter=("subtree", '<qos xmlns="urn:tarvos:params:xml:ns:yang:tarvos-qos"/>'),
        ).xml
        ok = "eth9" in data
        session.edit_config(target="running", config=f'<config xmlns="{nc_ns}">{remove}</config>')
    print("write test:", "ok" if ok else "entry not visible after edit")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
