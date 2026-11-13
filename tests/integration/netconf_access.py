#!/usr/bin/env python3
"""Report the NETCONF access a user actually gets, as seen from ncclient (SSH "password" method).

Prints exactly one word and exits 0:
  denied  authentication failed
  none    logged in, but get-config is refused
  ro      get-config works, edit-config is refused
  rw      get-config and edit-config both work
Any other failure (connection refused, timeout...) exits 1.

Usage: netconf_access.py HOST PORT USER PASSWORD
"""

import sys

from ncclient import manager
from ncclient.operations import RPCError
from ncclient.transport.errors import AuthenticationError

NC_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"
# Harmless write: re-asserts the default value of a NACM leaf.
WRITE = (
    f'<config xmlns="{NC_NS}"><nacm xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-acm">'
    "<enable-external-groups>true</enable-external-groups></nacm></config>"
)


def probe(host: str, port: str, user: str, password: str) -> str:
    try:
        session = manager.connect(
            host=host,
            port=int(port),
            username=user,
            password=password,
            hostkey_verify=False,
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
        )
    except AuthenticationError:
        return "denied"
    with session:
        try:
            session.get_config(source="running")
        except RPCError:
            return "none"
        try:
            session.edit_config(target="running", config=WRITE)
        except RPCError:
            return "ro"
        return "rw"


def main() -> int:
    if len(sys.argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(probe(*sys.argv[1:5]))
    except Exception as exc:  # noqa: BLE001 - any non-auth failure is a test error
        print(f"error: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
