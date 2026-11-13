"""Common renderer interface and helpers."""

from __future__ import annotations

import ipaddress
from abc import ABC, abstractmethod
from typing import Any

from tarvos_frr.frrconf import Rendered, starts_with_words


class ConfigError(ValueError):
    """Configuration that cannot be rendered/applied; reported to the NETCONF client."""


class Renderer(ABC):
    """Turns the Sysrepo data of one YANG subtree into owned FRR configuration.

    ``xpaths`` are read from the datastore and passed to :meth:`render` as
    ``{xpath: data}`` (``SysrepoSession.get_data`` dictionaries, module prefixes
    stripped). A renderer owns the top-level blocks whose header starts with one of
    ``block_prefixes`` and the interface lines starting with one of
    ``interface_prefixes``, but only while it is active (it has, or had, configuration).
    """

    key: str = ""
    # True: own only the block headers this renderer rendered (now or at the last apply),
    # not every block matching block_prefixes (for daemons that create default blocks).
    owns_rendered_blocks_only: bool = False
    modules: tuple[str, ...] = ()
    xpaths: tuple[str, ...] = ()
    block_prefixes: tuple[str, ...] = ()
    interface_prefixes: tuple[str, ...] = ()

    def owns_block(self, header: str) -> bool:
        return any(starts_with_words(header, prefix) for prefix in self.block_prefixes)

    def owns_interface_line(self, line: str) -> bool:
        text = line.strip()
        if text.startswith("no "):
            text = text[3:]
        return any(starts_with_words(text, prefix) for prefix in self.interface_prefixes)

    @abstractmethod
    def render(self, data: dict[str, Any]) -> Rendered: ...


def root(data: dict[str, Any], xpath: str, *path: str) -> Any:
    """Descend into ``data[xpath]`` along ``path`` (missing levels give ``{}``)."""
    node: Any = data.get(xpath) or {}
    for name in path:
        node = (node or {}).get(name) or {}
    return node


def items(node: Any, name: str) -> list[dict[str, Any]]:
    """List entries of ``node[name]`` (always a list)."""
    value = (node or {}).get(name) or []
    return value if isinstance(value, list) else [value]


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def is_ipv6_prefix(value: str) -> bool:
    return ":" in value


def bool_line(value: bool | None, line: str) -> list[str]:
    """``line`` when true, ``no line`` when false, nothing when unset."""
    if value is None:
        return []
    return [line] if value else [f"no {line}"]


def flag(value: bool | None, line: str) -> list[str]:
    """``line`` when true, nothing otherwise (FRR's default is off)."""
    return [line] if value else []


def ident(value: str) -> str:
    """Identityref value without its module prefix."""
    return value.split(":", 1)[1] if ":" in value else value


def indent(lines: list[str], depth: int = 1) -> list[str]:
    pad = " " * depth
    return [pad + line for line in lines]
