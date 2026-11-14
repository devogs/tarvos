#!/usr/bin/env python3
"""Print the reply of a NETCONF <get> with a subtree filter.

Usage: netconf_get.py HOST PORT FILTER [USER PASSWORD]  (default user admin/admin)
"""

import sys

from ncclient import manager


def main() -> int:
    host, port, subtree = sys.argv[1:4]
    user, password = (sys.argv[4:6] + ["admin", "admin"][len(sys.argv[4:6]) :])[:2]
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
        print(session.get(filter=("subtree", subtree)).xml)
    return 0


if __name__ == "__main__":
    sys.exit(main())
