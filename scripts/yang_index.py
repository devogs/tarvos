#!/usr/bin/env python3
"""Index the YANG schema loaded in Sysrepo for the gNMI server (tarvos-telemetry).

The gNMI server reads data from Sysrepo as RFC 7951 JSON, which does not say which
members of a list entry are its keys. This writes, for every implemented module:

* ``tops``: top-level data node name -> modules defining it (resolves gNMI paths whose
  first element has no module prefix, such as ``/bgp/state``);
* ``keys``: data path of every list (RFC 7951 member names: ``module:`` prefix on the
  top node and wherever the module changes) -> its key leaves, in order;
* ``modules``: name -> revision (gNMI Capabilities).

Usage: yang_index.py OUTPUT_JSON   (run after the modules are registered)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any

DATA = ("container", "list", "leaf", "leaf-list", "anydata", "anyxml")


def walk(node: Any, parent_path: str, parent_module: str, keys: dict[str, list[str]]) -> None:
    module = node.module().name()
    name = node.name() if module == parent_module else f"{module}:{node.name()}"
    path = f"{parent_path}/{name}"
    keyword = node.keyword()
    if keyword == "list":
        keys[path] = [k.name() for k in node.keys()]
    if keyword in ("container", "list"):
        for child in node.children():
            walk(child, path, module, keys)


def index(ctx: Any) -> dict[str, Any]:
    tops: dict[str, list[str]] = {}
    keys: dict[str, list[str]] = {}
    modules: dict[str, str] = {}
    for module in ctx:
        if not module.implemented():
            continue
        revisions = [str(r.date()) for r in module.revisions()]
        modules[module.name()] = revisions[0] if revisions else ""
        for node in module.children():
            if node.keyword() not in DATA:
                continue
            tops.setdefault(node.name(), []).append(module.name())
            walk(node, "", "", keys)
    return {"tops": tops, "keys": keys, "modules": modules}


def write(path: str, data: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".yang-index-")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, separators=(",", ":"), sort_keys=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main() -> int:
    from sysrepo import SysrepoConnection

    with SysrepoConnection() as connection, connection.get_ly_ctx() as ctx:
        data = index(ctx)
    write(sys.argv[1], data)
    count = f"{len(data['modules'])} modules, {len(data['keys'])} lists"
    print(f"[yang-index] {count}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
