#!/usr/bin/env python3
"""tarvos-frr-sync: NETCONF (Sysrepo) configuration of FRR 10.7.

* FRR native YANG of the mgmtd backends (frr-interface, frr-vrf, frr-zebra,
  frr-routing/frr-staticd, frr-ripd, frr-ripngd, frr-affinity-map) is pushed to mgmtd
  as one candidate/commit transaction; their state is served from mgmtd.
* The other daemons are configured from frr-filter, frr-route-map, frr-bgp-route-map,
  frr-bfdd, tarvos-bgp, tarvos-ospf, tarvos-isis and tarvos-frr, rendered into FRR
  configuration and applied with frr-reload.

Changes are applied and verified in the Sysrepo "change" event, so a configuration that
FRR rejects is refused over NETCONF with FRR's error message. On failure or abort, FRR
is resynchronised from the committed running datastore.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.environ.get("SCRIPTS_DIR", "/opt/tarvos/scripts"))

from tarvos_common import log as _log  # noqa: E402
from tarvos_frr import frrconf, mgmtd  # noqa: E402
from tarvos_frr import vtysh as frr_tools  # noqa: E402
from tarvos_frr.frrconf import Rendered  # noqa: E402
from tarvos_frr.renderers import ConfigError, Renderer, all_renderers  # noqa: E402

EMPTY = Rendered()


def log(message: str) -> None:
    _log("frr-sync", message)


class FrrSync:
    """Keeps FRR in line with the Sysrepo running datastore.

    Sysrepo delivers one change event per module, and an event session only shows the
    new data of *its* module (other modules still read as committed). So each event
    updates only the parts derived from its module; the others keep the state applied
    last (which already includes earlier events of the same transaction).

    ``frr`` is the :mod:`tarvos_frr.vtysh` module (injectable for tests). Sessions are
    duck-typed Sysrepo sessions (``get_data``, ``get_data_ly``).
    """

    def __init__(
        self,
        renderers: list[Renderer],
        frr: Any = frr_tools,
        read: Callable[[Any, str], dict[str, Any]] | None = None,
    ) -> None:
        self.renderers = renderers
        self.frr = frr
        self.read = read or _get_dict
        self.lock = threading.RLock()
        self.native: dict[str, dict[str, str]] = {}  # root module -> units
        self.rendered: dict[str, Rendered] = {}  # renderer key -> last applied
        self.dirty = True  # FRR state unknown: force a reload

    # -- native (mgmtd) --------------------------------------------------------------

    def native_units(self, session: Any, module: str) -> dict[str, str]:
        from sysrepo import SysrepoNotFoundError

        try:
            with session.get_data_ly(f"/{module}:*") as tree:
                if tree is None:
                    return {}
                roots = [n for n in tree.siblings() if n.module().name() == module]
                return mgmtd.collect_units(roots)
        except SysrepoNotFoundError:
            return {}

    def sync_native(self, session: Any, modules: set[str] | None = None) -> bool:
        selected = [m for m in mgmtd.ROOT_MODULES if modules is None or m in modules]
        new = {m: self.native_units(session, m) for m in selected}
        commands: list[str] = []
        for module in selected:
            commands += mgmtd.edit_commands(self.native.get(module, {}), new[module])
        if not commands:
            return False
        self.frr.mgmt_transaction(commands)
        self.native.update(new)
        return True

    # -- rendered (frr-reload) -------------------------------------------------------

    def render(self, session: Any, modules: set[str] | None = None) -> dict[str, Rendered]:
        cache: dict[str, dict[str, Any]] = {}
        results: dict[str, Rendered] = {}
        for renderer in self.renderers:
            if modules is not None and not modules & set(renderer.modules):
                results[renderer.key] = self.rendered.get(renderer.key, EMPTY)
                continue
            data = {}
            for xpath in renderer.xpaths:
                if xpath not in cache:
                    cache[xpath] = self.read(session, xpath)
                data[xpath] = cache[xpath]
            results[renderer.key] = renderer.render(data)
        return results

    def sync_rendered(self, session: Any, modules: set[str] | None = None) -> bool:
        results = self.render(session, modules)
        unchanged = all(
            results[r.key].key() == self.rendered.get(r.key, EMPTY).key() for r in self.renderers
        )
        if unchanged and not self.dirty:
            return False
        needed = set().union(*(result.daemons for result in results.values()))
        missing = needed - self.frr.running_daemons()
        if missing:
            raise ConfigError(
                f"FRR daemon(s) not running: {', '.join(sorted(missing))} "
                "(add them to FRR_DAEMONS)"
            )
        owners: list[frrconf.Owner] = []
        for r in self.renderers:
            now, before = results[r.key], self.rendered.get(r.key, EMPTY)
            if now.empty and before.empty:
                continue
            if r.owns_rendered_blocks_only:
                headers = {b.header for b in now.blocks} | {b.header for b in before.blocks}
                owners.append(frrconf.RenderedBlocksOwner(r, headers))
            else:
                owners.append(r)
        running = frrconf.parse(self.frr.running_config())
        target = frrconf.build_target(running, owners, [results[r.key] for r in self.renderers])
        text = frrconf.to_text(target)
        self.frr.check_config(text)
        # From here FRR may be partially changed: remember everything we may have put
        # there, so that a resynchronisation owns (and removes) it.
        attempted = dict(results)
        for key, value in self.rendered.items():
            if not value.empty and attempted.get(key, EMPTY).empty:
                attempted[key] = value
        self.rendered = attempted
        self.dirty = True
        self.frr.reload(text)
        self.rendered = results
        self.dirty = False
        return True

    # -- entry points ----------------------------------------------------------------

    def apply(self, session: Any, modules: set[str] | None = None) -> None:
        """Apply ``session``'s view of ``modules`` (all when None); raises on error."""
        with self.lock:
            changed = self.sync_native(session, modules)
            changed = self.sync_rendered(session, modules) or changed
            if changed:
                log("configuration applied")

    def resync(self, session: Any) -> None:
        """Best effort: bring FRR back to ``session`` (the committed running datastore)."""
        with self.lock:
            # self.native is what mgmtd holds (its transactions are atomic), so the diff
            # against the committed datastore also undoes earlier events of the
            # transaction. The rendered side is reloaded in full.
            self.dirty = True
            for step in (self.sync_native, self.sync_rendered):
                try:
                    step(session)
                except Exception as exc:  # keep going: the other path may still work
                    log(f"ERROR: resynchronisation ({step.__name__}): {exc}")

    def on_change(
        self, session: Any, module: str, event: str, open_running: Callable[[], Any]
    ) -> None:
        if event == "change":
            with self.lock:
                try:
                    self.apply(session, {module})
                except Exception as exc:
                    log(f"refused ({module}): {exc}")
                    with open_running() as running:
                        self.resync(running)
                    raise
        elif event == "abort":
            with self.lock, open_running() as running:
                log(f"transaction aborted ({module}), resynchronising")
                self.resync(running)

    def on_oper(self, xpath: str) -> dict[str, Any] | None:
        try:
            return mgmtd.to_dict_form(self.frr.mgmt_get(xpath))
        except Exception as exc:
            log(f"ERROR: reading {xpath}: {exc}")
            return None


