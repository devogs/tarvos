"""Shared helpers for Tarvos boot-time configurators (logging, env parsing, validation)."""

from __future__ import annotations

import ipaddress
import json
import re
import sys


class ConfigError(ValueError):
    """Raised for invalid user-supplied configuration."""


def log(component: str, message: str) -> None:
    print(f"[{component}] {message}", file=sys.stderr, flush=True)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_MESSAGE = 2000


def operator_message(text: str) -> str:
    """An error message fit for an <rpc-error>: no ANSI colours, no control characters
    (XML 1.0 forbids them: a client cannot parse a reply that carries one), bounded."""
    text = _CONTROL.sub("", _ANSI.sub("", str(text))).strip()
    if len(text) > MAX_MESSAGE:
        text = text[: MAX_MESSAGE - 3] + "..."
    return text


def parse_ip_list(raw: str | None, name: str, max_items: int) -> list[str]:
    """Parse an env list of IP addresses into a validated, ordered, de-duplicated list.

    Accepts a JSON list (``["10.0.0.1", "10.0.0.2"]``), a bracketed list without quotes
    (``[10.0.0.1, 10.0.0.2]``) or a plain comma/space separated list.
    """
    if raw is None or not raw.strip():
        return []
    text = raw.strip()
    try:
        loaded = json.loads(text)
        items = loaded if isinstance(loaded, list) else [loaded]
    except json.JSONDecodeError:
        items = re.split(r"[,\s]+", text.strip("[]"))

    addresses: list[str] = []
    for item in items:
        candidate = str(item).strip().strip("'\"")
        if not candidate:
            continue
        try:
            address = str(ipaddress.ip_address(candidate))
        except ValueError as exc:
            raise ConfigError(f"invalid {name} entry: {candidate!r}") from exc
        if address not in addresses:
            addresses.append(address)
    if len(addresses) > max_items:
        raise ConfigError(f"at most {max_items} {name} entries are supported")
    return addresses


def parse_int(name: str, value: str | None, default: int, low: int, high: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number


def parse_choice(name: str, value: str | None, default: str, choices: tuple[str, ...]) -> str:
    result = (value or default).strip().lower()
    if result not in choices:
        raise ConfigError(f"{name} must be one of: {', '.join(choices)}")
    return result
