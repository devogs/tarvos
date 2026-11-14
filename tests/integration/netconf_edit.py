#!/usr/bin/env python3
"""<edit-config> on running with the configuration XML read from stdin (the children
of <config>, any number of top-level elements).

Exit 0 when accepted; 3 when refused (the <rpc-error> message is printed on stdout, so
tests can check why); 1 on any other error.

Usage: netconf_edit.py HOST PORT [USER PASSWORD] < config.xml   (default admin/admin)
"""

import sys

from ncclient import manager
from ncclient.operations import RPCError

NC = "urn:ietf:params:xml:ns:netconf:base:1.0"


def main() -> int:
    host, port = sys.argv[1:3]
    user, password = (sys.argv[3:5] + ["admin", "admin"][len(sys.argv[3:5]) :])[:2]
    body = sys.stdin.read()
    with manager.connect(
        host=host,
        port=int(port),
        username=user,
        password=password,
        hostkey_verify=False,
        look_for_keys=False,
        allow_agent=False,
        timeout=60,
    ) as session:
        try:
            session.edit_config(target="running", config=f'<config xmlns="{NC}">{body}</config>')
        except RPCError as exc:
            print(exc.message or str(exc))
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
