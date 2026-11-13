"""FRR native YANG through mgmtd: Sysrepo data -> ``mgmt edit`` transactions.

The data of the native modules is cut into *units*: the children of each module's
top-level container (``/frr-interface:lib/interface[name='eth1']``,
``/frr-routing:routing/control-plane-protocols``, ...), descending further when a unit
is too large for one vtysh command. A change replaces each modified unit and removes
each deleted one, in a single candidate/commit transaction. mgmtd computes the real
differences and hands them to its backends (zebra, staticd, ripd, ripngd).

Units never configured over NETCONF are not touched, so CLI configuration of other
interfaces, VRFs, ... keeps working next to NETCONF.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

# Modules with top-level data handled by mgmtd in FRR 10.7, and the modules that only
# augment them (they ride along inside the units). Must match the Dockerfile.
ROOT_MODULES = (
    "frr-interface",
    "frr-vrf",
    "frr-routing",
    "frr-affinity-map",
    "frr-zebra",
    "frr-ripd",
    "frr-ripngd",
)
AUGMENT_MODULES = ("frr-staticd",)
MODULES = ROOT_MODULES + AUGMENT_MODULES

# vtysh/mgmtd vty line buffer is 8192 bytes; keep room for the command words and xpath.
MAX_COMMAND = 7000
_WHITESPACE = re.compile(r"\s")
# mgmtd refuses to delete the entry of an interface or VRF that exists in the kernel
# ("only inactive interfaces can be deleted"), like the CLI does: such entries are
# cleared instead (replaced by their key alone).
_KEEP_ENTRY = re.compile(r"^/(frr-interface:lib/interface|frr-vrf:lib/vrf)\[name='([^']+)'\]$")

# Operational data served from mgmtd: (module, xpath). Subscriptions are made on the
# top-level containers: sysrepo only calls a nested subscription (.../vrf/state) for
# list entries that exist in its own tree, i.e. that are configured, while mgmtd knows
# every interface and VRF. mgmtd returns state only, augments (frr-zebra: interface
# state, RIBs) included.
OPER_XPATHS = (
    ("frr-interface", "/frr-interface:lib"),
    ("frr-vrf", "/frr-vrf:lib"),
    ("frr-ripd", "/frr-ripd:ripd"),
    ("frr-ripngd", "/frr-ripngd:ripngd"),
)


class MgmtdError(ValueError):
    """An edit that cannot be expressed as mgmtd commands."""


class DataNode(Protocol):  # the subset of libyang.DNode used here
    def path(self) -> str: ...

    def children(self) -> Iterable[DataNode]: ...

    def print_mem(self, fmt: str, **kwargs: Any) -> str: ...


def _is_container(node: Any) -> bool:
    return type(node).__name__ == "DContainer"


def encode(json_text: str) -> str:
    """Make compact JSON a single vtysh token: whitespace only occurs inside strings,
    where JSON allows the equivalent \\u escape."""
    return json_text.strip().replace(" ", "\\u0020")


def collect_units(roots: Iterable[DataNode], limit: int = MAX_COMMAND) -> dict[str, str]:
    """{xpath: compact JSON} for the units below the given top-level nodes."""
    units: dict[str, str] = {}

    def add(node: DataNode) -> None:
        text = encode(node.print_mem("json", pretty=False))
        if text in ("", "{}"):  # implicit defaults only: nothing configured
            return
        children = list(node.children()) if _is_container(node) else []
        if len(text) > limit and children:
            for child in children:
                add(child)
            return
        units[node.path()] = text

    for top in roots:
        for child in top.children():
            add(child)
    return units


def to_dict_form(value: Any) -> Any:
    """RFC 7951 JSON (as returned by mgmtd) to the dictionaries libyang-python parses:
    an ``empty`` leaf is ``[null]`` in JSON and ``None`` in a dictionary."""
    if isinstance(value, dict):
        return {key: to_dict_form(item) for key, item in value.items()}
    if isinstance(value, list):
        if value == [None]:
            return None
        return [to_dict_form(item) for item in value]
    return value


def edit_commands(old: Mapping[str, str], new: Mapping[str, str]) -> list[str]:
    """mgmt commands turning the ``old`` units into the ``new`` ones (removals first)."""
    commands: list[str] = []
    for xpath in sorted(set(old) - set(new), key=len, reverse=True):
        keep = _KEEP_ENTRY.match(xpath)
        if keep:
            module, name = keep.group(1).split(":")[0], keep.group(2)
            node = keep.group(1).rsplit("/", 1)[1]
            data = encode(json.dumps({f"{module}:{node}": [{"name": name}]}, separators=(",", ":")))
            commands.append(f"mgmt edit replace {_check_xpath(xpath)} json {data}")
        else:
            commands.append(f"mgmt edit remove {_check_xpath(xpath)}")
    for xpath, data in new.items():
        if old.get(xpath) == data:
            continue
        command = f"mgmt edit replace {_check_xpath(xpath)} json {data}"
        if len(command) > MAX_COMMAND + 1000:
            raise MgmtdError(f"{xpath}: entry is too large for one mgmtd edit")
        commands.append(command)
    return commands


def _check_xpath(xpath: str) -> str:
    if _WHITESPACE.search(xpath):
        raise MgmtdError(f"{xpath}: list keys with spaces are not supported")
    return xpath
