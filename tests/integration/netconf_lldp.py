"""Write the LLDP tx-interval over NETCONF, read it back, then remove it.

The caller verifies that lldpd applied the value while it is set (see smoke.sh).

Usage: netconf_lldp.py set|clear HOST PORT USER PASSWORD
"""

import sys

from ncclient import manager

NS = "urn:tarvos:params:xml:ns:yang:tarvos-lldp"
NC = "urn:ietf:params:xml:ns:netconf:base:1.0"
INTERVAL = "7"


def main() -> int:
    action, host, port, user, password = sys.argv[1:6]
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
        if action == "set":
            leaf = f"<tx-interval>{INTERVAL}</tx-interval>"
        else:
            leaf = '<tx-interval xmlns:nc="%s" nc:operation="remove"/>' % NC
        session.edit_config(
            target="running",
            config=f'<config xmlns="{NC}"><lldp xmlns="{NS}">{leaf}</lldp></config>',
        )
        reply = session.get_config(
            source="running", filter=("subtree", f'<lldp xmlns="{NS}"/>')
        ).xml
        present = f"<tx-interval>{INTERVAL}</tx-interval>" in reply
        if present != (action == "set"):
            print(f"unexpected running config after {action}: {reply}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
