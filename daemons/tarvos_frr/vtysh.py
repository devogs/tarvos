"""The only place that runs FRR tools (vtysh, frr-reload). Argument lists, never a shell."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

VTYSH = os.environ.get("VTYSH", "vtysh")
FRR_RELOAD = os.environ.get("FRR_RELOAD", "/usr/lib/frr/frr-reload.py")
WORK_DIR = os.environ.get("TARVOS_RUN_DIR", "/run/tarvos")
TIMEOUT = 120
NOISE = (
    "% Configuration applied with notes:",
    "commit abort",
    "Warning: candidate datastore is not locked.",
    "Warning: running datastore is not locked.",
)


class FrrError(RuntimeError):
    """An FRR tool rejected the configuration; the message is meant for the operator."""


def _run(args: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FrrError(f"{args[0]} timed out after {timeout}s") from exc


def _clean(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip() and line.strip() not in NOISE]
    return "\n".join(lines).strip()


def vtysh(*commands: str) -> str:
    args = [VTYSH]
    for command in commands:
        args += ["-c", command]
    result = _run(args)
    if result.returncode != 0:
        raise FrrError(_clean(result.stdout + result.stderr) or f"vtysh exited {result.returncode}")
    return result.stdout


def running_config() -> str:
    return vtysh("show running-config no-header")


def running_daemons() -> set[str]:
    return set(vtysh("show daemons").split())


def mgmt_transaction(commands: list[str]) -> None:
    """Apply mgmt edits as one transaction; the candidate is reset first and on failure."""
    if not commands:
        return
    session = ["configure terminal file-lock", "mgmt commit abort", *commands, "mgmt commit apply"]
    try:
        vtysh(*session)
    except FrrError:
        try:
            vtysh("configure terminal file-lock", "mgmt commit abort")
        except FrrError:
            pass
        raise


def mgmt_get(xpath: str) -> dict[str, Any] | None:
    """Operational data from mgmtd as RFC 7951 JSON (decoded)."""
    text = vtysh(f"show mgmt get-data {xpath} json").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data or None


def _write(text: str, name: str) -> str:
    os.makedirs(WORK_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=name, suffix=".conf", dir=WORK_DIR)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    return path


def check_config(text: str) -> None:
    """vtysh dry run: syntax of every line, without touching the daemons."""
    path = _write(text, "check-")
    try:
        result = _run([VTYSH, "--dryrun", "-f", path])
    finally:
        os.unlink(path)
    if result.returncode != 0:
        raise FrrError(_clean(result.stdout + result.stderr) or "invalid FRR configuration")


def reload(text: str) -> None:
    """Make the running configuration equal to ``text`` with frr-reload (only the diff)."""
    path = _write(text, "target-")
    try:
        result = _run([FRR_RELOAD, "--reload", "--stdout", "--log-level", "error", path])
    finally:
        os.unlink(path)
    if result.returncode != 0:
        raise FrrError(_clean(result.stdout + result.stderr) or "frr-reload failed")
