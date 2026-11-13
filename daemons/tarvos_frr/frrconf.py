"""FRR configuration text: parse ``show running-config``, merge Tarvos-owned parts.

A configuration is a list of top-level :class:`Block` objects. ``router bgp 65000 ...
exit`` is one block with body lines; ``ip prefix-list ...`` is a block without body.

Ownership: a renderer owns some top-level blocks (by header) and some interface lines.
:func:`build_target` takes the running configuration, drops what the active renderers own
and adds what they rendered. Everything else is kept verbatim, so frr-reload, which
compares the target with the running configuration, only touches Tarvos-owned
configuration and leaves CLI/mgmtd-managed parts alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

SKIP_LINES = {"Building configuration...", "Current configuration:", "end"}
CLOSERS = {"exit", "exit-vrf"}


@dataclass
class Block:
    header: str
    lines: list[str] = field(default_factory=list)
    closer: str | None = None

    def render(self) -> list[str]:
        if not self.lines and self.closer is None:
            return [self.header]
        return [self.header, *self.lines, self.closer or "exit"]


@dataclass
class Rendered:
    """Output of a renderer: owned top-level blocks and interface lines."""

    blocks: list[Block] = field(default_factory=list)
    interfaces: dict[str, list[str]] = field(default_factory=dict)
    daemons: set[str] = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return not self.blocks and not any(self.interfaces.values())

    def add_interface_line(self, name: str, line: str) -> None:
        self.interfaces.setdefault(name, []).append(line)

    def key(self) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
        """Comparable representation (to detect "nothing changed")."""
        blocks = tuple(line for block in self.blocks for line in block.render())
        interfaces = tuple(sorted((k, tuple(v)) for k, v in self.interfaces.items() if v))
        return blocks, interfaces


class Owner(Protocol):
    def owns_block(self, header: str) -> bool: ...

    def owns_interface_line(self, line: str) -> bool: ...


@dataclass
class RenderedBlocksOwner:
    """Owns exactly the given block headers, and the interface lines of ``base``."""

    base: Owner
    headers: set[str]

    def owns_block(self, header: str) -> bool:
        return header in self.headers

    def owns_interface_line(self, line: str) -> bool:
        return self.base.owns_interface_line(line)


def starts_with_words(text: str, prefix: str) -> bool:
    """True if ``text`` starts with the words of ``prefix`` (``router ospf`` does not
    match ``router ospf6 ...``)."""
    return text == prefix or text.startswith(prefix + " ")


def interface_name(header: str) -> str | None:
    words = header.split()
    if len(words) >= 2 and words[0] == "interface":
        return words[1]
    return None


def parse(text: str) -> list[Block]:
    blocks: list[Block] = []
    current: Block | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped == "!" or stripped in SKIP_LINES:
            continue
        if line.startswith(" "):
            if current is None:  # orphan body line: keep it as its own block
                blocks.append(Block(stripped))
            else:
                current.lines.append(line)
            continue
        if line in CLOSERS:
            if current is not None:
                current.closer = line
            current = None
            continue
        current = Block(line)
        blocks.append(current)
    return blocks


def to_text(blocks: Iterable[Block]) -> str:
    out: list[str] = []
    for block in blocks:
        out.extend(block.render())
        out.append("!")
    out.append("end")
    return "\n".join(out) + "\n"


def build_target(
    running: Sequence[Block], owners: Sequence[Owner], rendered: Sequence[Rendered]
) -> list[Block]:
    """Running configuration with the owned parts replaced by the rendered ones."""

    def owned_block(header: str) -> bool:
        return any(owner.owns_block(header) for owner in owners)

    def owned_line(line: str) -> bool:
        return any(owner.owns_interface_line(line) for owner in owners)

    new_interface_lines: dict[str, list[str]] = {}
    for result in rendered:
        for name, lines in result.interfaces.items():
            new_interface_lines.setdefault(name, []).extend(lines)

    target: list[Block] = []
    seen_interfaces: set[str] = set()
    for block in running:
        name = interface_name(block.header)
        if name is not None:
            seen_interfaces.add(name)
            kept = [line for line in block.lines if not owned_line(line)]
            target.append(Block(block.header, kept + new_interface_lines.get(name, []), "exit"))
        elif not owned_block(block.header):
            target.append(block)
    for name, lines in new_interface_lines.items():
        if name not in seen_interfaces and lines:
            target.append(Block(f"interface {name}", list(lines), "exit"))
    for result in rendered:
        target.extend(result.blocks)
    return target