def _get_dict(session: Any, xpath: str) -> dict[str, Any]:
    from sysrepo import SysrepoNotFoundError

    try:
        return session.get_data(xpath) or {}
    except SysrepoNotFoundError:
        return {}


def wait_for_frr(timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if "mgmtd" in frr_tools.running_daemons():
                return True
        except frr_tools.FrrError:
            pass
        time.sleep(1)
    return False


def main() -> int:
    from sysrepo import SysrepoCallbackFailedError, SysrepoConnection, SysrepoError

    if not wait_for_frr():
        log("FATAL: FRR (mgmtd) is not reachable")
        return 1
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    sync = FrrSync(all_renderers())
    with SysrepoConnection() as connection, connection.start_session("running") as session:

        def open_running() -> Any:
            return connection.start_session("running")

        def callback(event_session: Any, event: str, req_id: int, module: Any) -> None:
            try:
                sync.on_change(event_session, module, event, open_running)
            except (ConfigError, mgmtd.MgmtdError, frr_tools.FrrError) as exc:
                # Whole-module subscriptions: sysrepo-python only forwards the message
                # for xpath subscriptions, so report it on the event session here.
                error = SysrepoCallbackFailedError(str(exc))
                event_session.set_error(error)
                raise error from exc

        with open_running() as running:
            try:
                sync.apply(running)
            except Exception as exc:
                log(f"ERROR: initial configuration: {exc}")
                sync.resync(running)

        modules = list(mgmtd.MODULES)
        for renderer in sync.renderers:
            modules += [m for m in renderer.modules if m not in modules]
        for module in modules:
            try:
                session.subscribe_module_change_unsafe(module, None, callback, private_data=module)
            except SysrepoError as exc:
                log(f"WARNING: {module} is not available ({exc})")
        for module, xpath in mgmtd.OPER_XPATHS:
            try:
                # Query the subscription xpath, not the request one: clients send
                # expressions such as "(/frr-vrf:lib)" that mgmtd does not parse, and
                # sysrepo filters the returned subtree anyway.
                session.subscribe_oper_data_request(
                    module,
                    xpath,
                    lambda _req_xpath, sub_xpath: sync.on_oper(sub_xpath),
                    private_data=xpath,
                )
            except SysrepoError as exc:
                log(f"WARNING: no operational data for {xpath} ({exc})")
        log(f"serving {len(modules)} modules")
        stop.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
