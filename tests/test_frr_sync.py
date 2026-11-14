import pytest
from frr_sync import FrrSync
from tarvos_frr.renderers import ConfigError
from tarvos_frr.renderers.bgp import BgpRenderer
from tarvos_frr.renderers.ospf import OspfRenderer
from tarvos_frr.vtysh import FrrError

RUNNING = "frr version 10.7.1\n!\ninterface eth1\n description x\nexit\n!\nend\n"


class FakeFrr:
    def __init__(self, daemons=("mgmtd", "zebra", "bgpd", "ospfd")):
        self.daemons = set(daemons)
        self.reloads: list[str] = []
        self.fail_reload = False

    def running_daemons(self):
        return self.daemons

    def running_config(self):
        return RUNNING

    def check_config(self, text):
        pass

    def reload(self, text):
        self.reloads.append(text)
        if self.fail_reload:
            raise FrrError("% Unknown command")

    def mgmt_transaction(self, commands):
        raise AssertionError("no native data in these tests")


class FakeSession:
    def __init__(self, data):
        self.data = data

    def get_data(self, xpath):
        return self.data.get(xpath, {})


BGP = {"/tarvos-bgp:bgp": {"bgp": {"instance": [{"vrf": "default", "asn": 65000}]}}}
OSPF = {
    "/tarvos-ospf:ospf": {"ospf": {"interface": [{"name": "eth1", "area": "0.0.0.0"}]}},
}


def make_sync(frr):
    sync = FrrSync([BgpRenderer(), OspfRenderer()], frr, read=lambda s, xpath: s.get_data(xpath))
    sync.sync_native = lambda session, modules=None: False  # native path tested elsewhere
    return sync


def test_apply_reloads_once_and_is_idempotent():
    frr = FakeFrr()
    sync = make_sync(frr)
    sync.apply(FakeSession({**BGP, **OSPF}))
    assert len(frr.reloads) == 1
    assert "router bgp 65000\nexit" in frr.reloads[0]
    assert "interface eth1\n description x\n ip ospf area 0.0.0.0\nexit" in frr.reloads[0]
    sync.apply(FakeSession({**BGP, **OSPF}))
    assert len(frr.reloads) == 1


def test_event_only_rerenders_its_module():
    frr = FakeFrr()
    sync = make_sync(frr)
    sync.apply(FakeSession({**BGP, **OSPF}))
    # A tarvos-bgp event session shows tarvos-ospf as committed (here: empty). The OSPF
    # part must come from what was applied last, not be removed.
    sync.apply(FakeSession({"/tarvos-bgp:bgp": {"bgp": {"instance": []}}}), {"tarvos-bgp"})
    last = frr.reloads[-1]
    assert "router bgp" not in last
    assert " ip ospf area 0.0.0.0" in last


def test_missing_daemon_is_refused_before_touching_frr():
    frr = FakeFrr(daemons=("mgmtd", "zebra"))
    with pytest.raises(ConfigError, match="bgpd"):
        make_sync(frr).apply(FakeSession(BGP))
    assert frr.reloads == []


def test_failed_reload_keeps_ownership_for_resync():
    frr = FakeFrr()
    sync = make_sync(frr)
    frr.fail_reload = True
    with pytest.raises(FrrError):
        sync.apply(FakeSession(BGP))
    frr.fail_reload = False
    sync.resync(FakeSession({}))  # committed datastore has no BGP
    assert "router bgp" not in frr.reloads[-1]
    assert sync.rendered["tarvos-bgp"].empty and not sync.dirty


def test_frr_reload_errors_are_clean_and_valid_in_xml():
    # v2.1.1 day-2 finding: an ASN change refused by bgpd came back with frr-reload's
    # ANSI colours (ESC is invalid in XML 1.0: ncclient could not parse the rpc-error).
    from tarvos_common import operator_message
    from tarvos_frr.vtysh import _clean

    raw = (
        "2026-11-14 19:41:55,103 \x1b[91m  ERROR\x1b[0m: Failed to execute no router bgp 65000\n"
        "[566|mgmtd] sending configuration\n[573|bgpd] sending configuration\n"
        "BGP instance name and AS number mismatch\nBGP instance is already running; AS is 65000\n"
        "Waiting for children to finish applying config...\n[566|mgmtd] done\n"
        "BGP instance is already running; AS is 65000\n"
    )
    assert _clean(raw) == (
        "Failed to execute no router bgp 65000\nBGP instance name and AS number mismatch\n"
        "BGP instance is already running; AS is 65000"
    )
    assert operator_message("a\x1b[1mb\x00c\x07") == "abc"
    assert len(operator_message("x" * 5000)) == 2000
