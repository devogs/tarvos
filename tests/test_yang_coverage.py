"""Every configuration leaf of a Tarvos module is read by its backend.

The v2.0.0 problem was configuration accepted and silently ignored (QoS, CoPP, AAA). A
leaf whose name never appears in its backend's source cannot be applied: it must either
be implemented, refused with an explicit error (a 'must'/'deviation' in the model), or
listed in ALLOWED below with the reason.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "daemons"
BACKENDS = {
    "tarvos-bgp": [D / "tarvos_frr/renderers/bgp.py", D / "tarvos_frr/renderers/policy.py"],
    "tarvos-ospf": [D / "tarvos_frr/renderers/ospf.py"],
    "tarvos-isis": [D / "tarvos_frr/renderers/isis.py"],
    "tarvos-frr": [D / "tarvos_frr/renderers/raw.py"],
    "tarvos-vrf": [D / "netdev_sync.py"],
    "tarvos-vxlan": [D / "netdev_sync.py"],
    "tarvos-qos": [D / "qos_sync.py"],
    "tarvos-nftables": [D / "nft_sync.py"],
    "tarvos-aaa": [D / "aaa_sync.py"],
    "tarvos-lldp": [D / "lldp_sync.py", ROOT / "scripts/lldp_configure.py"],
}
# Leaves read through a generic mechanism (list keys used by position, ...), with why.
ALLOWED: dict[str, set[str]] = {
    # read as f"{family}-route-map" next to f"{family}-unicast" (renderers/bgp.py)
    "tarvos-bgp": {"ipv4-route-map", "ipv6-route-map"},
}


def config_leaves(text: str) -> set[str]:
    """Names of the leaves and leaf-lists outside 'config false' subtrees."""
    names, depth, skip_from = set(), 0, None
    for line in text.splitlines():
        stripped = line.strip()
        if skip_from is None and stripped.startswith("config false"):
            skip_from = depth - 1  # the enclosing node is state
        m = re.match(r"(leaf|leaf-list) ([\w-]+)", stripped)
        if m and skip_from is None:
            names.add(m.group(2))
        depth += line.count("{") - line.count("}")
        if skip_from is not None and depth <= skip_from:
            skip_from = None
    return names


def test_no_configuration_leaf_is_ignored():
    missing = {}
    for module, sources in BACKENDS.items():
        yang = (ROOT / "yang" / f"{module}.yang").read_text()
        # groupings used only by state ("*-state") are not configuration
        yang = re.sub(r"grouping [\w-]*state \{.*?\n  \}\n", "", yang, flags=re.S)
        code = "".join(path.read_text() for path in sources)
        unread = {
            leaf for leaf in config_leaves(yang)
            if f'"{leaf}"' not in code and f"'{leaf}'" not in code
        } - ALLOWED.get(module, set())  # fmt: skip
        if unread:
            missing[module] = sorted(unread)
    assert missing == {}
