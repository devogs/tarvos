#!/usr/bin/env python3
"""Record one PAM authentication outcome for the tarvos-aaa operational state.

Called by pam_exec from the PAM stacks rendered by aaa_configure.py:
``aaa_event.py tacacs|local success|failure``. pam_exec passes PAM_USER, PAM_SERVICE
and PAM_RHOST in the environment; the password is never exposed (no expose_authtok).
The latest outcome per method is written atomically to AAA_STATE_DIR/<method>.json, and
AAA_STATE_DIR/counts.json counts every outcome per method and result (under a lock:
logins can be concurrent). Never fails the login: every error is swallowed (the module
is optional anyway).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import tempfile
import time

STATE_DIR = os.environ.get("AAA_STATE_DIR", "/run/tarvos/aaa")
METHODS = ("tacacs", "local")
RESULTS = ("success", "failure")
_SAFE = re.compile(r"^[A-Za-z0-9_.@:%+-]{1,64}$")


def clean(value: str | None) -> str | None:
    """Only short, plain values are kept: these come from the remote client."""
    return value if value and _SAFE.match(value) else None


def event(method: str, result: str, env: dict[str, str], now: float) -> dict[str, object]:
    record: dict[str, object] = {"method": method, "result": result, "time": int(now)}
    for key, field in (("PAM_USER", "user"), ("PAM_SERVICE", "service"), ("PAM_RHOST", "rhost")):
        value = clean(env.get(key))
        if value:
            record[field] = value
    return record


def write(directory: str, method: str, record: dict[str, object]) -> None:
    os.makedirs(directory, mode=0o750, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=directory, prefix=f".{method}-")
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle)
    os.chmod(path, 0o640)
    os.replace(path, os.path.join(directory, f"{method}.json"))


def count(directory: str, method: str, result: str) -> None:
    """Increment counts.json["<method>:<result>"]."""
    os.makedirs(directory, mode=0o750, exist_ok=True)
    with open(os.path.join(directory, ".counts.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = os.path.join(directory, "counts.json")
        try:
            with open(path, encoding="utf-8") as handle:
                counts = json.load(handle)
            if not isinstance(counts, dict):
                counts = {}
        except (OSError, ValueError):
            counts = {}
        key = f"{method}:{result}"
        counts[key] = int(counts.get(key, 0)) + 1
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".counts-")
        with os.fdopen(fd, "w") as handle:
            json.dump(counts, handle)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)


def main(argv: list[str]) -> int:
    try:
        method, result = argv[1], argv[2]
        if method in METHODS and result in RESULTS and os.environ.get("PAM_TYPE") == "auth":
            write(STATE_DIR, method, event(method, result, dict(os.environ), time.time()))
            count(STATE_DIR, method, result)
    except Exception:  # noqa: S110 - must never break a login
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
