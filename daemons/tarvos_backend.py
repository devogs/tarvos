"""Sysrepo plumbing shared by the single-module kernel backends (aaa, qos, nftables).

A backend is a small object with:

* ``module`` / ``xpath``: its YANG module and configuration root;
* ``apply(config)``: make the system match ``config`` (the module's data as a dict,
  ``{}`` when unconfigured), raising :class:`BackendError` with an operator-facing
  message when it cannot;
* ``oper``: ``(xpath, function(config) -> dict)`` pairs serving ``config false`` data;
* ``interval``: seconds between re-applications of the running configuration (None:
  never), for systems that drift (interfaces that appear later, ...).

:func:`serve` runs one or more backends in one process (``kernel_sync.py`` hosts
aaa, qos and nftables). It applies the running configuration at start, applies every change in the
Sysrepo "change" event (an error refuses the edit with the backend's message), returns
to the running configuration on refusal or abort, and serves the operational data from
the configuration applied last. Everything that imports sysrepo stays inside
:func:`serve`, so backends and this module's logic are unit testable.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from tarvos_common import log as _log
from tarvos_common import operator_message

IN_FLIGHT_TIMEOUT = 120.0  # a lost "done" event must not stop periodic re-application


class BackendError(ValueError):
    """Configuration that cannot be applied; the message is meant for the operator."""


class Backend(Protocol):
    module: str
    xpath: str
    name: str
    interval: float | None
    oper: list[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]]

    def apply(self, config: dict[str, Any]) -> None: ...


class Runner:
    """Serialises applications and remembers the configuration applied last."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.lock = threading.Lock()
        self.config: dict[str, Any] = {}
        # A transaction between its "change" and "done"/"abort" events (monotonic start):
        # the committed datastore still holds the old data, so periodic re-application
        # waits (it would undo the change being committed).
        self.in_flight: float | None = None
        # Reads the committed running configuration (set by serve).
        self.read_running: Callable[[], dict[str, Any]] = dict

    def log(self, message: str) -> None:
        _log(self.backend.name, message)

    def apply(self, config: dict[str, Any]) -> None:
        with self.lock:
            self.backend.apply(config)
            self.config = config

    def restore(self, read_running: Callable[[], dict[str, Any]]) -> None:
        """Best effort: back to the committed running configuration."""
        try:
            self.apply(read_running())
        except Exception as exc:
            self.log(f"ERROR: restoring the running configuration: {exc}")

    def on_change(
        self, event: str, read_event: Callable[[], dict[str, Any]],
        read_running: Callable[[], dict[str, Any]],
    ) -> None:  # fmt: skip
        self.in_flight = time.monotonic() if event == "change" else None
        if event == "change":
            try:
                self.apply(read_event())
            except BackendError as exc:
                self.log(f"refused: {exc}")
                self.restore(read_running)
                raise
            except Exception as exc:
                # Not a validation error (I/O error, tc/nft crash, ...): the change may be
                # partly applied, so restore as well and refuse with a clear message.
                self.log(f"refused (internal error): {exc!r}")
                self.restore(read_running)
                raise BackendError(f"{self.backend.name}: could not apply: {exc}") from exc
            self.log("configuration applied")
        elif event == "abort":
            self.log("transaction aborted, restoring the running configuration")
            self.restore(read_running)

    def on_oper(
        self, function: Callable[[dict[str, Any]], dict[str, Any]], _xpath: str, _private: Any
    ) -> dict[str, Any] | None:
        """Operational data of ``function`` (see :func:`oper_callback`)."""
        try:
            return function(self.config) or None
        except Exception as exc:  # callbacks must not raise into the C stack
            self.log(f"ERROR: operational data: {exc}")
            return None


def oper_callback(
    runner: Runner, function: Callable[[dict[str, Any]], dict[str, Any]]
) -> Callable[[str, Any], dict[str, Any] | None]:
    """sysrepo-python only accepts a plain function taking exactly (xpath, private_data)
    (no partial, no extra defaulted parameters)."""

    def callback(xpath: str, private_data: Any) -> dict[str, Any] | None:
        return runner.on_oper(function, xpath, private_data)

    return callback


def _read(session: Any, xpath: str) -> dict[str, Any]:
    from sysrepo import SysrepoNotFoundError

    try:
        return session.get_data(xpath) or {}
    except SysrepoNotFoundError:
        return {}


def _subscribe(connection: Any, session: Any, runner: Runner) -> bool:
    """Apply the running configuration and subscribe one backend; False if its module is
    not installed."""
    from sysrepo import SysrepoCallbackFailedError, SysrepoError

    backend = runner.backend
    try:
        _read(session, backend.xpath)
    except SysrepoError as exc:
        runner.log(f"{backend.module} is not available ({exc}); backend disabled")
        return False

    def read_running() -> dict[str, Any]:
        with connection.start_session("running") as running:
            return _read(running, backend.xpath)

    def callback(event_session: Any, event: str, req_id: int, private_data: Any) -> None:
        try:
            runner.on_change(event, lambda: _read(event_session, backend.xpath), read_running)
        except BackendError as exc:
            error = SysrepoCallbackFailedError(operator_message(str(exc)))
            event_session.set_error(error)
            raise error from exc

    runner.read_running = read_running
    try:
        runner.apply(read_running())
    except BackendError as exc:
        runner.log(f"ERROR: initial configuration: {exc}")
    session.subscribe_module_change_unsafe(backend.module, None, callback)
    for xpath, function in backend.oper:
        session.subscribe_oper_data_request(backend.module, xpath, oper_callback(runner, function))
    runner.log(f"serving {backend.module}")
    return True


def serve(*backends: Backend) -> int:
    """Run ``backends`` in this process, on one Sysrepo connection (one interpreter and
    connection instead of one per backend: the image stays lean)."""
    from sysrepo import SysrepoConnection

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with SysrepoConnection() as connection, connection.start_session("running") as session:
        runners = [r for r in map(Runner, backends) if _subscribe(connection, session, r)]
        periodic = [r for r in runners if r.backend.interval]
        tick = min((r.backend.interval or 0 for r in periodic), default=3600)
        due = {id(r): time.monotonic() + (r.backend.interval or 0) for r in periodic}
        while not stop.wait(tick):
            for runner in periodic:
                if time.monotonic() < due[id(runner)]:
                    continue
                started = runner.in_flight
                if started is not None and time.monotonic() - started < IN_FLIGHT_TIMEOUT:
                    continue
                due[id(runner)] = time.monotonic() + (runner.backend.interval or 0)
                try:
                    with runner.lock:
                        runner.backend.apply(runner.read_running())
                except Exception as exc:  # keep the loop alive
                    runner.log(f"ERROR: periodic re-application: {exc}")
    return 0
