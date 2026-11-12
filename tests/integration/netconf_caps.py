#!/usr/bin/env python3
"""Check that a NETCONF server advertises the given YANG modules.

Usage: netconf_caps.py HOST PORT USER PASSWORD MODULE [MODULE ...]
Exit code 0 when every module namespace is advertised, 1 otherwise.
"""

import sys

from ncclient import manager


def main() -> int:
    host, port, user, password, *modules = sys.argv[1:]
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
    missing = [
        m
        for m in modules
        if not any(f"urn:tarvos:params:xml:ns:yang:{m}" in c for c in capabilities)
    ]
    for module in missing:
        print(f"MISSING: {module}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
